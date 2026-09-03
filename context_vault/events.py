"""Cross-process idempotency markers for hook events."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from .atomic import atomic_write_json, lease_lock
from .paths import context_home

_MARKER_RETENTION_SECONDS = 90 * 86400


def _event_salt(home: Path | None = None) -> bytes:
    """本机事件盐。与 metrics 的 `.salt` 同源思路，但**独立文件**——events 目录
    无条件写入，而 metrics 是 opt-in，不能让前者去创建后者的目录。

    `O_CREAT|O_EXCL` 原子创建防 TOCTOU；`O_BINARY` 避免 Windows 文本模式把 0x0A
    改写成 0x0D0A 致盐值静默错位（metrics 侧踩过这个坑，实测触发率约 12%）。
    任何失败回落**进程内**随机盐：那会让本轮去重失效（等价于「没去重」），
    但绝不会把内容派生值写进磁盘。
    """
    d = context_home(home) / "state" / "events"
    p = d / ".salt"
    try:
        d.mkdir(parents=True, exist_ok=True)
        if p.exists():
            raw = p.read_bytes()
            if len(raw) >= 16:
                return raw
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                     | getattr(os, "O_BINARY", 0), 0o600)
        try:
            raw = secrets.token_bytes(32)
            os.write(fd, raw)
        finally:
            os.close(fd)
        return raw
    except FileExistsError:
        try:
            raw = p.read_bytes()
            if len(raw) >= 16:
                return raw
        except OSError:
            pass
    except OSError:
        pass
    return secrets.token_bytes(32)


def claim_event(runtime: str, event_name: str, session_id: str,
                 event_id: str, *, scope: str = "",
                 ttl_seconds: float | None = None,
                 home: Path | None = None,
                 stable: bool = True) -> bool:
    """Claim one normalized hook event with O_EXCL.

    Returning False means an equivalent invocation already started, so callers
    should silently exit.

    ⚠️ **`stable=False` 时 `event_id` 是内容派生的**：`HookContext` 在宿主不下发
    `prompt_id`/`turn_id` 时回落成 `sha256(runtime, event, session_id, source,
    prompt)` 的截断值——材料里含 **prompt 原文**，且**未加盐**。marker 保留 90 天，
    且不受 metrics 的 opt-in 约束。材料中除 prompt 外全部本地可得（session_id 就以
    明文目录名存在于 state 路径里），足以做离线确认攻击（「用户是否问过 X」）——
    正是本项目对关键词 hash 坚持加盐所要防的同一类攻击。

    故此处两道处理：落盘的 `event_id` 字段在不稳定时置空（marker 不需要原值，
    去重只认 `id`），`id` 本身用本机盐参与摘要。此前的 docstring 写着
    "Markers intentionally contain no prompt or cwd"，那句话对这条分支不成立。
    """
    if not event_id:
        return True
    material = "\0".join((runtime, event_name, session_id, event_id, scope))
    if not stable:
        material = _event_salt(home).hex() + "\0" + material
    digest = hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()
    session_hash = hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:24]
    marker = context_home(home) / "state" / "events" / runtime / f"{session_hash}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    # Amortized cleanup keeps one-file-per-session markers bounded without a
    # directory scan on every prompt.
    if digest.startswith("00"):
        cutoff = time.time() - _MARKER_RETENTION_SECONDS
        for old in marker.parent.glob("*.json"):
            try:
                if old != marker and old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    with lease_lock(marker):
        claims = []
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("claims"), list):
                claims = [item for item in data["claims"] if isinstance(item, dict)]
        except (OSError, ValueError, json.JSONDecodeError):
            claims = []
        now = time.time()
        if any(
            item.get("id") == digest
            and (
                ttl_seconds is None
                or now - float(item.get("claimed_at", now)) < ttl_seconds
            )
            for item in claims
        ):
            return False
        if ttl_seconds is not None:
            claims = [item for item in claims if item.get("id") != digest]
        claims.append({
            "id": digest,
            "event": event_name,
            # 不稳定 id 是内容派生（含 prompt 原文）的，绝不落盘明文——
            # 去重只认上面的 `id`，这个字段纯属可读性。
            "event_id": event_id if stable else "",
            "claimed_at": round(now, 3),
        })
        # A session only needs a bounded recent-event window. This prevents a
        # long-running agent from creating one file per prompt forever.
        claims = claims[-256:]
        atomic_write_json(marker, {
            "schema": 1,
            "runtime": runtime,
            "session_hash": session_hash,
            "claims": claims,
        })
        return True
