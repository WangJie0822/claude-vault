"""模型选择必须与隔离组合验证；测试不读取真实认证或访问真实模型。"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from context_vault import codex_model as model


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(model, "find_codex", lambda: "/trusted/codex", raising=False)
    monkeypatch.setenv("CODEX_HOME", "/synthetic/codex-home")
    calls = []
    def run(argv, input, timeout, env, cwd):
        calls.append((argv, input, timeout, env, cwd))
        if "mcp" in argv:
            return 0, json.dumps([{"name": 'plugin.name', "env": {"SYNTHETIC_SECRET": "not-to-log"}}, {"name": 'quoted"name'}])
        Path(argv[argv.index("--output-last-message") + 1]).write_text("合成结果", encoding="utf-8")
        return 0, "not the answer"
    return calls, run


@pytest.mark.parametrize("selected", [None, "CV/Exact-ID[large]", "A" * 128])
def test_inherits_user_config_and_preserves_exact_selection_with_isolation(harness, selected):
    calls, run = harness
    assert model.call_codex("长中文" * 40000, selection={"model": selected, "profile": "work", "provider": "custom"},
                            timeout=60, schema={"type": "object"}, runner=run) == "合成结果"
    assert len(calls) == 2
    for argv, text, timeout, env, cwd in calls:
        assert env["CODEX_HOME"] == "/synthetic/codex-home"
        assert env["VAULT_LOADER_DISABLE"] == "1"
        assert argv[argv.index("--profile") + 1] == "work"
        assert 'model_provider="custom"' in argv
        assert "--ignore-user-config" not in argv and "--dangerously-bypass-hook-trust" not in argv
    argv, text, _, _, cwd = calls[1]
    assert text == "长中文" * 40000
    if selected is None:
        assert "--model" not in argv
    else:
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == selected
    assert "skip_host_skill_discovery" in argv and "--ignore-rules" in argv
    assert 'mcp_servers={"plugin.name"={enabled=false},"quoted\\"name"={enabled=false}}' in argv
    for feature in ("hooks", "shell_tool", "unified_exec", "plugins", "apps", "multi_agent", "code_mode"):
        assert any(argv[i:i + 2] == ["--disable", feature] for i in range(len(argv)))
    assert "project_doc_max_bytes=0" in argv and 'web_search="disabled"' in argv
    assert not Path(cwd).exists()


def test_bad_mcp_discovery_prevents_any_model_request(harness, capsys):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return 0, "not json synthetic-secret"
    assert model.call_codex("x", selection={}, runner=run) is None
    assert len(calls) == 1 and "mcp" in calls[0]
    assert model.LAST_FAIL == "isolation_failed"
    assert "synthetic-secret" not in capsys.readouterr().err


def test_missing_cli_is_diagnostic_without_fallback(monkeypatch):
    monkeypatch.setattr(model, "find_codex", lambda: None, raising=False)
    assert model.call_codex("x", selection={}) is None
    assert model.LAST_FAIL == "no_codex"


def test_timeout_is_diagnostic_and_keeps_original_selection(harness):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    assert model.call_codex("x", selection={"model": "CV/Custom"}, timeout=2, runner=run) is None
    assert model.LAST_FAIL == "timeout"


def test_cleanup_error_cannot_discard_paid_result(harness, monkeypatch):
    _, run = harness
    monkeypatch.setattr(model, "cleanup", lambda _: (_ for _ in ()).throw(OSError("busy")), raising=False)
    assert model.call_codex("x", selection={}, runner=run) == "合成结果"


@pytest.mark.parametrize("selection", [{"model": "--bad"}, {"model": "x&whoami"}, {"profile": "bad\nname"}, {"provider": "a.b\""}])
def test_invalid_selection_does_not_spawn(harness, selection):
    calls, run = harness
    assert model.call_codex("x", selection=selection, runner=run) is None
    assert not calls


def test_executable_search_ignores_cwd_and_relative_path(tmp_path, monkeypatch):
    import os
    name = "codex.cmd" if os.name == "nt" else "codex"
    (tmp_path / name).write_text("synthetic", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "." + os.pathsep + "relative")
    assert model.find_codex() is None
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / name).chmod(0o755)
    assert model.find_codex() == str(tmp_path / name)
