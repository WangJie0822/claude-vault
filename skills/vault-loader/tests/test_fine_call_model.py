# -*- coding: utf-8 -*-
"""精排子调用 `_topic._call_model`（spec 2026-10-13 §6.2 / D8 / F03 / F69）：
argv 带三个隔离参数、cwd 是用完即删的空临时目录、超时杀**整棵**进程树。

杀树用例必须真起进程：PATH 最前放一个 `claude` 垫片（Windows 用 npm 同形的 `.cmd`），
垫片再起一个长睡的 python 孙进程并把 pid 写进文件——只杀直接子进程时孙进程照样活着、照样计费。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import _topic

SLEEPER = (
    "import os, sys, time\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "time.sleep(120)\n"
)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        k32 = ctypes.WinDLL("kernel32")
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)               # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(h))
        return code.value == 259                                # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait(pred, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


@pytest.fixture
def reap():
    """用例失败时别把长睡的孙进程留在机器上。"""
    pidfiles: list = []
    yield pidfiles
    for pf in pidfiles:
        try:
            pid = int(Path(pf).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=30)
            else:
                os.kill(pid, 9)
        except Exception:  # noqa: BLE001
            pass


def _install_shim(tmp_path: Path, monkeypatch, *, orphan: bool = False) -> Path:
    """`orphan=True`（仅 Windows）：垫片先 `ping` 一秒（保证已加入 Job）再 `start /b` 拉起孙进程后
    立即退出——孙进程成孤儿，`taskkill /T` 按父 pid 遍历不到它，只有 Job Object 能带走（F69）。
    孙进程继承了 stdout 管道，所以 `communicate` 照样等到超时。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text(SLEEPER, encoding="utf-8")
    pidfile = tmp_path / "grandchild.pid"
    if os.name == "nt":
        cmd = f'"{sys.executable}" "{sleeper}" "{pidfile}"'
        body = (f'@ping -n 2 127.0.0.1 >nul\r\n@start "" /b {cmd}\r\n' if orphan else f"@{cmd}\r\n")
        (bindir / "claude.cmd").write_text(body, encoding="utf-8")
    else:
        shim = bindir / "claude"
        shim.write_text(f'#!/bin/sh\n"{sys.executable}" "{sleeper}" "{pidfile}"\n', encoding="utf-8")
        shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return pidfile


def test_timeout_kills_grandchild_through_cmd_shim(tmp_path, monkeypatch, reap):
    pidfile = _install_shim(tmp_path, monkeypatch)
    reap.append(pidfile)
    monkeypatch.setattr(_topic, "_TIMEOUT", 3)
    t0 = time.monotonic()
    assert _topic._call_model("hi") is None
    assert _topic._LAST_FAIL == "timeout"
    assert time.monotonic() - t0 < 15, "timeout 不是硬上限：还在等孙进程关管道"
    assert pidfile.exists(), "阳性对照失败：垫片没把孙进程拉起来，本用例对杀树无判别力"
    pid = int(pidfile.read_text(encoding="utf-8"))
    assert _wait(lambda: not _alive(pid), 10), f"孙进程 {pid} 在超时后仍存活：进程树没被杀干净"


@pytest.mark.skipif(os.name != "nt", reason="孤儿孙进程只在 Windows 的 .cmd 垫片形态下出现（F69）")
def test_timeout_kills_orphaned_grandchild_via_job(tmp_path, monkeypatch, reap):
    pidfile = _install_shim(tmp_path, monkeypatch, orphan=True)
    reap.append(pidfile)
    monkeypatch.setattr(_topic, "_TIMEOUT", 4)
    assert _topic._call_model("hi") is None
    assert _topic._LAST_FAIL == "timeout"
    assert _wait(pidfile.exists, 5), "阳性对照失败：孤儿孙进程没被拉起"
    pid = int(pidfile.read_text(encoding="utf-8"))
    assert _wait(lambda: not _alive(pid), 10), f"孤儿孙进程 {pid} 存活：Job Object 没带走它"


def _capture(monkeypatch, out: str = "ok", rc: int = 0) -> dict:
    seen: dict = {}

    def fake(argv, input_text, timeout, env, cwd):
        seen.update(argv=list(argv), input=input_text, timeout=timeout, env=env, cwd=cwd,
                    cwd_existed=os.path.isdir(cwd))
        return rc, out

    monkeypatch.setattr(_topic.shutil, "which", lambda _n: "/fake/claude")
    monkeypatch.setattr(_topic, "_run_tree_capped", fake)
    return seen


def test_call_model_argv_has_isolation_and_temp_cwd(monkeypatch):
    seen = _capture(monkeypatch)
    assert _topic._call_model("提示") == "ok"
    assert seen["argv"][1:] == ["-p", "--model", "haiku", "--tools", "", "--no-session-persistence",
                                "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands"]
    assert seen["input"] == "提示" and seen["timeout"] == _topic._TIMEOUT
    assert seen["env"]["VAULT_LOADER_DISABLE"] == "1"
    assert seen["cwd_existed"] and os.path.abspath(seen["cwd"]) != os.getcwd()
    assert not os.path.exists(seen["cwd"]), "临时 cwd 用完未删除"


def test_call_model_uses_module_model_and_falls_back_on_bad_name(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(_topic, "_MODEL", "sonnet")
    _topic._call_model("p")
    assert seen["argv"][3] == "sonnet"
    monkeypatch.setattr(_topic, "_MODEL", "-x")
    _topic._call_model("p")
    assert seen["argv"][3] == "haiku"                 # spec §5.2：不合法回落 haiku，不得以 - 开头进 argv


@pytest.mark.parametrize("which,mode,expect", [
    (None, "", "no_claude"),
    ("/usr/bin/claude", "rc1", "exit_nonzero"),
    ("/usr/bin/claude", "timeout", "timeout"),
    ("/usr/bin/claude", "oserror", "error"),
])
def test_call_model_sets_last_fail(monkeypatch, which, mode, expect):
    def fake(argv, input_text, timeout, env, cwd):
        if mode == "rc1":
            return 1, ""
        if mode == "timeout":
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
        raise OSError("x")

    monkeypatch.setattr(_topic.shutil, "which", lambda _n: which)
    monkeypatch.setattr(_topic, "_run_tree_capped", fake)
    monkeypatch.setattr(_topic, "_LAST_FAIL", None)
    assert _topic._call_model("p") is None
    assert _topic._LAST_FAIL == expect


def test_run_tree_capped_popen_flags(monkeypatch, tmp_path):
    """内层调 claude 必须带 CREATE_NO_WINDOW——这一处才是弹窗真凶：detached 提炼子进程自身没有控制台，
    它再起 console 子系统的孙子进程时 Windows 会分配一个可见窗口（Claude Code 只抑制它直接 spawn 的 hook）。
    POSIX 上改为新会话（killpg 的前提），且不得传 Windows 常量。按真实平台分支断言。"""
    seen: dict = {}

    class _P:
        pid = 4242
        returncode = 0

        def __init__(self, argv, **kw):
            seen.update(kw)

        def communicate(self, input=None, timeout=None):
            return "ok", ""

        def kill(self):
            pass

    monkeypatch.setattr(_topic.subprocess, "Popen", _P)
    monkeypatch.setattr(_topic, "_win_new_job", lambda: None)
    assert _topic._run_tree_capped(["claude"], "x", 5, {}, str(tmp_path)) == (0, "ok")
    if os.name == "nt":
        assert seen.get("creationflags") == subprocess.CREATE_NO_WINDOW
        assert "start_new_session" not in seen
    else:
        assert seen.get("start_new_session") is True
        assert "creationflags" not in seen
