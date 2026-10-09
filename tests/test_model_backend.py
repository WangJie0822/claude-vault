from __future__ import annotations

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
