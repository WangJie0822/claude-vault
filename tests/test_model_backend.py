from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from context_vault import model


def test_backend_auto_requires_claude_plugin_root(monkeypatch):
    """auto 只在 Claude Code 的 hook / skill 环境里成立；Codex 的 PLUGIN_ROOT 不再有任何作用。"""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("PLUGIN_ROOT", "/plugin")
    with pytest.raises(ValueError, match="backend"):
        model.choose_backend("auto")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin")
    assert model.choose_backend("auto") == "claude"


def test_backend_rejects_codex():
    with pytest.raises(ValueError, match="unsupported backend"):
        model.choose_backend("codex")


def test_codex_backend_is_removed():
    assert not hasattr(model, "call_codex")
    assert not hasattr(model, "KEYWORDS_SCHEMA")


@pytest.mark.skipif(shutil.which("claude") is None, reason="本机未安装 claude CLI")
def test_claude_flags_still_exist_in_real_cli():
    """opt-in 集成用例：`call_claude` 用到的 flag 必须在真实 claude CLI 上仍然存在。"""
    exe = shutil.which("claude")
    out = subprocess.run([exe, "--help"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60).stdout
    # 2.3.0 起 argv 带三个隔离参数：CLI 太旧不认它们时每篇都会 model_failed（DO-M2；2.1.296 实测都在）
    flags = ["--model", "--tools", "--no-session-persistence"] + [f for f in model._ISOLATION if f.startswith("--")]
    missing = [f for f in flags if f not in out]
    assert not missing, f"claude CLI 不再支持 {missing}；call_claude 需要同步更新"


def test_call_claude_ignores_claude_planted_in_cwd(tmp_path, monkeypatch):
    """Python 3.9–3.11 的 which 会返回当前目录里的 claude.cmd 并被直接执行。"""
    from context_vault import exe_search
    monkeypatch.setattr(exe_search.sys, "platform", "win32")
    monkeypatch.setattr(exe_search.shutil, "which", lambda n, *a, **k: os.path.join(os.curdir, n + ".CMD"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    ran = []
    # 桩返回一个失败的 CompletedProcess：被误调时 call_claude 照常走完，由下面的断言报出，
    # 而不是在取 .returncode 时抛 AttributeError
    monkeypatch.setattr(model, "_run_capped",
                        lambda *a, **k: ran.append(a) or subprocess.CompletedProcess(a, 1, "", ""))
    assert model.call_claude("hi") is None
    assert ran == []


def _capture_run(monkeypatch, out="ok"):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["cwd"] = kw.get("cwd")
        seen["cwd_exists_during_call"] = bool(kw.get("cwd")) and os.path.isdir(kw["cwd"])
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(model, "which_no_cwd", lambda name: "C:/fake/claude.CMD")
    monkeypatch.setattr(model, "_run_capped", fake_run)
    return seen


def test_call_claude_passes_isolation_flags_and_temp_cwd(monkeypatch):
    seen = _capture_run(monkeypatch)
    assert model.call_claude("hi") == "ok"
    argv = seen["argv"]
    assert argv[1:] == ["-p", "--model", "haiku", "--tools", "", "--no-session-persistence",
                        "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands"]
    assert seen["cwd_exists_during_call"] and os.path.abspath(seen["cwd"]) != os.getcwd()
    assert not os.path.exists(seen["cwd"])


def test_call_claude_argv_has_no_cmd_metachars(monkeypatch):
    seen = _capture_run(monkeypatch)
    model.call_claude("hi", model="claude-haiku-5-5[1m]")
    assert not any(c in a for a in seen["argv"][1:] for c in '"%&|<>^!')


def test_call_claude_timeout_is_a_hard_cap_through_cmd_shim(tmp_path, monkeypatch):
    """npm 的 .CMD 垫片下真正干活的是孙进程：只杀直接子进程时还要等它关掉管道、照样跑完计费。
    超时必须杀整棵进程树（P2-PM1）。"""
    import sys
    import time
    if os.name == "nt":
        shim = tmp_path / "claude.cmd"
        shim.write_text('@"%s" -c "import time; time.sleep(30)"\r\n' % sys.executable, encoding="utf-8")
    else:
        shim = tmp_path / "claude"
        shim.write_text('#!/bin/sh\n"%s" -c "import time; time.sleep(30)"\n' % sys.executable, encoding="utf-8")
        shim.chmod(0o755)
    real = model.which_no_cwd
    monkeypatch.setattr(model, "which_no_cwd", lambda name: str(shim) if name == "claude" else real(name))
    t0 = time.monotonic()
    assert model.call_claude("hi", timeout=2) is None
    assert time.monotonic() - t0 < 15


def test_call_claude_keeps_result_when_temp_cleanup_fails(monkeypatch):
    """临时目录清理失败（Windows 上后代进程仍占用 cwd）不得吞掉已付费的成功结果。"""
    seen = _capture_run(monkeypatch, out="paid-result")
    real_rmtree = shutil.rmtree

    def boom(path, ignore_errors=False, **kw):
        if not ignore_errors:
            raise PermissionError(32, "being used by another process", str(path))
    monkeypatch.setattr(shutil, "rmtree", boom)
    try:
        assert model.call_claude("hi") == "paid-result"
    finally:
        monkeypatch.setattr(shutil, "rmtree", real_rmtree)
        if seen.get("cwd"):
            real_rmtree(seen["cwd"], ignore_errors=True)


@pytest.mark.parametrize("bad", ["-x", "haiku; rm", "a b", "", "x" * 0])
def test_call_claude_rejects_bad_model_without_spawning(monkeypatch, bad):
    seen = _capture_run(monkeypatch)
    assert model.call_claude("hi", model=bad) is None
    assert "argv" not in seen
