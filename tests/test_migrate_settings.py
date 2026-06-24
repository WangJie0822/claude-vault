"""tests/test_migrate_settings.py

Tests for scripts/migrate_settings.py  (run_migration function).

Coverage:
  - dry-run: finds exactly the 4 target entries, does NOT modify the file
  - --apply: removes exactly the 4 target entries, preserves all unrelated hooks,
             creates a backup, leaves valid JSON, drops empty groups,
             leaves unrelated-only events untouched
  - missing file: exits 0 with informational message
  - no matches: exits 0 with informational message
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make scripts/ importable without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from migrate_settings import run_migration, TARGET_FILENAMES  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

# Settings dict that contains ALL 4 target hooks mixed with unrelated hooks
_FIXTURE: dict = {
    "theme": "dark",
    "hooks": {
        # Event with ONE target + ONE unrelated in the same group list
        "SessionStart": [
            {
                "matcher": ".*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "/home/user/.claude/skills/vault-loader/scripts/session_start_load.py",
                    },
                    {
                        "type": "command",
                        "command": "/home/user/.claude/hooks/session_start_auto_notify.py",
                    },
                    {
                        "type": "command",
                        "command": "/home/user/.claude/hooks/audit_reminder.py",  # UNRELATED
                    },
                ],
            }
        ],
        # Event with ONE target (top-level single entry, no inner list)
        "UserPromptSubmit": [
            {
                "type": "command",
                "command": "/home/user/.claude/skills/vault-loader/scripts/prompt_submit_load.py",
            },
            {
                "type": "command",
                "command": "/home/user/.claude/hooks/worktree_guard.py",  # UNRELATED
            },
        ],
        # Event with ONE target entry (command via args list)
        "SessionEnd": [
            {
                "type": "command",
                "command": "python3",
                "args": ["/home/user/.claude/hooks/session_end_enqueue.py", "--mode=auto"],
            },
        ],
        # Event that has ONLY unrelated hooks — must be fully preserved
        "PreToolUse": [
            {
                "type": "command",
                "command": "/home/user/.claude/hooks/fact_first_guardian.py",
            },
        ],
    },
}


def _write_fixture(tmp_path: Path) -> Path:
    """Write the fixture dict to a temp settings.json and return its path."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(_FIXTURE, indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Helper: count how many times a target filename appears in the current hooks
# ---------------------------------------------------------------------------

def _count_targets_in_file(settings_path: Path) -> int:
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks: dict = data.get("hooks", {})
    count = 0
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            entries = group.get("hooks", [group]) if isinstance(group, dict) and "hooks" in group else ([group] if isinstance(group, dict) else group if isinstance(group, list) else [])
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                cmd = entry.get("command", "")
                args = entry.get("args", [])
                for name in TARGET_FILENAMES:
                    if name in cmd or any(name in a for a in args if isinstance(a, str)):
                        count += 1
                        break
    return count


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDryRun:
    """Dry-run (apply=False) must not touch the file."""

    def test_returns_zero(self, tmp_path):
        p = _write_fixture(tmp_path)
        rc = run_migration(p, apply=False)
        assert rc == 0

    def test_file_not_modified(self, tmp_path):
        p = _write_fixture(tmp_path)
        original_text = p.read_text(encoding="utf-8")
        run_migration(p, apply=False)
        assert p.read_text(encoding="utf-8") == original_text, (
            "dry-run must not modify the settings file"
        )

    def test_finds_all_four_targets(self, tmp_path, capsys):
        p = _write_fixture(tmp_path)
        run_migration(p, apply=False)
        captured = capsys.readouterr()
        # All 4 target script names should appear in the dry-run output
        for name in TARGET_FILENAMES:
            assert name in captured.out, f"Expected {name!r} in dry-run output"

    def test_reports_four_entries(self, tmp_path, capsys):
        p = _write_fixture(tmp_path)
        run_migration(p, apply=False)
        captured = capsys.readouterr()
        assert "4" in captured.out, "Should report 4 matched entries"

    def test_no_backup_created(self, tmp_path):
        p = _write_fixture(tmp_path)
        run_migration(p, apply=False)
        backups = list(tmp_path.glob("settings.json.bak-*"))
        assert not backups, "dry-run must not create a backup file"


