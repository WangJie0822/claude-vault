# tests/test_hooks_json.py
import json
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