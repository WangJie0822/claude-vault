"""auto-queue.jsonl 的并发安全读写。"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

# fcntl 仅在 *nix 可用；Windows 上无该模块。SessionEnd hook 单进程入队，
# 并发冲突几率极低，Windows 平台降级为无锁实现。
try:
    import fcntl as _fcntl  # type: ignore
    _HAS_FLOCK = True
except ImportError:
    _fcntl = None  # type: ignore
    _HAS_FLOCK = False


@dataclass
class QueueEntry:
    session_id: str
    cwd: str
    enqueued_at: str
    metrics: dict[str, Any]
    failure_count: int = 0


@contextmanager
def _flock(path: Path, mode: str):
    """对队列文件加排他锁的上下文管理器(自动创建父目录)。

    *nix 用 fcntl.flock 排他锁；Windows 无 fcntl，降级为无锁（SessionEnd
    hook 单进程入队，实际并发几率极低）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, mode)
    try:
        if _HAS_FLOCK:
            _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
        yield f
    finally:
        try:
            if _HAS_FLOCK:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
        finally:
            f.close()


def enqueue(queue_path: Path, entry: QueueEntry) -> None:
    """append 一条记录到 jsonl。多进程/多线程安全。"""
    line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
    with _flock(queue_path, "a") as f:
        f.write(line)


def list_queue(queue_path: Path) -> list[QueueEntry]:
    """读取所有条目,缺失/空文件返回 []。"""
    if not queue_path.exists():
        return []
    text = queue_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    out = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            out.append(QueueEntry(
                session_id=data["session_id"],
                cwd=data["cwd"],
                enqueued_at=data["enqueued_at"],
                metrics=data.get("metrics", {}),
                failure_count=int(data.get("failure_count", 0)),
            ))
        except (json.JSONDecodeError, KeyError):
            # 损坏行跳过
            continue
    return out


def _atomic_rewrite(queue_path: Path, entries: list[QueueEntry]) -> None:
    """原子重写整个队列(临时文件 + rename)。调用方必须已持锁。"""
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=queue_path.name + ".", suffix=".tmp", dir=queue_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
        os.replace(tmp_path, queue_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def dequeue_by_session(queue_path: Path, session_ids: Iterable[str]) -> None:
    """从队列移除指定 session_id 的所有条目。"""
    drop = set(session_ids)
    with _flock(queue_path, "a+") as f:
        f.seek(0)
        text = f.read()
    entries = []
    for line in text.strip().split("\n") if text.strip() else []:
        try:
            data = json.loads(line)
            if data["session_id"] in drop:
                continue
            entries.append(QueueEntry(
                session_id=data["session_id"], cwd=data["cwd"],
                enqueued_at=data["enqueued_at"],
                metrics=data.get("metrics", {}),
                failure_count=int(data.get("failure_count", 0)),
            ))
        except (json.JSONDecodeError, KeyError):
            continue
    with _flock(queue_path, "a") as f:
        # 持锁后做原子重写
        _atomic_rewrite(queue_path, entries)


def increment_failure(queue_path: Path, session_id: str) -> int:
    """对指定会话的 failure_count 加 1,返回新值;不存在返回 -1。"""
    new_value = -1
    with _flock(queue_path, "a") as f:
        entries = list_queue(queue_path)
        for e in entries:
            if e.session_id == session_id:
                e.failure_count += 1
                new_value = e.failure_count
        _atomic_rewrite(queue_path, entries)
    return new_value
