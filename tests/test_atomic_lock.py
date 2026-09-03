from __future__ import annotations

import pytest

from context_vault.atomic import lease_lock


def test_old_owner_does_not_remove_replacement_lock(tmp_path):
    target = tmp_path / "state.json"
    lock = tmp_path / "state.json.lock"
    with lease_lock(target):
        lock.write_text("replacement-owner", encoding="ascii")
    assert lock.read_text(encoding="ascii") == "replacement-owner"


def test_live_stale_owner_is_not_taken_over(tmp_path):
    target = tmp_path / "state.json"
    with lease_lock(target, stale_after=0):
        with pytest.raises(TimeoutError):
            with lease_lock(target, timeout=0.02, stale_after=0):
                pass


def test_dead_owner_lock_is_reclaimed(tmp_path):
    """属主进程已死的陈旧锁必须被接管。

    此前这条分支在 Windows 上是**死代码**：`os.kill(pid, 0)` 对不存在的 PID 抛的是
    `OSError(errno=EINVAL, winerror=87)` 而非 `ProcessLookupError`，被归进「歧义 =>
    保守判活」。后果是任何被硬杀的 hook 留下的 `.lock` 永久卡死该目标——去重与事件
    幂等静默失效，每轮还固定多付 timeout 秒。

    既有两条用例只覆盖 `_owner_alive -> True`，把它整体换成 `return True` 也全绿。
    """
    import os
    import time

    from context_vault.atomic import _owner_alive, update_json

    target = tmp_path / "state.json"
    lock = tmp_path / "state.json.lock"
    lock.write_text("4294967 0 deadbeef", encoding="ascii")   # 不存在的高位 PID
    old = time.time() - 600                                   # 10 分钟前
    os.utime(lock, (old, old))

    assert _owner_alive(lock) is False, "死 PID 必须判定为已死，否则接管分支不可达"
    result = update_json(target, lambda cur: {**cur, "ok": 1})
    assert result == {"ok": 1}
    assert target.is_file()


def test_hard_stale_lock_is_reclaimed_even_when_owner_looks_alive(tmp_path, monkeypatch):
    """存活探测恒判「活着」时，超过硬上限的锁仍须被强制接管。

    探测在 PID 复用、跨用户 PermissionError 等场景下会永远返回 True；没有这条
    与探测结果无关的自愈路径，锁就再没有出路。
    """
    import time

    from context_vault import atomic

    target = tmp_path / "state.json"
    lock = tmp_path / "state.json.lock"
    lock.write_text("1 0 stillalive", encoding="ascii")
    old = time.time() - 48 * 3600
    import os
    os.utime(lock, (old, old))
    monkeypatch.setattr(atomic, "_owner_alive", lambda _p: True)   # 恒判活

    result = atomic.update_json(target, lambda cur: {**cur, "ok": 2})
    assert result == {"ok": 2}


def test_undecodable_lock_does_not_escape_finally(tmp_path):
    """lock 内容不可 ASCII 解码时，释放阶段不得抛异常掩盖原始错误。"""
    from context_vault.atomic import lease_lock

    target = tmp_path / "state.json"
    lock = tmp_path / "state.json.lock"
    with lease_lock(target):
        lock.write_bytes(b"\xff\xfe\x00binary")     # 非 ASCII，read_text 会 UnicodeDecodeError
    assert lock.exists(), "不是本进程的 token，不得删除他人租约"
