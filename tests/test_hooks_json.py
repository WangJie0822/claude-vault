# tests/test_hooks_json.py
import json
import re
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


def test_hooks_json_valid_and_covers_events():
    data = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    h = data["hooks"]
    assert len(h["SessionStart"]) == 1          # vault-loader
    assert len(h["UserPromptSubmit"]) == 1
    assert "SessionEnd" not in h                 # auto-mode 已移除
    # All commands use CLAUDE_PLUGIN_ROOT, no private/absolute paths
    blob = json.dumps(data)
    assert "${CLAUDE_PLUGIN_ROOT}" in blob
    # No absolute OS paths or private usernames
    assert "C:\\Users" not in blob
    import os
    username = os.environ.get("USERNAME", os.environ.get("USER", ""))
    if username:
        assert username not in blob, f"Private username {username!r} found in hooks.json"


def test_session_start_has_matcher(tmp_path):
    """SessionStart entries must have matcher=startup|clear|compact to exclude resume (M-1 fix)."""
    data = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    for entry in data["hooks"]["SessionStart"]:
        assert entry.get("matcher") == "startup|clear|compact", (
            f"SessionStart entry missing matcher: {entry}"
        )


def test_hooks_json_uses_run_hook_cmd():
    """All hook commands must reference run-hook.cmd (polyglot wrapper), not run-hook.sh."""
    data = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    blob = json.dumps(data)
    assert "run-hook.cmd" in blob
    assert "run-hook.sh" not in blob


def test_hooks_json_commands_have_type_command():
    """Every hook entry must have type=command."""
    data = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    for event, entries in data["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                assert hook["type"] == "command", (
                    f"Event {event}: hook type is {hook['type']!r}, expected 'command'"
                )


def test_hooks_json_has_no_codex_only_fields():
    """`commandWindows` 与 `additionalContextLimit` 只被 Codex 读取，2.0.0 起不得再出现。"""
    data = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    for entries in data["hooks"].values():
        for entry in entries:
            for hook in entry["hooks"]:
                assert "commandWindows" not in hook
                assert "additionalContextLimit" not in hook


_COMMAND_FORM = re.compile(
    r'^/bin/sh "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/run-hook\.cmd" '
    r'skills/vault-loader/scripts/[a-z_]+\.py \|\| exit 1$')
_ENTRY_OF = {"SessionStart": "session_start_load.py", "UserPromptSubmit": "prompt_submit_load.py"}


def test_hook_commands_run_wrapper_through_shell():
    """命令的第一个词必须是解释器的绝对路径，不能是 wrapper 路径，也不能是裸名。

    - `run-hook.cmd` 在 git 里是 100644：POSIX 上直接执行它会报 Permission denied（126），hook 从不运行；
    - 裸名 `sh`（或 `"${BASH:-sh}"` 在 dash 下的回落）要按 PATH 查找，PATH 含 `.` 时会执行仓库里预置的 `sh`；
      `/bin/sh` 在 macOS、Linux 与 Git Bash 下都存在（Git Bash 里映射到 Git 自带的 sh）；
    - `|| exit 1`：dash 打不开文件时以 2 退出，UserPromptSubmit 收到 2 会拦截用户提交；
    - `shell: bash` 把对 Git Bash 的依赖写进声明；
    - 不得有 `args`：有 `args` 时 Claude Code 改走 exec form、忽略 `shell`、把整串当成可执行文件名，
      而 exec form 在本插件不可行（wrapper 是 100644；Windows 的 exec form 不能 spawn `.cmd`）。
    """
    data = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    hooks = [h for entries in data["hooks"].values() for e in entries for h in e["hooks"]]
    assert len(hooks) == 2
    for hook in hooks:
        assert _COMMAND_FORM.match(hook["command"]), hook["command"]
        assert hook.get("shell") == "bash", hook
        assert "args" not in hook, hook


def test_each_event_runs_its_own_entry():
    """命令串里的脚本名由 `_COMMAND_FORM` 放行任意值，两个事件指错入口时其余用例分不出来。"""
    data = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    got = {ev: [h["command"] for e in es for h in e["hooks"]] for ev, es in data.items()}
    assert set(got) == set(_ENTRY_OF)
    for ev, cmds in got.items():
        assert len(cmds) == 1 and f"skills/vault-loader/scripts/{_ENTRY_OF[ev]} " in cmds[0], (ev, cmds)
