"""mod 与 Python 之间的按轮请求与会话快照。

契约（字段、status / reason 取值与是否终局、两侧上限、摘要、清理责任）见仓库的
`.claude/rules/mod.md`「契约 v1」；字段名由 `tests/fixtures/mod_contract/` 钉住。

请求由 mod 在 `next()` 之前写、本模块读完即删；快照由本模块原子写、mod 只读。按「每个文件
单写者」设计，不加锁——前提是同一会话不会并发跑两轮 UPS（未实测）。快照与请求的路径只由
stdin 里的 session_id / prompt_id 推出：mod 不向 Python 提供文件系统路径，所以 mod 侧算错
路径的后果只是「请求没被读到」，不会把 Python 的写入引到别处。快照同时是标注的 ack 通道。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 判据定义在 `_state`（理由见那里的 docstring）；本模块沿用原名。
from ._state import is_link_like as _is_link_like

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ANN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# `_metrics.h` 的输出：加盐 SHA 前 16 位十六进制
PROMPT_H_RE = re.compile(r"^[0-9a-f]{16}$")

REQUEST_VERSION = 1
MAX_REQUEST_BYTES = 1_000_000
MAX_EXCLUDE = 50
MAX_PIN = 8
MAX_UNPIN = 8
MAX_ANNOTATIONS = 50
MAX_ITEM_CHARS = 512
# 契约 v1 增量（spec 2026-10-13 §10.6）：面板的会话设置、「设为默认」与主模型读过的 vault 笔记。
# 两侧镜像：mod 的 LIMITS.settings / persist / reads。
MAX_SETTINGS = 10
MAX_PERSIST = 10
MAX_READS = 50
MAX_SETTING_KEY_CHARS = 64
MAX_SETTING_STR_CHARS = 64
# 与 analyze_metrics.KINDS / VERDICTS 对齐（守卫 test_ann_constants_track_analyze_metrics）；
# 这里不 import analyze_metrics：它是 1800 行的 CLI 模块，UPS 热路径不该为两组常量加载它。
ANN_KINDS = ("admitted_fulltext", "admitted_list", "llm_pinned", "llm_supplement")
ANN_VERDICTS = ("relevant", "irrelevant", "unsure")


def live_dir(home: Optional[Path] = None) -> Path:
    """`<home>/.context-vault/state/live/`：与命名空间无关，和 `state/events/` 同类
    （不落 `state/claude/`，否则未迁移机器会被误判为已迁移，见 spec F25）。"""
    try:
        from context_vault.paths import context_home
        return context_home(home) / "state" / "live"
    except ImportError:          # legacy 独立布局拿不到 context_vault
        return (home or Path.home()) / ".context-vault" / "state" / "live"


def session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


def _valid_uuid(v) -> bool:
    return isinstance(v, str) and bool(UUID_RE.match(v))


def request_path(session_id, prompt_id, home: Optional[Path] = None) -> Optional[Path]:
    if not (_valid_uuid(session_id) and _valid_uuid(prompt_id)):
        return None
    return live_dir(home) / f"{session_hash(session_id)}-{prompt_id}.req.json"


def ss_request_path(session_id, home: Optional[Path] = None) -> Optional[Path]:
    if not _valid_uuid(session_id):
        return None
    return live_dir(home) / f"{session_hash(session_id)}-ss.req.json"


def snapshot_path(session_id, home: Optional[Path] = None) -> Optional[Path]:
    if not _valid_uuid(session_id):
        return None
    return live_dir(home) / f"{session_hash(session_id)}.json"


def request_exists(path: Optional[Path]) -> bool:
    if path is None:
        return False
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def take_request(path: Optional[Path]) -> "tuple[object, bool]":
    """读并删除请求文件。返回 (解析结果或 None, 文件是否存在过)。

    不跟随链接（`lstat`）；超过 `MAX_REQUEST_BYTES` 或解析失败都按「没有可用请求」处理，
    但文件照样删掉，免得每轮重读同一个坏文件。
    """
    if path is None:
        return None, False
    try:
        st = os.lstat(path)
    except OSError:
        return None, False
    obj = None
    try:
        if stat.S_ISREG(st.st_mode) and st.st_size <= MAX_REQUEST_BYTES:
            obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        obj = None
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return obj, True


@dataclass(frozen=True)
class SessionOps:
    exclude: tuple = ()
    pin: tuple = ()
    unpin_llm: tuple = ()
    annotations: tuple = ()
    rejected: tuple = ()
    digest: Optional[str] = None
    # 契约 v1 增量：settings 只过结构校验（键是否认识、值是否合法由 _fine_config 判），
    # persist 只含面板认识的键名，reads 只含过了 safe_rel_note_path 的 vault 相对路径
    settings: dict = field(default_factory=dict)
    persist: tuple = ()
    reads: tuple = ()
    # 原始请求有没有带一个**对象**形态的 `settings`。缺键（2.3.0 形态的 mod）与值不是对象（null / 字符串 /
    # 列表）都按「没带」：沿用已存的会话设置；只有显式给出对象（含 `{}`，即「清空」）才为 True。几种情形解析后的
    # `settings` 都可能是 {}，只能靠它区分（`prompt_submit_load._request_settings`）。整份作废的请求为 False。
    has_settings: bool = False


def _str_list(raw, cap: int, what: str, rejected: list) -> list:
    """先截断再逐条校验；元素必须是非空、不超长的 str；去重保序。"""
    if not isinstance(raw, list):
        return []
    if len(raw) > cap:
        rejected.append({"what": what, "key": "", "reason": "over_cap"})
    out: list = []
    for item in raw[:cap]:
        if isinstance(item, str) and 0 < len(item) <= MAX_ITEM_CHARS:
            if item not in out:
                out.append(item)
        else:
            key = item[:64] if isinstance(item, str) else ""
            rejected.append({"what": what, "key": key, "reason": "bad_item"})
    return out


def _annotation_list(raw, rejected: list) -> list:
    if not isinstance(raw, list):
        return []
    if len(raw) > MAX_ANNOTATIONS:
        rejected.append({"what": "annotation", "key": "", "reason": "over_cap"})
    out: list = []
    for a in raw[:MAX_ANNOTATIONS]:
        aid = a.get("id") if isinstance(a, dict) else None
        key = aid[:64] if isinstance(aid, str) else ""
        ok = isinstance(a, dict) and isinstance(aid, str) and bool(ANN_ID_RE.match(aid))
        if ok:
            path, ts, ph = a.get("path"), a.get("ts"), a.get("prompt_h")
            ok = (isinstance(path, str) and 0 < len(path) <= MAX_ITEM_CHARS
                  and a.get("kind") in ANN_KINDS
                  and a.get("verdict") in ANN_VERDICTS
                  and _valid_uuid(a.get("session"))
                  and isinstance(ph, str) and bool(PROMPT_H_RE.match(ph))
                  and isinstance(ts, (int, float)) and not isinstance(ts, bool))
        if not ok:
            rejected.append({"what": "annotation", "key": key, "reason": "bad_item"})
            continue
        out.append({"id": aid, "path": a["path"], "kind": a["kind"], "verdict": a["verdict"],
                    "session": a["session"], "prompt_h": a["prompt_h"], "ts": int(a["ts"])})
    return out


def _panel_keys() -> tuple:
    """面板认识的设置键（`_fine_config.PANEL_KEYS`）。延迟导入：本模块在 UPS 热路径上单独兜底，
    拿不到时一律不认，persist 全部按 bad_item 拒收。"""
    try:
        from ._fine_config import PANEL_KEYS
        return tuple(PANEL_KEYS)
    except Exception:  # noqa: BLE001
        return ()


def _settings_dict(raw, rejected: list) -> dict:
    """会话设置的结构校验：键是非空、不超长的 str；值是 bool、|n| ≤ 1e9 的 int 或非空不超长的 str。
    float（含 NaN / Infinity）、None、列表、对象一律 bad_item。先截断再逐条校验，与 `_str_list` 同形。"""
    if not isinstance(raw, dict):
        return {}
    items = list(raw.items())
    if len(items) > MAX_SETTINGS:
        rejected.append({"what": "settings", "key": "", "reason": "over_cap"})
    out: dict = {}
    for k, v in items[:MAX_SETTINGS]:
        ok = isinstance(k, str) and 0 < len(k) <= MAX_SETTING_KEY_CHARS
        if ok:
            if isinstance(v, bool):
                pass
            elif isinstance(v, int):
                ok = abs(v) <= 10 ** 9
            elif isinstance(v, str):
                ok = 0 < len(v) <= MAX_SETTING_STR_CHARS
            else:
                ok = False
        if not ok:
            rejected.append({"what": "settings", "key": k[:64] if isinstance(k, str) else "",
                             "reason": "bad_item"})
            continue
        out[k] = v
    return out


def _persist_list(raw, rejected: list) -> list:
    """「设为默认」的键名：结构同 `_str_list`，另只认面板的键。是否只收窄由 `prompt_submit_load._apply_persist`
    经 `_fine_config.check_persist` 判。"""
    keys = _str_list(raw, MAX_PERSIST, "persist", rejected)
    panel = _panel_keys()
    out: list = []
    for k in keys:
        if k in panel:
            out.append(k)
        else:
            rejected.append({"what": "persist", "key": k[:64], "reason": "bad_item"})
    return out


def _reads_list(raw) -> list:
    """主模型读过的 vault 笔记（mod 观测，spec D6）。是观测数据：不合格的条目静默丢弃，不进 rejected
    （它们不是用户操作，mod 也不需要据此清队列——mod 送出即清空）。"""
    if not isinstance(raw, list):
        return []
    try:
        from ._note_paths import safe_rel_note_path
    except Exception:  # noqa: BLE001
        return []
    out: list = []
    for p in raw[:MAX_READS]:
        if (isinstance(p, str) and 0 < len(p) <= MAX_ITEM_CHARS
                and safe_rel_note_path(p) and p not in out):
            out.append(p)
    return out


def request_digest(obj) -> Optional[str]:
    """请求摘要：mod 侧对同一组值做
    `JSON.stringify([v, exclude, pin, unpin_llm, ids, settings, persist, reads])`，取 sha256 前 32 位。
    取的是**原样**值（校验之前），mod 才能逐字复算；settings 是对象，键序即 mod 写出的顺序
    （JSON 解析保序）。缺这三项（2.3.0 形态）按 {} / [] / [] 计。"""
    if not isinstance(obj, dict):
        return None

    def _raw(k):
        v = obj.get(k)
        return v if isinstance(v, list) else []

    settings = obj.get("settings")
    ids = [a.get("id") for a in _raw("annotations") if isinstance(a, dict)]
    payload = [obj.get("v"), _raw("exclude"), _raw("pin"), _raw("unpin_llm"), ids,
               settings if isinstance(settings, dict) else {}, _raw("persist"), _raw("reads")]
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def parse_request(obj) -> SessionOps:
    if not isinstance(obj, dict) or obj.get("v") != REQUEST_VERSION:
        # has_settings 显式为 False：整份作废的请求不算带了 settings，沿用已存的会话设置
        return SessionOps(rejected=({"what": "request", "key": "", "reason": "bad_request"},),
                          digest=request_digest(obj), has_settings=False)
    rejected: list = []
    exclude = _str_list(obj.get("exclude"), MAX_EXCLUDE, "exclude", rejected)
    pin = _str_list(obj.get("pin"), MAX_PIN, "pin", rejected)
    unpin = _str_list(obj.get("unpin_llm"), MAX_UNPIN, "unpin_llm", rejected)
    anns = _annotation_list(obj.get("annotations"), rejected)
    settings = _settings_dict(obj.get("settings"), rejected)
    persist = _persist_list(obj.get("persist"), rejected)
    reads = _reads_list(obj.get("reads"))
    return SessionOps(exclude=tuple(exclude), pin=tuple(pin), unpin_llm=tuple(unpin),
                      annotations=tuple(anns), rejected=tuple(rejected),
                      digest=request_digest(obj), settings=settings,
                      persist=tuple(persist), reads=tuple(reads),
                      has_settings=isinstance(obj.get("settings"), dict))


def restrict_to_index(ops: SessionOps, keys) -> SessionOps:
    """路径类操作只认当前索引（`active_entries`）里存在的篇；其余进 `rejected`。"""
    keys = set(keys)
    rejected = list(ops.rejected)

    def _keep(items, what):
        kept = []
        for p in items:
            if p in keys:
                kept.append(p)
            else:
                rejected.append({"what": what, "key": p[:64], "reason": "not_in_index"})
        return tuple(kept)

    return dataclasses.replace(ops, exclude=_keep(ops.exclude, "exclude"),
                               pin=_keep(ops.pin, "pin"),
                               unpin_llm=_keep(ops.unpin_llm, "unpin_llm"),
                               rejected=tuple(rejected))


def excluded_set(ops: Optional[SessionOps]) -> frozenset:
    """本会话排除 = exclude ∪ unpin_llm（取消一篇 LLM 点名等价于排除它，spec §6.2）。"""
    if ops is None:
        return frozenset()
    return frozenset(ops.exclude) | frozenset(ops.unpin_llm)


SNAPSHOT_VERSION = 1
SNAPSHOT_TTL_SECONDS = 24 * 3600
REQUEST_TTL_SECONDS = 3600
MAX_TITLE_CHARS = 120
MAX_LIST_ITEMS = 50
# 一份合法请求最多产生的拒收条数：每类逐条至多一条（bad_item / not_in_index / 标注终局拒收
# 互斥）再加一条 over_cap。快照不能截得比它短：截掉的 not_in_index 让 mod 永远收不到
# 「这条操作已失效」（节点 2 T-M4）。
MAX_REJECTED = ((MAX_EXCLUDE + 1) + (MAX_PIN + 1) + (MAX_UNPIN + 1) + (MAX_ANNOTATIONS + 1)
                + (MAX_SETTINGS + 1) + (MAX_PERSIST + 1))
# 快照 vault_root：mod 据它判断 Read 是否落在 vault 内，路径常超过标题上限 120，单独给上限
MAX_ROOT_CHARS = 1024
# supps[].recheck：passed 本轮提问词复核命中、failed 复核未命中；skipped 是 2.4.0 开发期闸门轮投递补充时的
# 取值，批次 3c 起不再产生，与 mod 的值域有对拍守卫，保留
SUPP_RECHECKS = ("passed", "skipped", "failed")
# settings.source 的值域
SETTING_SOURCES = ("session", "config", "default")
# 两个净化函数都不剥的可伪装字符：bidi 控制（含 U+061C 阿拉伯字母标记）、零宽与不可见运算符
# （spec 2026-10-12 §3.5）。mod 侧 STRIP_RE 同步，两侧字符集由 parity 用例钉住。
_INVISIBLE_RE = re.compile(r"[؜​-‏‪-‮⁠-⁤⁦-⁩﻿]")


def display_text(s, limit: int = MAX_TITLE_CHARS) -> str:
    """快照里要画在终端上的字段：先按注入侧折成单行，再按显示侧剥完整 C1。

    两步都要：`sanitize_injected_text(keep_newlines=False)` 折叠换行与 Unicode 行分隔符，
    `sanitize_for_display` 剥 C1（`_output.py` 的 FIX-6 注释写明显示侧必须剥）。
    """
    if not isinstance(s, str):
        return ""
    from scripts._output import sanitize_for_display, sanitize_injected_text
    t = sanitize_for_display(sanitize_injected_text(s, keep_newlines=False))
    return _INVISIBLE_RE.sub("", t)[:limit]


def finite_num(v):
    """快照里的数值字段：只放有限的 int / float，bool、非数值、NaN、±Infinity 一律 None（mod 显示「未知」）。

    `write_snapshot` 的 `json.dumps` 会把非有限数写成 `Infinity` / `NaN`，mod 的 `JSON.parse` 不认，
    整份快照作废、面板静默冻结；配置里的阈值与权重不经校验就能是这些值（full-review S-1）。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None


