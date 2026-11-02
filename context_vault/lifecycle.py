"""Codex 上下文代际栅栏：模型在锁外运行，结果提交与压缩重置互斥。"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat

from .atomic import lease_lock
from .paths import storage_root


def _path(home, thread_id):
    if not isinstance(thread_id, str) or not 0 < len(thread_id) <= 128:
        raise ValueError("invalid lifecycle identity")
    base = storage_root(home, host="codex", kind="state") / "lifecycle"
    for path in (home / ".context-vault", home / ".context-vault/state", base.parent.parent,
                 base.parent, base):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("linked lifecycle directory")
    return base / (hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:24] + ".json")


def _read(path):
    if not path.exists():
        return {"v": 1, "epoch": 0, "start_seq": 0}
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise ValueError("invalid lifecycle state")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("v") != 1 or type(data.get("epoch")) is not int
            or not 0 <= data["epoch"] < 2**31):
        raise ValueError("invalid lifecycle generation")
    return data


def current_epoch(*, home: Path, thread_id: str) -> int:
    return _read(_path(home, thread_id))["epoch"]


def known_thread(*, home: Path, thread_id: str) -> bool:
    return _path(home, thread_id).exists()


@contextmanager
def locked_thread(*, home: Path, thread_id: str):
    path = _path(home, thread_id)
    with lease_lock(path):
        yield path, _read(path)


@contextmanager
def result_guard(payload, *, home: Path):
    if "codex_context" not in payload:
        yield True
        return
    context = payload.get("codex_context")
    if not isinstance(context, dict) or type(context.get("epoch")) is not int:
        yield False
        return
    stack = ExitStack()
    state = None
    try:
        path = _path(home, context.get("thread_id"))
        if path.exists():
            _, state = stack.enter_context(locked_thread(home=home, thread_id=context["thread_id"]))
    except (OSError, ValueError):
        stack.close()
    if state is None:
        yield False
        return
    with stack:
        yield state["epoch"] == context["epoch"]
