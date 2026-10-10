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
    assert "--model" in out, "claude 不再支持 --model；call_claude 需要同步更新"


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
    monkeypatch.setattr(model.subprocess, "run",
                        lambda *a, **k: ran.append(a) or subprocess.CompletedProcess(a, 1, "", ""))
    assert model.call_claude("hi") is None
    assert ran == []