def note_title(path: str) -> str:
    last = path.split("/")[-1]
    return display_text(last[:-3] if last.endswith(".md") else last)


def supp_item(path: str, src_seq, recheck: str, rendered: bool) -> dict:
    """快照 `supps[]` 的一条（spec §10.6）。集合外的 recheck 与非 int 的 src_seq 记 None（mod 显示「未知」）。"""
    seq = src_seq if isinstance(src_seq, int) and not isinstance(src_seq, bool) else None
    return {"path": path, "title": note_title(path), "src_seq": seq,
            "recheck": recheck if recheck in SUPP_RECHECKS else None,
            "rendered": rendered is True}


def settings_snapshot(effective, session, locked, source) -> dict:
    """快照 `settings`（spec §10.3）：只输出面板认识的键；`effective` 与 `source` 恒含全部键。
    数值经 finite_num、字符串经 display_text：配置里的值未必干净，非有限数会让 mod 整份拒收快照。"""
    keys = _panel_keys()

    def _val(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            try:
                return finite_num(v)
            except (OverflowError, ValueError):     # 超大 int：math.isfinite 转 float 时抛
                return None
        if isinstance(v, str):
            return display_text(v, MAX_SETTING_STR_CHARS)
        return None

    eff = effective if isinstance(effective, dict) else {}
    ses = session if isinstance(session, dict) else {}
    src = source if isinstance(source, dict) else {}
    lock = set(locked) if isinstance(locked, (list, tuple, set, frozenset)) else set()
    return {
        "effective": {k: _val(eff.get(k)) for k in keys},
        "session": {k: _val(ses[k]) for k in keys if k in ses},
        "locked": [k for k in keys if k in lock],
        "source": {k: (src.get(k) if src.get(k) in SETTING_SOURCES else None) for k in keys},
    }


def vault_root_text(p) -> Optional[str]:
    """快照 `vault_root`：Python 侧 resolve 后的 vault 绝对路径。mod 不知道用户把 vault 配在哪，
    只能靠它判断主模型的 Read 是否落在 vault 内（spec §10.5；本计划「实现决定」）。

    超过 `MAX_ROOT_CHARS` 时返回 None（不下发）而不是截断：截断出来的前缀会让 mod 把 vault 外的路径
    判成 vault 内。"""
    if p is None:
        return None
    t = display_text(str(p), MAX_ROOT_CHARS + 1)
    return t if t and len(t) <= MAX_ROOT_CHARS else None


def ensure_live_dir(home: Optional[Path] = None) -> None:
    d = live_dir(home)
    if _is_link_like(d):
        raise OSError(f"live dir is a link: {d}")
    d.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def write_snapshot(path: Path, data: dict) -> None:
    """原子写；POSIX 上目录 0700、文件 0600（与 metrics 的 `.salt` 同一要求）。"""
    if _is_link_like(path.parent):
        raise OSError(f"live dir is a link: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


@dataclass
class TurnReport:
    """一轮 hook 的会话快照素材。各出口只改 `status` 与列表，写出统一由 `finalize` 做。

    `status` 取值：`disabled`、`gate:<reason>`、`scored` / `dry_run`、`pending`、`duplicate`（不写快照）。
    2.4.0 开发期还有 `supp_only`（闸门轮只投递了精排补充），批次 3c 起不再产生，mod 仍认它。
    """
    event: str = "UserPromptSubmit"
    session_id: str = ""
    prompt_id: Optional[str] = None
    home: Optional[Path] = None
    req_path: Optional[Path] = None
    snap_path: Optional[Path] = None
    request_seen: bool = False
    request_taken: bool = False
    status: str = "disabled"
    ops: Optional[SessionOps] = None
    injected: list = field(default_factory=list)
    pins_llm: list = field(default_factory=list)
    pins_user: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    session_start: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    acked: list = field(default_factory=list)
    metrics_on: bool = False
    prompt_h: str = ""
    # spec 2026-10-12 §3.1 的增量键：未计算时为 None。mod 遇 None 保留上一次的展示，从未收到过对象时
    # 才显示「未知」（`vault-mod.js` 的 absorb）
    llm: Optional[dict] = None
    thresholds: Optional[dict] = None
    # spec 2026-10-13 §10.6 的增量键：没人填时 supps / persisted 为空列表、settings / vault_root 为 None，
    # 四个键在快照里恒在
    supps: list = field(default_factory=list)
    settings: Optional[dict] = None
    persisted: list = field(default_factory=list)
    vault_root: Optional[str] = None

    @classmethod
    def for_ups(cls, hook_input: dict, home: Optional[Path] = None) -> "TurnReport":
        sid = hook_input.get("session_id") or ""
        pid = hook_input.get("prompt_id") or ""
        req = request_path(sid, pid, home)
        return cls(event="UserPromptSubmit", session_id=sid, prompt_id=pid, home=home,
                   req_path=req, snap_path=snapshot_path(sid, home),
                   request_seen=request_exists(req))

    @classmethod
    def for_session_start(cls, hook_input: dict, home: Optional[Path] = None) -> "TurnReport":
        sid = hook_input.get("session_id") or ""
        req = ss_request_path(sid, home)
        return cls(event="SessionStart", session_id=sid, prompt_id=None, home=home,
                   req_path=req, snap_path=snapshot_path(sid, home),
                   request_seen=request_exists(req))

    def take_ops(self) -> Optional[SessionOps]:
        if not self.request_seen or self.request_taken:
            return self.ops
        obj, _ = take_request(self.req_path)
        self.request_taken = True
        self.ops = parse_request(obj)
        self.rejected.extend(self.ops.rejected)
        return self.ops

    def restrict(self, keys) -> Optional[SessionOps]:
        if self.ops is None:
            return None
        before = len(self.ops.rejected)
        self.ops = restrict_to_index(self.ops, keys)
        self.rejected.extend(self.ops.rejected[before:])
        return self.ops

    def reject_annotations(self, anns, reason: str) -> None:
        for a in anns:
            self.rejected.append({"what": "annotation", "key": str(a.get("id", ""))[:64],
                                  "reason": reason})

    def to_snapshot(self) -> dict:
        snap = {
            "v": SNAPSHOT_VERSION,
            "status": self.status,
            "event": self.event,
            "prompt_id": self.prompt_id,
            "request_digest": self.ops.digest if self.ops is not None else None,
            "injected": self.injected[:MAX_LIST_ITEMS],
            "pins": {"llm": self.pins_llm[:MAX_LIST_ITEMS],
                     "user": self.pins_user[:MAX_LIST_ITEMS]},
            "excluded": self.excluded[:MAX_LIST_ITEMS],
            "session_start": self.session_start[:MAX_LIST_ITEMS],
            "rejected": self.rejected[:MAX_REJECTED],
            "acked": self.acked[:MAX_ANNOTATIONS],
            "metrics_on": bool(self.metrics_on),
            "llm": self.llm,
            "thresholds": self.thresholds,
            "ts": int(time.time()),
            "supps": self.supps[:MAX_LIST_ITEMS],
            "settings": self.settings,
            "persisted": self.persisted[:MAX_PERSIST],
            "vault_root": self.vault_root,
        }
        if self.metrics_on and self.prompt_h:
            snap["prompt_h"] = self.prompt_h
        return snap

    def finalize(self) -> None:
        """有请求时写快照，并保证请求文件不残留。

        重复事件两样都不做：请求属于抢到事件的那个进程，这里删掉会让它读空；
        若那个进程没读到（崩溃等），由 `cleanup_live` 按 1 小时过期清掉。
        """
        if self.status == "duplicate":
            return
        try:
            if self.request_seen and self.snap_path is not None:
                write_snapshot(self.snap_path, self.to_snapshot())
        finally:
            if self.req_path is not None and not self.request_taken:
                try:
                    self.req_path.unlink()
                except OSError:
                    pass


# 本插件在 live/ 下只写这三种文件名（session_hash 是 24 位十六进制）；清理只认它们，
# 目录被换成别处或混进了别的文件时也删不到无关文件（节点 2 S-M1）。
_SNAPSHOT_NAME = re.compile(r"[0-9a-f]{24}\.json")
_REQUEST_NAME = re.compile(
    r"[0-9a-f]{24}-(?:ss|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"\.req\.json")
_TMP_NAME = re.compile(r"\.[0-9a-f]{24}\.json\.\d+\.[0-9a-f]{8}\.tmp")


def _plugin_file_ttl(name: str) -> Optional[int]:
    if _SNAPSHOT_NAME.fullmatch(name):
        return SNAPSHOT_TTL_SECONDS
    if _REQUEST_NAME.fullmatch(name) or _TMP_NAME.fullmatch(name):
        return REQUEST_TTL_SECONDS
    return None


def _iter_regular_files(home: Optional[Path]):
    """`live/` 下本插件写出的常规文件；目录本身是链接或 reparse point 时一个都不给。"""
    d = live_dir(home)
    if _is_link_like(d) or not d.is_dir():
        return
    with os.scandir(d) as it:
        for ent in it:
            if _plugin_file_ttl(ent.name) is None:
                continue
            try:
                if ent.is_symlink() or not ent.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            yield ent


def cleanup_live(home: Optional[Path] = None, now: Optional[float] = None) -> int:
    """快照超过 24 小时、请求与临时文件超过 1 小时的删除；只认本插件的文件名，不跟随链接与 junction。"""
    now = time.time() if now is None else now
    removed = 0
    for ent in _iter_regular_files(home):
        ttl = _plugin_file_ttl(ent.name)
        try:
            if now - ent.stat(follow_symlinks=False).st_mtime > ttl:
                os.unlink(ent.path)
                removed += 1
        except OSError:
            continue
    return removed


def purge_live(home: Optional[Path] = None) -> int:
    """`analyze_metrics --purge` 用：删掉 `live/` 下本插件写出的全部常规文件。"""
    removed = 0
    for ent in _iter_regular_files(home):
        try:
            os.unlink(ent.path)
            removed += 1
        except OSError:
            continue
    return removed
