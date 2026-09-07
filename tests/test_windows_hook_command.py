"""Execute the declared Windows command, including Codex's verbatim root paths."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows command interpreter")
@pytest.mark.parametrize("verbatim", [False, True])
@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_windows_declaration_delivers_stdin_to_wrapper(tmp_path, verbatim, event):
    # Spaces and non-ASCII names exercise real command quoting. A probe beneath
    # the real wrapper reports stdin, proving success isn't just fail-open exit 0.
    root = tmp_path / "plugin space 知识库"
    (root / "hooks").mkdir(parents=True)
    shutil.copyfile(ROOT / "hooks/run-hook.cmd", root / "hooks/run-hook.cmd")
    data = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    hook = data["hooks"][event][0]["hooks"][0]
    script = hook["command"].split()[-1]
    target = root / script
    target.parent.mkdir(parents=True)
    target.write_text("import sys\nsys.stdout.write(sys.stdin.read())\n", encoding="utf-8")
    plugin_root = str(root)
    if verbatim:
        plugin_root = "\\\\?\\" + plugin_root
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": plugin_root,
           "PLUGIN_ROOT": plugin_root, "PYTHONUTF8": "1"}
    payload = json.dumps({"hook_event_name": event, "prompt": "知识库测试"})
    # Codex build_command passes the whole declaration as a raw quoted /C arg.
    #
    # 刻意**不**再参数化出 `powershell -Command <整条声明>` 那一半：`commandWindows`
    # 的值本身就以 `powershell.exe` 开头，把它再塞进一个 `-Command` 里是生产中不存在
    # 的嵌套形态 —— 且外层解释器会先把内层的 `$var` 展开成空，于是任何在声明里用了
    # PowerShell 变量的**正确**实现都会被它判红（实测 `MissingVariableNameAfterForeach`）。
    # 这类与被测对象无关的假信号比少一组覆盖更有害。
    command = 'cmd.exe /C "' + hook["commandWindows"] + '"'
    result = subprocess.run(command,
                            input=payload, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == json.loads(payload)


def _minimal_tree(base, event, name, body):
    """一份最小插件树：真 wrapper + 一个可辨认的目标脚本。返回 (root, hook)。"""
    root = base / name
    (root / "hooks").mkdir(parents=True)
    shutil.copyfile(ROOT / "hooks/run-hook.cmd", root / "hooks/run-hook.cmd")
    data = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    hook = data["hooks"][event][0]["hooks"][0]
    target = root / hook["command"].split()[-1]
    target.parent.mkdir(parents=True)
    target.write_text(body, encoding="utf-8")
    return root, hook


@pytest.mark.skipif(os.name != "nt", reason="Windows command interpreter")
@pytest.mark.parametrize("shape", ["both", "plugin_only", "claude_only", "neither"])
@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_windows_declaration_never_fails_closed(tmp_path, event, shape):
    """hook 必须 fail-open：任何 root 变量形态下都不得返回非零。

    `commandWindows` 只被 Codex 读（codex.exe 命中 11 处 / claude.exe 0 处，
    多重阳性对照存活），而 Codex 侧的 root 变量是 `PLUGIN_ROOT` ——
    `context_vault/runtime.py:28` 正以它作为「这是 Codex」的判据。
    此前该声明无条件 `Get-Item Env:CLAUDE_PLUGIN_ROOT`，缺失即抛
    ItemNotFoundException、rc=1、wrapper 从未被执行，恰好在它所服务的那个
    runtime 上 fail-closed，违反 CLAUDE.md「所有 hook fail-open」硬不变量。

    `neither` 一档同样必须 rc=0：`run-hook.cmd:15-18` 连「脚本压根不存在」
    都是 `exit /b 0` + 一行 stderr 提示，PowerShell 前置层不得比它更严。
    """
    root, hook = _minimal_tree(tmp_path, event, "plugin space 知识库",
                               "import sys\nsys.stdout.write('RAN')\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")}
    env["PYTHONUTF8"] = "1"
    if shape in ("both", "plugin_only"):
        env["PLUGIN_ROOT"] = str(root)
    if shape in ("both", "claude_only"):
        env["CLAUDE_PLUGIN_ROOT"] = str(root)

    result = subprocess.run('cmd.exe /C "' + hook["commandWindows"] + '"',
                            input="{}", capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env, timeout=30)
    assert result.returncode == 0, f"{shape}: rc={result.returncode} stderr={result.stderr}"
    if shape == "neither":
        assert "RAN" not in result.stdout
    else:
        assert "RAN" in result.stdout, f"{shape}: stderr={result.stderr}"


@pytest.mark.skipif(os.name != "nt", reason="Windows command interpreter")
def test_windows_declaration_keeps_plugin_root_precedence(tmp_path):
    """两个 root 并存且不一致时，必须遵守 `run-hook.cmd:12` 的 PLUGIN_ROOT 优先。

    此前 `commandWindows` 用 `Set-Item Env:PLUGIN_ROOT (Get-Item
    Env:CLAUDE_PLUGIN_ROOT).Value` 覆盖宿主给定的 PLUGIN_ROOT，使 wrapper 的两个
    候选指向同一根、带存在性校验的择根无从发生 —— 于是**静默**（rc 仍为 0）
    执行了另一份插件副本。双 runtime 用户在同一终端里自然产生该形态。
    """
    event = "UserPromptSubmit"
    a, hook = _minimal_tree(tmp_path / "a", event, "codex-copy",
                            "import sys\nsys.stdout.write('FROM_PLUGIN_ROOT')\n")
    b, _ = _minimal_tree(tmp_path / "b", event, "stale-claude-copy",
                         "import sys\nsys.stdout.write('FROM_CLAUDE_PLUGIN_ROOT')\n")
    env = {**os.environ, "PLUGIN_ROOT": str(a),
           "CLAUDE_PLUGIN_ROOT": str(b), "PYTHONUTF8": "1"}
    result = subprocess.run('cmd.exe /C "' + hook["commandWindows"] + '"',
                            input="{}", capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "FROM_PLUGIN_ROOT" in result.stdout, (
        f"执行了非宿主指定的副本：{result.stdout!r}")
