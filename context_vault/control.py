"""Codex 按线程排队、认领与回执；不启动网络、不运行模型。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Any, Mapping, Optional
import uuid

from .atomic import atomic_write_json, lease_lock
from .host_config import validate_model
from .runtime import HookContext, RuntimeKind
from .session_source import SessionRef, resolve_current_session

ATTACH_TTL = 60
CAPABILITY_TTL = 3600
CLAIM_TTL = 30
MAX_STATE_BYTES = 1024 * 1024
MAX_OPERATION_IDS = 512
_TOKEN = re.compile(r"([0-9a-f]{24})\.([A-Za-z0-9_-]{43})")
_OP_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MODULE_ROOT = Path(__file__).resolve().parents[1] / "skills/vault-loader"


def loader_module(name):
    """仅加载同一插件内的 scripts；已有同名包来自别处时拒绝。"""
    package = sys.modules.get("scripts")
    origin = getattr(package, "__file__", None)
    if package is not None and (not origin or not Path(origin).resolve().is_relative_to(_MODULE_ROOT.resolve())):
        raise ValueError("unexpected loader package")
    root = str(_MODULE_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    module = importlib.import_module("scripts." + name)
    if not Path(module.__file__).resolve().is_relative_to(_MODULE_ROOT.resolve()):
        raise ValueError("unexpected loader module")
    return module


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def core_session_id(thread_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "context-vault/codex/" + thread_id))


def _path(home, key):
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{24}", key):
        raise ValueError("invalid control identity")
    base = Path(home) / ".context-vault/state/live/codex"
    # 固定目录链不能被链接重定向到其它宿主或用户文件。
    for path in (*reversed(list(base.parents)[:3]), base):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("linked control directory")
    target = base / (key + ".json")
    if target.is_symlink():
        raise ValueError("linked control state")
    return target


def _read(path):
    if not path.exists():
        return {}
    if not stat.S_ISREG(os.lstat(path).st_mode) or path.stat().st_size > MAX_STATE_BYTES:
        raise ValueError("invalid control state")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("v") != 2:
        raise ValueError("invalid control state")
    return data


def _write(path, data):
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("control state capacity exceeded")
    atomic_write_json(path, data, private=True)


def _public(data):
    result = deepcopy({key: data.get(key) for key in ("host", "thread_id", "epoch", "queued_revision",
        "applied_revision", "snapshot", "items", "snapshot_ts", "panel_base_url", "display")})
    result["intent"] = deepcopy(data.get("desired", {}))
    result["read_coverage"] = "structured_success_only; other_tools_unknown"
    if result["snapshot"] is not None:
        result["snapshot"] = {"host": "codex", "thread_id": data["thread_id"],
            "epoch": data.get("snapshot_epoch", 0), "turn_id": data.get("snapshot_turn"),
            "revision": data.get("snapshot_revision", 0), "core": result["snapshot"]}
    return result


def _authorized(data, capability):
    attachment = data.get("attachment", {})
    if (not attachment or time.time() >= attachment.get("expires", 0)
            or not secrets.compare_digest(attachment.get("digest", ""), _digest(capability))):
        raise ValueError("invalid or expired capability")


def _cap_path(capability, home):
    match = _TOKEN.fullmatch(capability) if isinstance(capability, str) else None
    if match is None:
        raise ValueError("invalid capability")
    return _path(home, match[1])


def _ref_path(ref: SessionRef, home: Path):
    if ref.host != "codex" or ref.actor_kind != "main":
        raise ValueError("only verified main Codex threads can attach")
    verified = resolve_current_session(host="codex", context={"thread_id": ref.thread_id,
        "transcript_path": str(ref.transcript_path), "codex_home": os.environ.get("CODEX_HOME", str(home / ".codex"))}, home=home)
    if verified.actor_kind != "main":
        raise ValueError("main thread identity required")
    key = _digest(ref.thread_id)[:24]
    return _path(home, key)


def _initial(thread_id, home):
    from .lifecycle import current_epoch
    return {"v": 2, "host": "codex", "thread_id": thread_id,
        "epoch": current_epoch(home=home, thread_id=thread_id),
        "queued_revision": 0, "applied_revision": 0, "desired": {}, "operations": {},
        "annotations": [], "persist": [], "items": {}, "snapshot": None, "snapshot_ts": None}


def local_state(ref: SessionRef, *, home: Path) -> dict:
    """可信本地调用读取；不创建附件、不续租、不向浏览器授予凭证。"""
    path = _ref_path(ref, home)
    data = _read(path) or _initial(ref.thread_id, home)
    if data["thread_id"] != ref.thread_id:
        raise ValueError("thread mismatch")
    return _public(data)


def set_panel_base_url(ref: SessionRef, url: str, *, home: Path) -> None:
    """仅记录非秘密页面地址，不用它判断服务身份或授予授权。"""
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.netloc != "127.0.0.1:" + str(parsed.port) or parsed.path != "/"
            or parsed.query or parsed.fragment):
        raise ValueError("invalid panel address")
    path = _ref_path(ref, home)
    with lease_lock(path):
        data = _read(path)
        if data.get("thread_id") != ref.thread_id:
            raise ValueError("thread mismatch")
        data["panel_base_url"] = url
        data.pop('bootstrap', None)
        _write(path, data)


def attach(ref: SessionRef, *, home: Path) -> dict:
    path = _ref_path(ref, home)
    key = _digest(ref.thread_id)[:24]
    capability = key + "." + secrets.token_urlsafe(32)
    with lease_lock(path):
        from .lifecycle import current_epoch
        data = _read(path) or _initial(ref.thread_id, home)
        data["epoch"] = current_epoch(home=home, thread_id=ref.thread_id)
        if data.get("thread_id") != ref.thread_id:
            raise ValueError("control identity mismatch")
        now = time.time()
        data["attachment"] = {"digest": _digest(capability), "expires": now + CAPABILITY_TTL,
                              "lease_until": now + ATTACH_TTL}
        _write(path, data)
        return dict(_public(data), capability=capability)


def get_state(capability: str, *, home: Path) -> dict:
    path = _cap_path(capability, home)
    with lease_lock(path):
        data = _read(path)
        _authorized(data, capability)
        data["attachment"]["lease_until"] = time.time() + ATTACH_TTL
        _write(path, data)
        return _public(data)


def detach(capability: str, *, home: Path) -> None:
    path = _cap_path(capability, home)
    with lease_lock(path):
        data = _read(path)
        _authorized(data, capability)
        data["attachment"]["lease_until"] = 0
        _write(path, data)


def _list(raw, cap):
    if not isinstance(raw, list) or len(raw) > cap or any(not isinstance(x, str) for x in raw):
        raise ValueError("invalid operation list")
    return list(dict.fromkeys(raw))


def _item(data, key):
    item = data.get("items", {}).get(key) if isinstance(key, str) else None
    if not isinstance(item, dict) or not loader_module("_note_paths").safe_rel_note_path(item.get("path")):
        raise ValueError("unknown note identity")
    return item


def submit_ops(capability: str, ops: Mapping[str, Any], *, home: Path) -> dict:
    return _submit(_cap_path(capability, home), ops, home=home, capability=capability)


def submit_local_ops(ref: SessionRef, ops: Mapping[str, Any], *, home: Path) -> dict:
    """本地入口先验证精确主线程；浏览器入口只能使用 capability。"""
    return _submit(_ref_path(ref, home), ops, home=home, thread_id=ref.thread_id)


def record_read(ref: SessionRef, rel: str, *, home: Path) -> dict:
    """只接收读取成功后的索引路径；按线程/代际/轮次去重，下一轮交现有 metrics 核心。"""
    from . import lifecycle
    path = _ref_path(ref, home)
    if not lifecycle.known_thread(home=home, thread_id=ref.thread_id):
        return {"observed": False, "reason": "context_unknown"}
    if not loader_module("_note_paths").safe_rel_note_path(rel):
        raise ValueError("invalid note path")
    with lifecycle.locked_thread(home=home, thread_id=ref.thread_id) as (_, metadata):
        with lease_lock(path):
            data = _read(path) or _initial(ref.thread_id, home)
            if data["thread_id"] != ref.thread_id or data["epoch"] != metadata["epoch"]:
                return {"observed": False, "reason": "context_changed"}
            identity = _digest(json.dumps([ref.thread_id, metadata["epoch"], metadata.get("last_turn_id"), rel]))
            if identity in data.get("seen_reads", []):
                return {"observed": True, "deduplicated": True}
            queue = data.get("reads", [])
            if len(queue) >= loader_module("_session_ops").MAX_READS:
                return {"observed": False, "reason": "observation_capacity"}
            data["reads"] = queue + [{"id": identity, "path": rel}]
            data["seen_reads"] = (data.get("seen_reads", []) + [identity])[-256:]
            _write(path, data)
    return {"observed": True, "deduplicated": False}


def _submit(path, ops, *, home, capability=None, thread_id=None):
    allowed = {"epoch", "operation_id", "exclude", "pin", "unpin_llm", "settings", "annotations", "persist"}
    if (not isinstance(ops, dict) or set(ops) - allowed
            or not isinstance(ops.get("operation_id"), str) or not _OP_ID.fullmatch(ops["operation_id"])):
        raise ValueError("invalid control operation")
    encoded = json.dumps(ops, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if len(encoded.encode("utf-8")) > 100_000:
        raise ValueError("operation too large")
    limits, policy = loader_module("_session_ops"), loader_module("_fine_config")
    with lease_lock(path):
        data = _read(path) or (_initial(thread_id, home) if thread_id is not None else {})
        if thread_id is None:
            _authorized(data, capability)
        elif data.get("thread_id") != thread_id:
            raise ValueError("thread mismatch")
        if type(ops.get("epoch")) is not int or ops["epoch"] != data["epoch"]:
            raise ValueError("stale control epoch")
        op_id, digest = ops["operation_id"], _digest(encoded)
        previous = data["operations"].get(op_id)
        if previous:
            if previous["digest"] != digest:
                raise ValueError("operation identity reused with different content")
            return {"status": "queued", "queued_revision": previous["revision"]}
        if len(data["operations"]) >= MAX_OPERATION_IDS:
            raise ValueError("operation history capacity exceeded")
        desired = deepcopy(data["desired"])
        for key, cap in (("exclude", limits.MAX_EXCLUDE), ("pin", limits.MAX_PIN), ("unpin_llm", limits.MAX_UNPIN)):
            if key in ops:
                desired[key] = [_item(data, item)["path"] for item in _list(ops[key], cap)]
        if "settings" in ops:
            settings = ops["settings"]
            if not isinstance(settings, dict) or len(settings) > limits.MAX_SETTINGS or set(settings) - set(policy.PANEL_KEYS):
                raise ValueError("unknown setting")
            for key, value in settings.items():
                if key == "fine_model":
                    validate_model(value, host="codex")
                elif not policy.validate_value(key, value)[0]:
                    raise ValueError("invalid setting value")
            desired["settings"] = deepcopy(settings)
        annotations = ops.get("annotations", [])
        if not isinstance(annotations, list) or len(annotations) + len(data["annotations"]) > limits.MAX_ANNOTATIONS:
            raise ValueError("annotation capacity exceeded")
        if annotations and not (data.get("snapshot") or {}).get("metrics_on"):
            raise ValueError("metrics_off")
        for index, ann in enumerate(annotations):
            if (not isinstance(ann, dict) or set(ann) - {"item", "verdict", "annotation_id"}
                    or ann.get("verdict") not in limits.ANN_VERDICTS):
                raise ValueError("invalid annotation")
            item = _item(data, ann.get("item"))
            if (not isinstance(ann.get("annotation_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", ann["annotation_id"]) or not item.get("annotation_id")
                    or not secrets.compare_digest(ann["annotation_id"], item["annotation_id"])):
                raise ValueError("stale annotation identity")
            if item.get("kind") not in limits.ANN_KINDS:
                raise ValueError("annotation source unavailable")
            data["annotations"].append({"id": _digest(json.dumps([data["thread_id"], op_id, index], separators=(",", ":")))[:32],
                "path": item["path"], "kind": item["kind"], "verdict": ann["verdict"],
                "session": core_session_id(data["thread_id"]), "prompt_h": item.get("prompt_h", ""),
                "ts": int(time.time()), "revision": data["queued_revision"] + 1})
        if "persist" in ops:
            keys = _list(ops["persist"], limits.MAX_PERSIST)
            if any(key not in policy.PANEL_KEYS or key not in desired.get("settings", {}) for key in keys):
                raise ValueError("unknown persist setting")
            if len(data["persist"]) + len(keys) > limits.MAX_PERSIST:
                raise ValueError("persist capacity exceeded")
            data["persist"].extend({"operation_id": op_id, "key": key,
                                    "revision": data["queued_revision"] + 1} for key in keys)
        data["desired"] = desired
        data["queued_revision"] += 1
        data["operations"][op_id] = {"digest": digest, "revision": data["queued_revision"]}
        _write(path, data)
        return {"status": "queued", "queued_revision": data["queued_revision"]}


def _context_path(ctx, home):
    if (ctx.runtime is not RuntimeKind.CODEX or ctx.actor_kind != "main" or not ctx.thread_id
            or (not ctx.stable_event_id and ctx.event != "SessionStart")
            or ctx.event not in ("SessionStart", "UserPromptSubmit")):
        return None
    return _path(home, _digest(ctx.thread_id)[:24])


def has_attachment(ctx: HookContext, *, home: Path) -> bool:
    path = _context_path(ctx, home)
    if path is None or not path.exists():
        return False
    attachment = _read(path).get("attachment", {})
    return time.time() < min(attachment.get("expires", 0), attachment.get("lease_until", 0))


def advance_epoch(ctx: HookContext, *, home: Path) -> None:
    path = _context_path(ctx, home)
    if path is None or not path.exists():
        return
    with lease_lock(path):
        data = _read(path)
        if data.get("thread_id") != ctx.thread_id or ctx.context_epoch <= data["epoch"]:
            return
        data["epoch"], data["applied_revision"] = ctx.context_epoch, 0
        data.pop("claim", None)
        data.pop('bootstrap', None)
        data['snapshot'], data['snapshot_ts'], data['display'] = None, None, None
        data['items'] = _intent_items(data)
        data["seen_turns"] = []
        _write(path, data)


def begin_bootstrap_capture(ctx: HookContext, *, home: Path) -> None:
    """仅由已核验 Desktop 的 SS 入口调用，不授予浏览器授权。"""
    path = _context_path(ctx, home)
    if path is None or ctx.event != 'SessionStart':
        return
    with lease_lock(path):
        data = _read(path) or _initial(ctx.thread_id, home)
        if data.get('thread_id') != ctx.thread_id or data.get('epoch') != ctx.context_epoch:
            return
        data['bootstrap'] = {'epoch': ctx.context_epoch, 'SessionStart': True, 'UserPromptSubmit': True}
        _write(path, data)


def claim_control(ctx: HookContext, *, home: Path, allow_bootstrap=False) -> Optional[dict]:
    path = _context_path(ctx, home)
    if path is None or not path.exists():
        return None
    with lease_lock(path):
        data = _read(path)
        if data.get("thread_id") != ctx.thread_id or data.get("epoch") != ctx.context_epoch:
            return None
        attachment = data.get("attachment", {})
        now = time.time()
        attached = now < min(attachment.get("expires", 0), attachment.get("lease_until", 0))
        bootstrap = data.get('bootstrap', {})
        capture = bool(allow_bootstrap and bootstrap.get('epoch') == ctx.context_epoch
                       and bootstrap.get(ctx.event) is True)
        if not attached and not capture and not (data["desired"] or data["annotations"] or data["persist"] or data.get("reads")):
            return None
        claim = data.get("claim", {})
        if claim and now < claim["until"]:
            return None
        # 已结束的同一事件不得重新领取，崩溃后的同一事件也不重放副作用。
        if ctx.event_id in data.get("seen_turns", []):
            return None
        core = {"v": 1, "exclude": [], "pin": [], "unpin_llm": [], **deepcopy(data["desired"]),
                "annotations": [{k: v for k, v in ann.items() if k != "revision"}
                                for ann in data["annotations"]],
                "persist": list(dict.fromkeys(p["key"] for p in data["persist"])),
                "reads": list(dict.fromkeys(item["path"] for item in data.get("reads", [])))}
        claim = {"turn_id": ctx.event_id, "epoch": ctx.context_epoch,
                 "revision": data["queued_revision"], "until": now + CLAIM_TTL,
                 "capture_only": capture,
                 "read_ids": [item["id"] for item in data.get("reads", [])]}
        if capture:
            bootstrap[ctx.event] = False
        data["claim"] = claim
        data["seen_turns"] = (data.get("seen_turns", []) + [ctx.event_id])[-256:]
        _write(path, data)
        return {"host": "codex", "thread_id": ctx.thread_id, "epoch": ctx.context_epoch,
                "turn_id": ctx.event_id, "revision": claim["revision"], "core": core}


def _snapshot_items(report, event, *, epoch, turn_id):
    groups = [(report.get("injected", []), None, 'coarse'), (report.get("session_start", []), None, 'session_start'),
              (report.get("excluded", []), None, 'excluded'), (report.get("supps", []), "llm_supplement", 'supps')]
    pins = report.get("pins", {})
    if isinstance(pins, dict):
        groups.extend([(pins.get("llm", []), "llm_pinned", 'llm'), (pins.get("user", []), None, 'user')])
    items = {}
    paths = loader_module("_note_paths")
    for entries, kind, group in groups:
        for item in entries if isinstance(entries, list) else []:
            if isinstance(item, dict) and paths.safe_rel_note_path(item.get("path")):
                saved = deepcopy(item)
                saved['_group'] = group
                if kind:
                    saved["kind"] = kind
                saved["prompt_h"] = report.get("prompt_h", "")
                # 同篇可同时粗排与模型点名；不能让后一个来源覆盖前一个标注入口。
                key = _digest(item["path"] + "\0" + str(saved.get("kind", "")) + "\0" + event)[:24]
                # 笔记操作保持稳定身份；标注单独绑定实际显示的轮次，旧页面不得重定向至新提问。
                saved["annotation_id"] = _digest(json.dumps(
                    [key, epoch, turn_id, saved["prompt_h"]], separators=(",", ":")))[:32]
                items[key] = saved
    return items


def _intent_items(data):
    """跨代际只保留用户意图身份，永不继承旧提问标注身份。"""
    items = {}
    paths = loader_module('_note_paths')
    for group in ('exclude', 'pin', 'unpin_llm'):
        for path in data.get('desired', {}).get(group, []):
            if paths.safe_rel_note_path(path):
                key = _digest(path + '\0user_intent\0' + str(data['epoch']))[:24]
                items[key] = {'path': path, 'kind': 'user_intent', '_group': 'intent'}
    return items


def ack_control(ctx: HookContext, *, revision: int, report: Mapping[str, Any], home: Path) -> None:
    path = _context_path(ctx, home)
    if path is None or not path.exists() or not isinstance(report, dict) or report.get("status") == "duplicate":
        return
    with lease_lock(path):
        data = _read(path)
        claim = data.get("claim", {})
        if (data.get("thread_id") != ctx.thread_id or data.get("epoch") != ctx.context_epoch
                or claim.get("turn_id") != ctx.event_id or claim.get("revision") != revision
                or time.time() >= claim.get("until", 0)):
            return
        data.pop("claim", None)
        attachment = data.get("attachment", {})
        if claim.get('capture_only') or time.time() < min(attachment.get("expires", 0), attachment.get("lease_until", 0)):
            from .panel_view import merge_display
            data['display'] = merge_display(data.get('display'), report, ctx.event)
            data["snapshot"] = deepcopy(report)
            data["snapshot_epoch"], data["snapshot_revision"] = ctx.context_epoch, revision
            data["snapshot_turn"] = ctx.event_id if ctx.event == "UserPromptSubmit" else None
            if report.get("status") in ("scored", "dry_run"):
                retained = {key: item for key, item in data["items"].items()
                            if (item.get("_event") == "SessionStart") != (ctx.event == "SessionStart")}
                fresh = _snapshot_items(report, ctx.event, epoch=ctx.context_epoch, turn_id=ctx.event_id)
                for item in fresh.values():
                    item["_event"] = ctx.event
                data["items"] = {**retained, **fresh}
            elif ctx.event == 'UserPromptSubmit':
                # 补充属于本轮；gate/supp_only 不能留下上一轮的补充或改变粗排标注目标。
                retained = {key: item for key, item in data['items'].items() if item.get('kind') != 'llm_supplement'}
                fresh = _snapshot_items({'supps': report.get('supps', [])}, ctx.event,
                                        epoch=ctx.context_epoch, turn_id=ctx.event_id)
                for item in fresh.values():
                    item['_event'] = ctx.event
                data['items'] = {**retained, **fresh}
            data['items'].update(_intent_items(data))
            data["snapshot_ts"] = int(time.time())
        # 闸门前已经发生的副作用回执必须出队，与整体 revision 生效分别判断。
        acked = set(report.get("acked", []))
        persisted = set(report.get("persisted", []))
        for rejected in report.get("rejected", []):
            if rejected.get("what") == "annotation":
                acked.add(rejected.get("key"))
            if rejected.get("what") == "persist":
                persisted.add(rejected.get("key"))
        data["annotations"] = [ann for ann in data["annotations"]
            if ann["revision"] > revision or ann["id"] not in acked]
        data["persist"] = [item for item in data["persist"]
            if item["revision"] > revision or item["key"] not in persisted]
        if report.get("status") == "scored" and ctx.event == "UserPromptSubmit":
            consumed = set(claim.get("read_ids", []))
            data["reads"] = [item for item in data.get("reads", []) if item["id"] not in consumed]
            unresolved = any(item["revision"] <= revision for item in data["annotations"] + data["persist"])
            if not unresolved and not report.get("rejected"):
                data["applied_revision"] = max(data["applied_revision"], revision)
        _write(path, data)
