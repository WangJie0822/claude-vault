"""实际执行声明的完整命令，验证 UTF-8 stdin 与宿主根，而非只验 JSON。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = json.loads((ROOT / "hooks/codex-hooks.json").read_text(encoding="utf-8"))["hooks"]


def _fake_root(path):
    (path / "hooks").mkdir(parents=True)
    (path / "context_vault").mkdir()
    for name in ("run-hook.cmd", "run-codex-hook.sh", "run-codex-hook.ps1"):
        shutil.copyfile(ROOT / "hooks" / name, path / "hooks" / name)
    (path / "context_vault/codex_entry.py").write_text(
        "import sys\nsys.stdout.buffer.write(b'REACHED:' + sys.stdin.buffer.read())\n", encoding="utf-8")
    return path


def _run(command, root, home, payload, *, windows):
    command = command.replace("${PLUGIN_ROOT}", str(root).replace("\\", "/"))
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PLUGIN_ROOT=str(root),
               CLAUDE_PLUGIN_ROOT=str(home / "unrelated-plugin"), PYTHONUTF8="1")
    if windows:
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        if os.name == "nt":
            git = Path(shutil.which("git")).resolve()
            bash = next(p / "bin/bash.exe" for p in list(git.parents)[:3] if (p / "bin/bash.exe").is_file())
            argv = [str(bash), "-c", command]
        else:
            argv = ["/bin/sh", "-c", command]
    return subprocess.run(argv, input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True, env=env, cwd=home, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
@pytest.mark.parametrize("windows", [False, pytest.param(True, marks=pytest.mark.skipif(os.name != "nt", reason="Windows command override"))])
def test_declared_command_delivers_original_utf8_input(tmp_path, event, windows):
    root = _fake_root(tmp_path / "plugin 空格")
    command = HOOKS[event][0]["hooks"][0]["commandWindows" if windows else "command"]
    payload = {"hook_event_name": event, "cwd": str(tmp_path / "项目"), "prompt": "查找召回设计😀"}
    got = _run(command, root, tmp_path, payload, windows=windows)
    assert got.returncode == 0, got.stderr.decode("utf-8", "replace")
    assert got.stdout.startswith(b"REACHED:"), got.stderr.decode("utf-8", "replace")
    assert json.loads(got.stdout[len(b"REACHED:"):]) == payload


def test_codex_manifest_overrides_claude_hook_discovery():
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["hooks"] == "./hooks/codex-hooks.json"
    assert set(HOOKS) == {"SessionStart", "UserPromptSubmit"}
    claude = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    assert claude["modules"] == ["./vault-mod.js"]
