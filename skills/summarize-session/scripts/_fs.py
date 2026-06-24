"""文件系统工具: git mv + fallback rename + 跨平台文件锁。"""

import os
import subprocess
import time
import json
import tempfile
from pathlib import Path


def git_mv_or_rename(vault: Path, src_rel: str, dst_rel: str) -> None:
    """优先 git mv(保留 rename 历史),失败退回 Path.rename。

    - git mv 成功:直接 return
    - git mv 失败(rc != 0):尝试 Path.rename(rename 失败抛 OSError,不再二次包装)
    - 目标父目录不存在时自动创建
    """
    result = subprocess.run(
        ['git', 'mv', src_rel, dst_rel],
        cwd=str(vault), capture_output=True, text=True,
    )
    if result.returncode == 0:
        return
    src_abs = vault / src_rel
    dst_abs = vault / dst_rel
    dst_abs.parent.mkdir(parents=True, exist_ok=True)
    src_abs.rename(dst_abs)


# ========== 跨平台文件锁 ==========
#
# 由 rebuild_index.py:25-56 提取到此公共模块；同步加固：
# 1) LOCK_TIMEOUT 30s → 300s 兜底 backfill 等长操作
# 2) stale 锁强删前先 PID 探活，活进程的锁永不删
# 3) 新增 _refresh_lock 供长操作持锁者主动刷新 mtime

# 文件锁兜底超时（秒）—— 300s = 5min 覆盖 backfill 100+ 条 + Vault 写场景
LOCK_TIMEOUT = 300

# 持锁者刷新 mtime 的建议间隔（秒）—— 必须远小于 LOCK_TIMEOUT
_LOCK_REFRESH_INTERVAL = 5


def _pid_alive(pid: int) -> bool:
    """跨平台探测 PID 是否存活。

    - Linux/macOS: os.kill(pid, 0) 不抛异常即存活
    - Windows: OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) 成功即存活；
      返回 NULL 时若 GetLastError != 87(ERROR_INVALID_PARAMETER) 视为存活（保守）
    - PID 非正数视为不存活
    - 任何无法判定的异常按"存活"处理（避免误删活进程锁）
    """
    if pid <= 0:
        return False
    try:
        if os.name == 'nt':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                # 直接调 kernel32.GetLastError()；ctypes.get_last_error 仅对 use_last_error=True 的 WinDLL 有效
                # 87 = ERROR_INVALID_PARAMETER（PID 不存在）；其它错（如权限不足）保守按存活处理
                err = kernel32.GetLastError()
                return err != 87
            kernel32.CloseHandle(handle)
            return True
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        # 任何意料外异常按存活处理（保守，不误删）
        return True


def _read_lock_pid(lock_path: str) -> int:
    """读 lock 文件首行的 PID；读不出或非数字返回 0。"""
    try:
        with open(lock_path, 'r', encoding='utf-8') as f:
            line = f.readline().strip()
            return int(line) if line.isdigit() else 0
    except (OSError, ValueError):
        return 0


def _acquire_lock(lock_path: str, timeout: int = LOCK_TIMEOUT) -> bool:
    """获取文件锁（跨平台），返回是否成功。

    - 用 O_CREAT|O_EXCL 原子创建
    - lock 文件首行写 PID，第二行写时间戳，便于 stale 判定
    - 已存在的锁：检查 mtime 是否超 timeout（视为 stale 候选）
      若 stale 且锁主 PID 不存活 → 强删后重试
      若 stale 但锁主 PID 仍存活 → 不删，继续等待（活进程长操作保护）
    - 等待期间每 0.2s 重试一次，直到 timeout
    """
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n{time.time()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            # 检查锁是否 stale；stale 且锁主已死才强删
            try:
                lock_mtime = os.path.getmtime(lock_path)
                if time.time() - lock_mtime > timeout:
                    pid = _read_lock_pid(lock_path)
                    if pid and _pid_alive(pid):
                        # 活进程的锁不强删（长操作通过 _refresh_lock 续期失败也可能 mtime 过期）
                        pass
                    else:
                        try:
                            os.remove(lock_path)
                            continue
                        except OSError:
                            pass
            except OSError:
                # 文件读 mtime 失败（可能刚被别的进程删了）→ 走重试分支
                pass
            if time.time() >= deadline:
                return False
            time.sleep(0.2)


def _refresh_lock(lock_path: str) -> None:
    """长操作中持锁者主动调用，刷新 mtime 防止被 stale 判定误删。

    建议每 _LOCK_REFRESH_INTERVAL 秒调用一次。
    锁文件已不存在或权限不足时静默忽略（不影响业务流程）。
    """
    try:
        os.utime(lock_path, None)
    except OSError:
        pass


def _release_lock(lock_path: str) -> None:
    """释放文件锁（删 lock 文件）。文件已不存在时静默忽略。"""
    try:
        os.remove(lock_path)
    except OSError:
        pass


def _replace_with_retry(tmp, path, retries=5, backoff=0.3):
    """os.replace 遇 PermissionError（WinError 5/32，Obsidian/OCular 短暂占用）指数退避重试。
    最终失败：清理 tmp 再 raise。"""
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == retries - 1:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(backoff * (2 ** attempt))


def atomic_write_text(path, text, retries=5, backoff=0.3):
    """文本原子写入：tempfile + os.replace（带占用重试）。crash-safe + Windows 占用健壮。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.atomic_', suffix='.tmp', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _replace_with_retry(tmp, str(path), retries, backoff)


def atomic_write_json(path: str, data, retries=5, backoff=0.3) -> None:
    """JSON 原子写入：tempfile + os.replace（带占用重试）。供 sync / prune / reclaim 复用。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.pending_', suffix='.tmp', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _replace_with_retry(tmp, str(path), retries, backoff)
