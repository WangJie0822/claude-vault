"""tests for scripts/_fs.py"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# 沿用本 tests/ 目录的导入约定（同 conftest.py 风格）：把 scripts/ 插到 sys.path
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _fs import _acquire_lock, _release_lock, LOCK_TIMEOUT  # noqa: E402


def test_acquire_lock_creates_lock_file(tmp_path):
    """成功获取锁后 lock 文件应存在；释放后应消失。"""
    lock_path = str(tmp_path / 'test.lock')
    assert _acquire_lock(lock_path) is True
    assert Path(lock_path).exists()
    _release_lock(lock_path)
    assert not Path(lock_path).exists()


def test_acquire_lock_blocks_second_acquire_until_released(tmp_path):
    """同一锁路径第二次获取在锁未释放前应失败；释放后可再次获取。"""
    lock_path = str(tmp_path / 'test.lock')
    assert _acquire_lock(lock_path) is True
    assert _acquire_lock(lock_path, timeout=1) is False
    _release_lock(lock_path)
    assert _acquire_lock(lock_path, timeout=1) is True
    _release_lock(lock_path)


def test_lock_timeout_default_is_300(tmp_path):
    """LOCK_TIMEOUT 兜底为 300s（覆盖 backfill 长操作场景）。"""
    assert LOCK_TIMEOUT == 300


def test_stale_lock_with_dead_pid_gets_removed(tmp_path):
    """stale 锁（mtime 过期）且 PID 已死 → 被强删后允许新进程获取。"""
    lock_path = str(tmp_path / 'test.lock')
    # 构造一个早已过期 + PID 不存在的 lock 文件
    with open(lock_path, 'w', encoding='utf-8') as f:
        f.write('999999\n0')
    very_old = time.time() - 1000
    os.utime(lock_path, (very_old, very_old))
    assert _acquire_lock(lock_path, timeout=1) is True
    _release_lock(lock_path)


def test_stale_lock_with_alive_pid_not_removed(tmp_path):
    """stale 锁（mtime 过期）但 PID 仍存活 → 不强删，新进程获取失败。"""
    lock_path = str(tmp_path / 'test.lock')
    # 用本测试进程自己的 PID 模拟"活的持锁者"
    with open(lock_path, 'w', encoding='utf-8') as f:
        f.write(f'{os.getpid()}\n0')
    very_old = time.time() - 1000
    os.utime(lock_path, (very_old, very_old))
    assert _acquire_lock(lock_path, timeout=1) is False
    os.remove(lock_path)


def test_refresh_lock_updates_mtime(tmp_path):
    """_refresh_lock 调用后 lock 文件 mtime 应被刷新为近时间。"""
    from _fs import _refresh_lock
    lock_path = str(tmp_path / 'test.lock')
    _acquire_lock(lock_path)
    very_old = time.time() - 1000
    os.utime(lock_path, (very_old, very_old))
    _refresh_lock(lock_path)
    assert time.time() - os.path.getmtime(lock_path) < 5
    _release_lock(lock_path)


def test_atomic_write_json_writes_content(tmp_path):
    """atomic_write_json 写出 JSON，内容可解析且等于输入。"""
    import json
    from _fs import atomic_write_json
    p = tmp_path / 'out.json'
    data = [{'a': 1}, {'b': '中文'}]
    atomic_write_json(str(p), data)
    assert json.loads(p.read_text(encoding='utf-8')) == data


def test_atomic_write_json_creates_parent_dir(tmp_path):
    """父目录不存在时自动创建。"""
    import json
    from _fs import atomic_write_json
    p = tmp_path / 'sub' / 'deep' / 'out.json'
    atomic_write_json(str(p), {'x': 1})
    assert json.loads(p.read_text(encoding='utf-8')) == {'x': 1}


def test_atomic_write_json_no_temp_leftover(tmp_path):
    """写完不残留 .tmp 临时文件。"""
    from _fs import atomic_write_json
    p = tmp_path / 'out.json'
    atomic_write_json(str(p), {'x': 1})
    leftovers = [f for f in p.parent.iterdir() if f.suffix == '.tmp']
    assert leftovers == []


def test_atomic_write_text_writes_content(tmp_path):
    from _fs import atomic_write_text
    p = tmp_path / 'out.md'
    atomic_write_text(str(p), '正文\n第二行')
    assert p.read_text(encoding='utf-8') == '正文\n第二行'


def test_atomic_write_text_retries_then_succeeds(tmp_path, monkeypatch):
    """前 2 次 os.replace 抛 PermissionError，第 3 次成功（模拟 Obsidian 占用）。"""
    import _fs
    p = tmp_path / 'out.md'
    calls = {'n': 0}
    real_replace = os.replace

    def flaky_replace(src, dst):
        calls['n'] += 1
        if calls['n'] < 3:
            raise PermissionError(13, 'occupied')
        return real_replace(src, dst)

    monkeypatch.setattr(_fs.os, 'replace', flaky_replace)
    _fs.atomic_write_text(str(p), 'hi', retries=5, backoff=0.001)
    assert p.read_text(encoding='utf-8') == 'hi'
    assert calls['n'] == 3


def test_atomic_write_text_cleans_tmp_on_final_failure(tmp_path, monkeypatch):
    """os.replace 持续失败 → raise + 无 .tmp 残留。"""
    import _fs
    p = tmp_path / 'out.md'

    def always_fail(src, dst):
        raise PermissionError(13, 'occupied')

    monkeypatch.setattr(_fs.os, 'replace', always_fail)
    with pytest.raises(PermissionError):
        _fs.atomic_write_text(str(p), 'hi', retries=3, backoff=0.001)
    leftovers = [f for f in tmp_path.iterdir() if f.suffix == '.tmp']
    assert leftovers == []