class TestApply:
    """--apply mode must strip targets, preserve unrelated hooks, backup."""

    def test_returns_zero(self, tmp_path):
        p = _write_fixture(tmp_path)
        rc = run_migration(p, apply=True)
        assert rc == 0

    def test_backup_file_created(self, tmp_path):
        p = _write_fixture(tmp_path)
        run_migration(p, apply=True)
        backups = list(tmp_path.glob("settings.json.bak-*"))
        assert len(backups) == 1, "Exactly one backup file should be created"

    def test_backup_has_original_content(self, tmp_path):
        p = _write_fixture(tmp_path)
        original_bytes = p.read_bytes()
        run_migration(p, apply=True)
        backups = list(tmp_path.glob("settings.json.bak-*"))
        assert backups[0].read_bytes() == original_bytes

    def test_result_is_valid_json(self, tmp_path):
        p = _write_fixture(tmp_path)
        run_migration(p, apply=True)
        result = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(result, dict)

    def test_no_target_entries_remain(self, tmp_path):
        p = _write_fixture(tmp_path)
        run_migration(p, apply=True)
        remaining = _count_targets_in_file(p)
        assert remaining == 0, f"Expected 0 target entries after apply, found {remaining}"

    def test_unrelated_hooks_preserved_worktree_guard(self, tmp_path):
        p = _write_fixture(tmp_path)
        run_migration(p, apply=True)
        content = p.read_text(encoding="utf-8")
        assert "worktree_guard.py" in content, "worktree_guard.py (unrelated) must be preserved"

    def test_unrelated_hooks_preserved_audit_reminder(self, tmp_path):
        p = _write_fixture(tmp_path)
        run_migration(p, apply=True)
        content = p.read_text(encoding="utf-8")
        assert "audit_reminder.py" in content, "audit_reminder.py (unrelated) must be preserved"

    def test_unrelated_only_event_preserved(self, tmp_path):
        p = _write_fixture(tmp_path)
        run_migration(p, apply=True)
        data = json.loads(p.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        assert "PreToolUse" in hooks, "PreToolUse event (unrelated-only) must remain"
        assert hooks["PreToolUse"], "PreToolUse hooks list must not be empty"
        # Verify fact_first_guardian.py is still there
        content = p.read_text(encoding="utf-8")
        assert "fact_first_guardian.py" in content

    def test_non_hook_settings_preserved(self, tmp_path):
        p = _write_fixture(tmp_path)
        run_migration(p, apply=True)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data.get("theme") == "dark", "Non-hook settings must be preserved"

    def test_session_start_event_still_has_unrelated(self, tmp_path):
        """SessionStart should keep audit_reminder.py but drop the 2 target entries."""
        p = _write_fixture(tmp_path)
        run_migration(p, apply=True)
        content = p.read_text(encoding="utf-8")
        assert "audit_reminder.py" in content
        assert "session_start_load.py" not in content
        assert "session_start_auto_notify.py" not in content

    def test_session_end_event_empty_group_removed(self, tmp_path):
        """SessionEnd only had the target; after apply that event key may be absent or empty."""
        p = _write_fixture(tmp_path)
        run_migration(p, apply=True)
        data = json.loads(p.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        # SessionEnd had only the target → either key absent or list is empty
        session_end = hooks.get("SessionEnd", [])
        assert session_end == [], f"SessionEnd should have no entries, got: {session_end}"


class TestEdgeCases:
    """Edge-case handling: missing file, no matches."""

    def test_missing_file_exits_zero(self, tmp_path, capsys):
        nonexistent = tmp_path / "no_such_settings.json"
        rc = run_migration(nonexistent, apply=False)
        assert rc == 0
        captured = capsys.readouterr()
        assert "nothing to migrate" in captured.out.lower() or "not found" in captured.out.lower()

    def test_no_matches_exits_zero(self, tmp_path, capsys):
        p = tmp_path / "settings.json"
        clean = {"hooks": {"PreToolUse": [{"type": "command", "command": "/some/other/hook.py"}]}}
        p.write_text(json.dumps(clean), encoding="utf-8")
        rc = run_migration(p, apply=False)
        assert rc == 0
        captured = capsys.readouterr()
        assert "nothing to migrate" in captured.out.lower()

    def test_no_matches_does_not_modify_file(self, tmp_path):
        p = tmp_path / "settings.json"
        clean = {"hooks": {"PreToolUse": [{"type": "command", "command": "/some/other/hook.py"}]}}
        original = json.dumps(clean)
        p.write_text(original, encoding="utf-8")
        run_migration(p, apply=True)
        assert p.read_text(encoding="utf-8") == original

    def test_target_in_args_list_detected(self, tmp_path, capsys):
        """Entry where filename is in args (not command) must be matched."""
        p = tmp_path / "settings.json"
        data = {
            "hooks": {
                "SessionEnd": [
                    {
                        "type": "command",
                        "command": "python3",
                        "args": ["/abs/path/session_end_enqueue.py"],
                    }
                ]
            }
        }
        p.write_text(json.dumps(data), encoding="utf-8")
        run_migration(p, apply=False)
        captured = capsys.readouterr()
        assert "session_end_enqueue.py" in captured.out
