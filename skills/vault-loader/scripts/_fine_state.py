# -*- coding: utf-8 -*-
"""per-session 精排状态（spec 2026-10-13 §7）：逐轮补充需要跨轮次记住的东西。

落点按**会话**（`<sid_h>.json`，`sid_h = sha256(session_id)[:24]`，与 mod 同口径），不按 cwd、
也不按项目根：这里记的是「本会话第几轮、哪篇已注入过、补充队列、会话设置、失败计数」，全是
per-session 语义；按 cwd 切会让同一会话 `cd` 进子目录就丢掉补充队列（topics 在 2026-09-10 踩过的同一个坑）。
`_state.configure_context` 的 docstring 警告过「按 session 切会单调增长」——这里由 SessionStart
的 `cleanup_fine`（超过 `state_ttl_hours` 删除）与 `analyze_metrics --purge` 兜住（F57）。

**不存 prompt、K 轮文本、目录**（F16）：`to_json` 的键集合由守卫钉住。

热路径（spec §7）：UPS 每轮至多一次读、一次条件写；条件写在 `lease_lock(timeout=0.25 s)` 内
读-改-写，锁超时返回 None、调用方按 `degraded=lock` 降级（F52）。在飞标记与机器槽位是同目录下的
独立文件，`O_EXCL` 创建，不经这把锁。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import _state                      # 先导入 _state：它负责把插件根放进 sys.path
# 不从 `_session_ops` 取：那是 UPS 里单独兜底的快照模块，它导入失败不得连带本模块（2.4.0 full-review A-H1）
from ._state import is_link_like as _is_link_like

try:
    from context_vault.atomic import atomic_write_json, lease_lock
except ImportError:  # legacy 独立布局：退化为无锁写，与 _metrics / _annotations 的 façade 同理
    from contextlib import contextmanager

    @contextmanager
    def lease_lock(target, *, timeout=2.0, stale_after=30.0, hard_stale_after=24 * 3600.0):
        yield

    def atomic_write_json(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

FINE_VERSION = 1
MAX_INJECTED = 300
TURN_STARTED_KEEP = 3
MAX_FINE_SLOTS = 4                 # 【初值】机器级并发上限
STATS_Q_MAX = 5
STREAK_LIMIT = 2
DISABLE_COOLDOWN_SEC = 1800        # 【初值】streak 停用满 30 分钟可恢复（F66）
LOCK_TIMEOUT_SEC = 0.25
DISABLED_REASONS = ("streak", "no_claude", "call_cap", "bytes_cap")
# 与 `_topic.PENDING_GRACE_SEC` 同值（parity 用例钉住）。不 import `_topic`：UPS 与子进程都会导入本模块，
# `_topic` 带着子进程管理与模板，没必要为一个常量把它拉进来
PENDING_GRACE_SEC = 30
MAX_FINE_BYTES = 256 * 1024        # 超过即视为损坏、按默认状态处理（与 `_state.MAX_STATE_BYTES` 同一思路）
# 标记内容读不出来（写到一半被杀、被手工改坏）时按 `fine_timeout_sec` 的上限算过期，保证它终究会被回收
DEFAULT_MARKER_TIMEOUT = 300
_START_SKEW_SEC = 300              # 标记里的 started 超前文件 mtime 这么多以上 ⇒ 不可信，改用 mtime
_MAX_SETTINGS_KEYS = 10
_MAX_SETTING_STR = 64
_MAX_PATH_CHARS = 512
_MAX_SUPP_PATHS = 8                # = fine_max_notes 的硬上限
_MAX_MARKER_BYTES = 4096
_TMP_TTL_SEC = 3600
STATS_KEYS = ("gen_id", "mode", "ok", "fail", "none", "n_sel", "n_echo_mismatch",
              "n_catalog", "n_catalog_dropped", "n_summary_layer", "bytes", "degraded")
# stats_q 里 `degraded`（子进程的降级码列表）的上限：码表现有 5 个，每个都是短 ASCII 枚举
_MAX_DEGRADED = 8
_MAX_DEGRADED_CHARS = 32

# 本模块只写这几种文件名；清理与 purge 只认它们，目录里混进别的文件也删不到（照 `_session_ops` 的先例）
_JSON_NAME = re.compile(r"[0-9a-f]{24}\.json")
_INFLIGHT_NAME = re.compile(r"[0-9a-f]{24}\.inflight")
_LOCK_NAME = re.compile(r"[0-9a-f]{24}\.json\.lock")
_TMP_NAME = re.compile(r"\.[0-9a-f]{24}\.json\.\d+\.[0-9a-f]{8}\.tmp")
_SLOT_NAME = re.compile(r"slot-\d{1,2}\.json")
_SEQ_KEY = re.compile(r"[0-9]{1,9}")

_WARNED: set = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        print(message, file=sys.stderr)


# ---------- 落点 ----------

def sid_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


def fine_dir() -> Path:
    """按**调用时**生效的命名空间取目录（与 `_state.session_topics_path` 同一判定）。"""
    rt = _state.current_runtime()
    if rt != "legacy":
        return _state.context_home() / "state" / rt / "fine"
    return Path.home() / ".claude" / "vault-loader-fine"


def _valid_sid(session_id) -> bool:
    return isinstance(session_id, str) and bool(session_id)


def fine_path(session_id: str) -> "Path | None":
    return fine_dir() / f"{sid_hash(session_id)}.json" if _valid_sid(session_id) else None


def inflight_path(session_id: str) -> "Path | None":
    return fine_dir() / f"{sid_hash(session_id)}.inflight" if _valid_sid(session_id) else None


def slots_dir() -> Path:
    return fine_dir() / "slots"


# ---------- 状态与校验 ----------

def _int(v) -> "int | None":
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _num(v) -> "float | None":
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        if not math.isfinite(v):
            return None
        return float(v)
    except OverflowError:   # 超大整数（10**400）转不成 float：只让该字段回落，不能把异常带出去
        return None


def _short_str(v, n: int) -> "str | None":
    return v if isinstance(v, str) and 0 < len(v) <= n else None


def _clean_paths(v, cap: int, *, tail: bool) -> list:
    """str、非空、不超长、去重保序；`tail` 时留最近的 cap 条，否则留最前的 cap 条。"""
    if not isinstance(v, list):
        return []
    out: list = []
    seen: set = set()
    for p in v:
        if isinstance(p, str) and 0 < len(p) <= _MAX_PATH_CHARS and p not in seen:
            seen.add(p)
            out.append(p)
    return out[-cap:] if tail else out[:cap]


def _clean_supp(v) -> "dict | None":
    if not isinstance(v, dict):
        return None
    gen = _short_str(v.get("gen_id"), 64)
    seq = _int(v.get("src_seq"))
    ts = _num(v.get("ts"))
    if gen is None or seq is None or ts is None or not isinstance(v.get("paths"), list):
        return None
    return {"gen_id": gen, "src_seq": seq,
            "paths": _clean_paths(v.get("paths"), _MAX_SUPP_PATHS, tail=False), "ts": ts}


def _clean_settings(v) -> dict:
    if not isinstance(v, dict):
        return {}
    out: dict = {}
    for k, x in list(v.items())[:_MAX_SETTINGS_KEYS]:
        if not isinstance(k, str):
            continue
        if isinstance(x, float) and not math.isfinite(x):
            continue
        if isinstance(x, str) and len(x) > _MAX_SETTING_STR:
            continue
        if isinstance(x, (bool, int, float, str)):
            out[k] = x
    return out


def _clean_stats(v) -> list:
    """子进程统计：只留 `STATS_KEYS`。标量键留 bool、非负 int、≤ 64 字的 str 与显式的 null（成功那条的
    `fail` 是 None，键要留着——每条键恒定）；`degraded` 只留非空、≤ 32 字的字符串，至多 8 个，不是列表时
    不留这个键。没有 gen_id 的整条丢弃。"""
    if not isinstance(v, list):
        return []
    out: list = []
    for item in v:
        if not isinstance(item, dict) or _short_str(item.get("gen_id"), 64) is None:
            continue
        rec: dict = {}
        for k in STATS_KEYS:
            if k not in item:
                continue
            x = item[k]
            if k == "degraded":
                if isinstance(x, list):
                    rec[k] = [s for s in x if isinstance(s, str)
                              and 0 < len(s) <= _MAX_DEGRADED_CHARS][:_MAX_DEGRADED]
            elif x is None or isinstance(x, bool) or (isinstance(x, int) and x >= 0) \
                    or (isinstance(x, str) and len(x) <= 64):
                rec[k] = x
        out.append(rec)
    return out[-STATS_Q_MAX:]


@dataclass
class FineState:
    turn_seq: int = 0
    turn_started: dict = field(default_factory=dict)
    supp: "dict | None" = None
    injected: list = field(default_factory=list)
    session_settings: dict = field(default_factory=dict)
    fail_streak: int = 0
    disabled_reason: "str | None" = None
    disabled_at: "float | None" = None
    calls: int = 0
    bytes_sent: int = 0
    last_fail: "str | None" = None
    last_ts: "float | None" = None
    stats_q: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {"v": FINE_VERSION, "turn_seq": self.turn_seq, "turn_started": dict(self.turn_started),
                "supp": self.supp, "injected": list(self.injected),
                "session_settings": dict(self.session_settings), "fail_streak": self.fail_streak,
                "disabled_reason": self.disabled_reason, "disabled_at": self.disabled_at,
                "calls": self.calls, "bytes_sent": self.bytes_sent, "last_fail": self.last_fail,
                "last_ts": self.last_ts, "stats_q": list(self.stats_q)}

    @classmethod
    def from_json(cls, raw) -> "FineState":
        """逐字段类型校验，坏字段回落默认；整份不是 dict 或校验中途出错 → 默认状态。永不抛。"""
        st = cls()
        if not isinstance(raw, dict):
            return st
        try:
            st.turn_seq = _int(raw.get("turn_seq")) or 0
            started = raw.get("turn_started")
            if isinstance(started, dict):
                good = {int(k): _num(v) for k, v in started.items()
                        if isinstance(k, str) and _SEQ_KEY.fullmatch(k) and _num(v) is not None}
                st.turn_started = {str(k): good[k] for k in sorted(good)[-TURN_STARTED_KEEP:]}
            st.supp = _clean_supp(raw.get("supp"))
            st.injected = _clean_paths(raw.get("injected"), MAX_INJECTED, tail=True)
            st.session_settings = _clean_settings(raw.get("session_settings"))
            st.fail_streak = _int(raw.get("fail_streak")) or 0
            reason = raw.get("disabled_reason")
            st.disabled_reason = reason if reason in DISABLED_REASONS else None
            st.disabled_at = _num(raw.get("disabled_at")) if st.disabled_reason else None
            st.calls = _int(raw.get("calls")) or 0
            st.bytes_sent = _int(raw.get("bytes_sent")) or 0
            st.last_fail = _short_str(raw.get("last_fail"), 32)
            st.last_ts = _num(raw.get("last_ts"))
            st.stats_q = _clean_stats(raw.get("stats_q"))
        except Exception:                          # noqa: BLE001 — 坏数据只回落默认，绝不抛
            return cls()
        return st


def _read(p: Path) -> FineState:
    try:
        st = os.lstat(p)
    except OSError:
        return FineState()
    try:
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FINE_BYTES:
            return FineState()
        return FineState.from_json(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError) as exc:   # 深层嵌套的 `[` 抛 RecursionError，不是 ValueError
        _warn_once(f"read:{p}",f"[vault-loader] 精排状态读取失败，按空状态处理：{exc}")
        return FineState()


def load_fine(session_id: str) -> FineState:
    p = fine_path(session_id)
    return _read(p) if p is not None else FineState()


def update_fine(session_id: str, mutate) -> "FineState | None":
    """锁内读-改-写。`mutate` 返回 None 表示不写（返回读到的状态）；失败一律返回 None、不写。

    写盘前经 `from_json(to_json())` 归一：各上限（injected 300、turn_started 3、stats_q 5）只在这一处施加，
    调用方可以放心地整列表追加。
    """
    p = fine_path(session_id)
    if p is None:
        return None
    try:
        if _is_link_like(p.parent):
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        with lease_lock(p, timeout=LOCK_TIMEOUT_SEC):
            cur = _read(p)
            new = mutate(cur)
            if new is None:
                return cur
            clean = FineState.from_json(new.to_json())
            atomic_write_json(p, clean.to_json())
            return clean
    except Exception as exc:                       # noqa: BLE001 — fail-open
        _warn_once(f"update:{exc.__class__.__name__}", f"[vault-loader] 精排状态写入失败：{exc}")
        return None  # 锁超时 / IO 失败：本轮当作没写成


# ---------- 在飞标记与槽位 ----------

def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _marker_write(p: Path, payload: dict) -> bool:
    """O_EXCL 创建。`FileExistsError`（以及 Windows delete-pending 时的 `PermissionError`）交给调用方。"""
    data = json.dumps(payload).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(p, flags, 0o600)
    try:
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except BaseException:
        _unlink_quiet(p)   # 写失败（磁盘满等）：别留一个空标记挡住后续 claim 到它过期
        raise
    return True


def _marker_payload(gen_id: str, timeout_sec, now) -> "dict | None":
    """标记内容；`timeout_sec` 转不成整数（None / inf / "x"）→ None，调用方按失败处理（fail-open）。"""
    try:
        return {"gen_id": gen_id, "started": now, "timeout": int(timeout_sec)}
    except (TypeError, ValueError, OverflowError):
        return None


def _marker_read(p: "Path | None") -> "dict | None":
    """标记内容；文件不存在 → None。读不出内容时按 mtime + `DEFAULT_MARKER_TIMEOUT` 合成一份（`corrupt`）。"""
    if p is None:
        return None
    try:
        st = os.lstat(p)
    except OSError:
        return None
    raw = None
    if stat.S_ISREG(st.st_mode) and st.st_size <= _MAX_MARKER_BYTES:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            raw = None
    gen = raw.get("gen_id") if isinstance(raw, dict) else None
    started = _num(raw.get("started")) if isinstance(raw, dict) else None
    timeout = _int(raw.get("timeout")) if isinstance(raw, dict) else None
    if not isinstance(gen, str) or started is None or timeout is None:
        return {"gen_id": "", "started": st.st_mtime, "timeout": DEFAULT_MARKER_TIMEOUT, "corrupt": True}
    if started > st.st_mtime + _START_SKEW_SEC:
        started = st.st_mtime
    return {"gen_id": gen, "started": started, "timeout": min(timeout, DEFAULT_MARKER_TIMEOUT)}


def _expired(m: dict, now: float) -> bool:
    return now > m["started"] + m["timeout"] + PENDING_GRACE_SEC


def inflight_active(session_id: str, now: float) -> "dict | None":
    m = _marker_read(inflight_path(session_id))
    return m if m is not None and not _expired(m, now) else None


def inflight_expired(session_id: str, now: float) -> "dict | None":
    m = _marker_read(inflight_path(session_id))
    return m if m is not None and _expired(m, now) else None


def claim_inflight(session_id: str, gen_id: str, timeout_sec: int, now: float) -> bool:
    """本会话至多一个在飞子进程（Q6）。已过期的标记先删再试一次；其它失败一律不发起（F04）。"""
    p = inflight_path(session_id)
    if p is None or not isinstance(gen_id, str) or not gen_id:
        return False
    payload = _marker_payload(gen_id, timeout_sec, now)
    if payload is None:
        return False
    try:
        if _is_link_like(p.parent):
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    for _ in range(2):
        try:
            return _marker_write(p, payload)
        except FileExistsError:
            m = _marker_read(p)
            if m is None or not _expired(m, now):
                return False
            _unlink_quiet(p)   # 回收过期在飞标记：子进程被杀或机器重启后留下的
        except OSError:
            return False
    return False


def release_inflight(session_id: str, gen_id: str) -> None:
    p = inflight_path(session_id)
    m = _marker_read(p)
    if m is not None and gen_id and m["gen_id"] == gen_id:
        _unlink_quiet(p)


def release_expired_inflight(session_id: str, now: float) -> bool:
    """重新读标记，只在它此刻（`now`）仍然过期时才删；返回是否删了。

    与 `release_inflight` 的区别：不认 gen_id。读不出内容的损坏标记 gen_id 是合成的空串，按 gen 永远对不上；
    而「还过期」这个条件对损坏标记同样成立。未过期的标记（别的子进程刚发起的）一律不动。"""
    p = inflight_path(session_id)
    m = _marker_read(p)
    if m is None or not _expired(m, now):
        return False
    _unlink_quiet(p)
    return True


def claim_slot(gen_id: str, timeout_sec: int, now: float) -> "str | None":
    """机器级并发：`slots/slot-<i>.json` 至多 `MAX_FINE_SLOTS` 个；过期的可回收。返回槽位文件名。"""
    if not isinstance(gen_id, str) or not gen_id:
        return None
    payload = _marker_payload(gen_id, timeout_sec, now)
    if payload is None:
        return None
    d = slots_dir()
    try:
        if _is_link_like(d.parent) or _is_link_like(d):
            return None
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    for i in range(MAX_FINE_SLOTS):
        name = f"slot-{i}.json"
        p = d / name
        for _ in range(2):
            try:
                _marker_write(p, payload)
                return name
            except FileExistsError:
                m = _marker_read(p)
                if m is not None and _expired(m, now):
                    _unlink_quiet(p)   # 回收过期槽位
                    continue
                break
            except OSError:
                break
    return None


def release_slot(slot: "str | None", gen_id: str) -> None:
    if not isinstance(slot, str) or not _SLOT_NAME.fullmatch(slot):
        return
    p = slots_dir() / slot
    m = _marker_read(p)
    if m is not None and gen_id and m["gen_id"] == gen_id:
        _unlink_quiet(p)


# ---------- 清理 ----------

def _own_regular_files(d: Path, patterns):
    """`d` 下本模块写出的常规文件；目录本身是链接或 reparse point 时一个都不给。"""
    if _is_link_like(d) or not d.is_dir():
        return
    with os.scandir(d) as it:
        for ent in it:
            if not any(p.fullmatch(ent.name) for p in patterns):
                continue
            try:
                if ent.is_symlink() or not ent.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            yield ent


def cleanup_fine(ttl_hours: float, now: "float | None" = None) -> int:
    """SessionStart 调：超过 TTL 的状态文件与锁、超过 1 小时的临时文件、已过期的在飞标记与槽位。"""
    now = time.time() if now is None else now
    try:
        ttl = float(ttl_hours) * 3600
    except (TypeError, ValueError):
        ttl = float("inf")
    ttl_ok = math.isfinite(ttl) and ttl > 0
    removed = 0
    d = fine_dir()
    for ent in _own_regular_files(d, (_JSON_NAME, _LOCK_NAME, _TMP_NAME, _INFLIGHT_NAME)):
        p = Path(ent.path)
        try:
            age = now - ent.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if _INFLIGHT_NAME.fullmatch(ent.name):
            m = _marker_read(p)
            gone = m is not None and _expired(m, now)
        elif _TMP_NAME.fullmatch(ent.name):
            gone = age > _TMP_TTL_SEC
        else:
            gone = ttl_ok and age > ttl
        if gone:
            _unlink_quiet(p)
            removed += 1
    for ent in _own_regular_files(d / "slots", (_SLOT_NAME,)):
        p = Path(ent.path)
        m = _marker_read(p)
        if m is not None and _expired(m, now):
            _unlink_quiet(p)
            removed += 1
    return removed


def purge_fine(home: "Path | None" = None) -> int:
    """`analyze_metrics --purge` 用：删掉两个命名空间下本模块写出的全部文件，空目录一并删除。

    canonical 只有 `claude` 一个命名空间（2.0.0 起白名单只剩它），legacy 在 `~/.claude/vault-loader-fine`。
    """
    base = home or Path.home()
    removed = 0
    for d in (_state.context_home(base) / "state" / "claude" / "fine",
              base / ".claude" / "vault-loader-fine"):
        for sub, pats in ((d / "slots", (_SLOT_NAME,)),
                          (d, (_JSON_NAME, _LOCK_NAME, _TMP_NAME, _INFLIGHT_NAME))):
            for ent in list(_own_regular_files(sub, pats)):
                try:
                    os.unlink(ent.path)
                    removed += 1
                except OSError:
                    continue
        for sub in (d / "slots", d):
            try:
                if not _is_link_like(sub):
                    sub.rmdir()                    # 只删空目录：混进别的文件时保留
            except OSError:
                pass
    return removed
