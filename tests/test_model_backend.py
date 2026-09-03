from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from context_vault import model


class _Result:
    returncode = 0
    stdout = ""
    stderr = ""


def test_codex_backend_uses_schema_output_and_isolated_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(model.shutil, "which", lambda name: "codex-bin" if name == "codex" else None)

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text('{"keywords":["context"]}', encoding="utf-8")
        return _Result()

    monkeypatch.setattr(model.subprocess, "run", fake_run)
    result = model.call_codex("prompt")
    assert json.loads(result)["keywords"] == ["context"]
    argv = seen["argv"]
    assert argv[:3] == ["codex-bin", "exec", "-"]
    for flag in ("--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                 "--output-schema", "--output-last-message"):
        assert flag in argv
    disabled = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "--disable"]
    assert {"hooks", "shell_tool", "apps", "browser_use", "computer_use"} <= set(disabled)
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["env"]["VAULT_LOADER_DISABLE"] == "1"


def test_backend_auto_follows_codex_plugin_env(monkeypatch):
    monkeypatch.setenv("PLUGIN_ROOT", "/plugin")
    assert model.choose_backend("auto") == "codex"
    monkeypatch.delenv("PLUGIN_ROOT")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin")
    assert model.choose_backend("auto") == "claude"
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT")
    with pytest.raises(ValueError, match="cannot infer"):
        model.choose_backend("auto")


@pytest.mark.skipif(shutil.which("codex") is None, reason="本机未安装 codex CLI")
def test_codex_flags_still_exist_in_real_cli():
    """opt-in 集成用例：`call_codex` 用到的 flag 必须在真实 codex CLI 上仍然存在。

    其余用例把 `shutil.which` 与 `subprocess.run` 双双替换掉，只断言 argv 里含某些
    字符串——对「真实 codex 是否接受这些 flag」**完全没有判别力**。CLI 改名或废弃
    某个 flag 时那组用例仍全绿，而失败形态是 `call_codex` 恒返回 None →
    `enrich_keywords` 静默「跳过（codex 失败/缺失）」。
    """
    # 必须用 which 的返回值当 argv[0]：Windows 上这两个命令是 `.CMD`，
    # subprocess 不做 PATHEXT 解析，传裸名会 FileNotFoundError。
    exe = shutil.which("codex")
    out = subprocess.run([exe, "exec", "--help"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60).stdout
    for flag in ("--output-last-message", "--skip-git-repo-check"):
        assert flag in out, f"codex exec 不再支持 {flag}；call_codex 需要同步更新"


@pytest.mark.skipif(shutil.which("claude") is None, reason="本机未安装 claude CLI")
def test_claude_flags_still_exist_in_real_cli():
    """同上，针对 `call_claude`——它此前一条用例都没有。"""
    exe = shutil.which("claude")
    out = subprocess.run([exe, "--help"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60).stdout
    assert "--model" in out, "claude 不再支持 --model；call_claude 需要同步更新"
