"""Small cross-platform atomic JSON update primitives with a lease lock."""
from __future__ import annotations

import errno
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


def _owner_alive(lock: Path) -> bool:
    try:
        raw = lock.read_text(encoding="ascii", errors="replace").split()
        pid = int(raw[0])
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        # ⚠️ Windows 的 `os.kill(pid, 0)` 对**不存在的 PID** 抛的是
        # `OSError(errno=EINVAL, winerror=87)`，不是 `ProcessLookupError`
        # （本机实测：CPython 3.14.7 / Win11，未使用的高位 PID 与已退出且句柄
        # 已释放的进程都是这个形态）。旧代码把它归进下面的「歧义」分支恒判活，
        # 于是 `lease_lock` 的陈旧锁接管在 Windows 上是**死代码**：任何被硬杀的
        # hook（宿主超时、Ctrl-C、断电、OOM）留下的 `.lock` 会永久卡死该目标，
        # 此后每次 `update_json` 空转到 timeout 再抛——注入去重与事件幂等全部
        # 静默失效，且每轮固定多付 timeout 秒（实测 0.175s → 2.15s）。
        if os.name == "nt" and (getattr(exc, "winerror", None) == 87
                                or exc.errno == errno.EINVAL):
            return False
        # 其余情形（POSIX 上他用户进程的 PermissionError 等）语义确实是
        # 「无法判定」，保守判活——多等一会儿好过放进第二个写者。
        return True
    except (ValueError, IndexError):
        # 含 UnicodeDecodeError（ValueError 子类）：lock 内容不可解析时同样保守判活。
        return True


@contextmanager
def lease_lock(target: Path, *, timeout: float = 2.0,
               stale_after: float = 30.0,
               hard_stale_after: float = 24 * 3600.0) -> Iterator[None]:
    lock = target.with_name(target.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    token = f"{os.getpid()} {time.time()} {secrets.token_hex(12)}"
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, token.encode("ascii"))
            finally:
                os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                if age > stale_after and not _owner_alive(lock):
                    lock.unlink(missing_ok=True)
                    continue
                if age > hard_stale_after:
                    # 无论存活探测说什么都强制接管。探测在若干场景下会恒判「活着」
                    # ——PID 复用、跨用户的 PermissionError、未预料到的 OSError
                    # 形态——那时锁就再没有出路了。一天前的租约不可能还在服务一个
                    # 秒级操作；给这道守卫留一条与探测结果无关的自愈路径。
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"lock timeout: {lock}")
            time.sleep(0.01)
    try:
        yield
    finally:
        try:
            # A stale-lock takeover may have replaced ownership while this
            # process was still alive. Never unlink another owner's lease.
            if lock.read_text(encoding="ascii", errors="replace") == token:
                lock.unlink(missing_ok=True)
        # 含 ValueError（UnicodeDecodeError 是其子类）：lock 内容不可解码时
        # 不得让异常从 finally 逃逸——那会掩盖 with 块里的原始异常。
        except (OSError, ValueError):
            pass


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (2 ** attempt))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def update_json(path: Path, mutate: Callable[[dict], dict], *,
                max_bytes: int | None = None) -> dict:
    """读-改-写一份 JSON，全程持锁。

    ⚠️ **超过 `max_bytes` 时 `current` 置空，`mutate({})` 的结果会整体覆盖原文件**
    ——包括本次调用不认识的键。这是**有意的**：调用方（`_state`）的语义就是
    「超出上限即视为损坏并重置」，且写端已在 90KB 提前裁剪、100KB 才判损坏，
    正常路径够不着。但代价要写明：真踩到时 `diag_ts` / `fallback_ts` 这类旁路
    字段会一起没掉，表现为诊断冷却被重置（多打一次提示），不是数据损坏。
    刻意不改成「超限也读入再让 mutate 裁剪」——那会把一个无界大小的文件读进内存，
    而这条路径本就是为「文件已经不正常」准备的。
    """
    with lease_lock(path):
        current: dict = {}
        try:
            if path.exists() and (max_bytes is None or path.stat().st_size <= max_bytes):
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = loaded
        except (OSError, ValueError, json.JSONDecodeError):
            current = {}
        updated = mutate(dict(current))
        atomic_write_json(path, updated)
        return updated
