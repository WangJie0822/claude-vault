"""按 Claude Code 的方式执行 `hooks/hooks.json` 里的完整 `command`：核对脚本被找到、
stdin 到达脚本，脚本缺失时仍 exit 0（所有 hook fail-open）。

Claude Code 把 `${CLAUDE_PLUGIN_ROOT}` 插值进命令串后交给 shell 执行。Windows 上装了
Git Bash 时用它，而 Git Bash 会把 `.cmd` 文件交给 cmd.exe 执行（2026-10-08 本机实测：
polyglot 探针只输出 batch 段的标记），所以这里在 Windows 上覆盖的是 batch 段、在
macOS / Linux 上覆盖的是 sh 段——`run-hook.cmd` 必须同时是合法的 batch 与 POSIX sh。

本文件不覆盖第三种形态：Windows 上没装 Git Bash 时，Claude Code 改用 PowerShell 执行
同一条命令串，而该命令串在 PowerShell 里解析失败、脚本根本没被执行（full-review
P4A-E1，1.x 起就存在，不是 2.0.0 引入的，另开项处理）。所以这组用例全绿只说明
「有 Git Bash 的 Windows」与 macOS / Linux 可用。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]


def _find_bash() -> str | None:
    """Claude Code 在 Windows 上用的是 Git Bash，不是 PATH 上第一个 bash。

    Windows 常规终端里 PATH 上第一个 bash 是 System32 的 WSL 启动器。所以这里从 git 的
    安装位置往上找 `bin\\bash.exe`（`...\\Git\\cmd\\git.exe` 或 `...\\Git\\mingw64\\bin\\git.exe`），
    找不到就返回 None 让整组用例跳过，绝不退回 PATH 查找。
    """
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    if git:
        for base in list(Path(git).resolve().parents)[:3]:
            cand = base / "bin" / "bash.exe"
            if cand.is_file():
                return str(cand)
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="没有 bash，无法按 Claude Code 的方式执行 command")

_ECHO_SCRIPT = "import sys\nprint('STDIN_REACHED:' + sys.stdin.read())\n"


def _commands() -> list[tuple[str, str]]:
    return [(event, hook["command"]) for event, entries in HOOKS.items()
            for entry in entries for hook in entry["hooks"]]


def _fake_root(base: Path, *, with_scripts: bool = True) -> Path:
    """复制 wrapper；两个入口位置放回显 stdin 的替身脚本。"""
    (base / "hooks").mkdir(parents=True)
    shutil.copy2(ROOT / "hooks" / "run-hook.cmd", base / "hooks" / "run-hook.cmd")
    if with_scripts:
        scripts = base / "skills" / "vault-loader" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("session_start_load.py", "prompt_submit_load.py"):
            (scripts / name).write_text(_ECHO_SCRIPT, encoding="utf-8")
    return base


def _run(command: str, root: Path, env_root: str | None) -> subprocess.CompletedProcess:
    cmd = command.replace("${CLAUDE_PLUGIN_ROOT}", root.as_posix())
    env = dict(os.environ)
    env.pop("PLUGIN_ROOT", None)
    if env_root is None:
        env.pop("CLAUDE_PLUGIN_ROOT", None)
    else:
        env["CLAUDE_PLUGIN_ROOT"] = env_root
    return subprocess.run([BASH, "-c", cmd], input='{"probe": 1}', capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=60)


@needs_bash
@pytest.mark.parametrize("event,command", _commands())
def test_claude_command_reaches_script_with_stdin(tmp_path, event, command):
    root = _fake_root(tmp_path / "plugin")
    r = _run(command, root, str(root))
    assert r.returncode == 0, r.stderr
    assert 'STDIN_REACHED:{"probe": 1}' in r.stdout, (event, r.stdout, r.stderr)


@needs_bash
@pytest.mark.parametrize("event,command", _commands())
def test_claude_command_survives_space_and_cjk_in_root(tmp_path, event, command):
    """Review Focus 4：Windows 用户目录常含空格与中文。"""
    root = _fake_root(tmp_path / "插件 根")
    r = _run(command, root, str(root))
    assert r.returncode == 0, r.stderr
    assert "STDIN_REACHED:" in r.stdout, (event, r.stdout, r.stderr)


@needs_bash
@pytest.mark.parametrize("event,command", _commands())
def test_claude_command_falls_back_to_wrapper_dir_without_env(tmp_path, event, command):
    """Review Focus 5：hook 子进程环境里没有 CLAUDE_PLUGIN_ROOT 时回落到 wrapper 自身位置。"""
    root = _fake_root(tmp_path / "plugin")
    r = _run(command, root, None)
    assert r.returncode == 0, r.stderr
    assert "STDIN_REACHED:" in r.stdout, (event, r.stdout, r.stderr)


@needs_bash
def test_missing_script_still_exits_zero(tmp_path):
    root = _fake_root(tmp_path / "plugin", with_scripts=False)
    _, command = _commands()[0]
    r = _run(command, root, str(root))
    assert r.returncode == 0
    assert "hook script not found" in r.stderr


@pytest.mark.skipif(os.name != "nt", reason="WSL 启动器只出现在 Windows 的 PATH 上")
def test_find_bash_never_picks_the_wsl_launcher(monkeypatch):
    """Windows 常规终端里 PATH 上第一个 bash 是 System32 的 WSL 启动器，
    不是 Git Bash；拿它执行 `D:/...` 路径必然失败，整组用例会误报红。"""
    real_which = shutil.which
    wsl = r"C:\Windows\System32\bash.exe"
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **k: wsl if name == "bash" else real_which(name, *a, **k))
    found = _find_bash()
    if real_which("git") is None:
        assert found is None
    else:
        assert found and "system32" not in found.lower() and Path(found).is_file(), found
