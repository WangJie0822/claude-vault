"""tests/test_uninstall.py

Tests for uninstall.build_remove_command() — pure command construction,
no actual unregistration or filesystem changes happen.

Also tests _remove_data_dirs() data-safety: only runtime artifacts are removed,
never the whole skill dir, and the user's knowledge-vault is always preserved.
"""
import sys
import shutil
import importlib
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import uninstall as u


def test_remove_command_windows():
    cmd = u.build_remove_command("win32")
    assert "schtasks" in cmd[0] and "/Delete" in cmd
    assert "claude-vault-auto" in " ".join(cmd)


def test_remove_command_all_platforms():
    for p in ("win32", "linux", "darwin"):
        assert u.build_remove_command(p)  # non-empty


def test_remove_command_windows_force_flag():
    """Windows remove command must include /F (no prompt)."""
    cmd = u.build_remove_command("win32")
    assert "/F" in cmd


def test_remove_command_windows_task_name():
    """Task name must match what install_scheduler registers."""
    cmd = u.build_remove_command("win32")
    cmd_str = " ".join(cmd)
    assert "claude-vault-auto" in cmd_str


def test_remove_command_linux_references_timer():
    """Linux remove command references the timer unit name."""
    cmd = u.build_remove_command("linux")
    cmd_str = " ".join(cmd)
    assert "claude-vault-auto" in cmd_str


def test_remove_command_darwin_references_label():
    """macOS remove command references the launchd label."""
    cmd = u.build_remove_command("darwin")
    cmd_str = " ".join(cmd)
    assert "claude-vault" in cmd_str


def test_remove_command_unknown_platform_raises():
    """Unsupported platform should raise ValueError."""
    try:
        u.build_remove_command("freebsd")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# Data-safety tests for _remove_data_dirs()
# --------------------------------------------------------------------------- #

def _build_fake_home(tmp_path: Path) -> Path:
    """Create a fake ~/.claude tree populated with runtime artifacts + user notes.

    Returns the fake home directory.
    """
    claude = tmp_path / ".claude"

    # Runtime files inside skill dirs (should be deleted)
    (claude / "skills" / "vault-loader").mkdir(parents=True)
    (claude / "skills" / "vault-loader" / "config.json").write_text(
        '{"vault_path": "/tmp/vault"}', encoding="utf-8"
    )
    # An extra non-runtime file that must NOT be deleted
    (claude / "skills" / "vault-loader" / "SKILL.md").write_text(
        "author source", encoding="utf-8"
    )

    (claude / "skills" / "summarize-session").mkdir(parents=True)
    for fname in ("config.json", "auto-queue.jsonl", "summarized-sessions.json", ".auto-paused"):
        (claude / "skills" / "summarize-session" / fname).write_text("runtime", encoding="utf-8")
    auto_runs = claude / "skills" / "summarize-session" / "auto-runs"
    auto_runs.mkdir()
    (auto_runs / "run-2024-01.log").write_text("log data", encoding="utf-8")

    # .vault-loader-disabled marker
    (claude / ".vault-loader-disabled").write_text("", encoding="utf-8")

    # Per-project state file
    proj_dir = claude / "projects" / "abc123deadbeef"
    proj_dir.mkdir(parents=True)
    (proj_dir / "vault-loader-state.json").write_text('{"timestamp":0}', encoding="utf-8")

    # User's knowledge-vault with a note (must NEVER be deleted)
    vault = claude / "knowledge-vault"
    vault.mkdir()
    (vault / "my-note.md").write_text("precious user note", encoding="utf-8")

    return tmp_path


def _patch_module_paths(fake_home: Path) -> None:
    """Redirect all module-level Path constants to the fake home."""
    claude = fake_home / ".claude"
    u._CLAUDE_DIR = claude
    u._SKILL_ROOT_VAULT_LOADER = claude / "skills" / "vault-loader"
    u._SKILL_ROOT_SUMMARIZE = claude / "skills" / "summarize-session"
    u._DEFAULT_VAULT_PATH = claude / "knowledge-vault"
    u._PROJECTS_DIR = claude / "projects"
    u._RUNTIME_AUTO_RUNS_DIR = claude / "skills" / "summarize-session" / "auto-runs"
    u._RUNTIME_FILES = (
        claude / "skills" / "vault-loader" / "config.json",
        claude / "skills" / "summarize-session" / "config.json",
        claude / "skills" / "summarize-session" / "auto-queue.jsonl",
        claude / "skills" / "summarize-session" / "summarized-sessions.json",
        claude / "skills" / "summarize-session" / ".auto-paused",
        claude / ".vault-loader-disabled",
    )


def _restore_module_paths() -> None:
    """Restore module-level Path constants to real home after patching."""
    home = Path.home()
    claude = home / ".claude"
    u._CLAUDE_DIR = claude
    u._SKILL_ROOT_VAULT_LOADER = claude / "skills" / "vault-loader"
    u._SKILL_ROOT_SUMMARIZE = claude / "skills" / "summarize-session"
    u._DEFAULT_VAULT_PATH = claude / "knowledge-vault"
    u._PROJECTS_DIR = claude / "projects"
    u._RUNTIME_AUTO_RUNS_DIR = claude / "skills" / "summarize-session" / "auto-runs"
    u._RUNTIME_FILES = (
        claude / "skills" / "vault-loader" / "config.json",
        claude / "skills" / "summarize-session" / "config.json",
        claude / "skills" / "summarize-session" / "auto-queue.jsonl",
        claude / "skills" / "summarize-session" / "summarized-sessions.json",
        claude / "skills" / "summarize-session" / ".auto-paused",
        claude / ".vault-loader-disabled",
    )


def test_remove_data_dirs_only_removes_runtime_artifacts(tmp_path, capsys):
    """_remove_data_dirs() deletes runtime artifacts and preserves everything else."""
    fake_home = _build_fake_home(tmp_path)
    _patch_module_paths(fake_home)
    try:
        u._remove_data_dirs()
    finally:
        _restore_module_paths()

    claude = fake_home / ".claude"

    # --- Runtime artifacts MUST be gone ---
    assert not (claude / "skills" / "vault-loader" / "config.json").exists(), \
        "vault-loader/config.json should have been removed"
    assert not (claude / "skills" / "summarize-session" / "config.json").exists(), \
        "summarize-session/config.json should have been removed"
    assert not (claude / "skills" / "summarize-session" / "auto-queue.jsonl").exists(), \
        "auto-queue.jsonl should have been removed"
    assert not (claude / "skills" / "summarize-session" / "summarized-sessions.json").exists(), \
        "summarized-sessions.json should have been removed"
    assert not (claude / "skills" / "summarize-session" / ".auto-paused").exists(), \
        ".auto-paused should have been removed"
    assert not (claude / "skills" / "summarize-session" / "auto-runs").exists(), \
        "auto-runs/ subdir should have been removed"
    assert not (claude / ".vault-loader-disabled").exists(), \
        ".vault-loader-disabled should have been removed"
    assert not (claude / "projects" / "abc123deadbeef" / "vault-loader-state.json").exists(), \
        "per-project vault-loader-state.json should have been removed"

    # --- Whole skill dirs MUST still exist (not rmtree'd) ---
    assert (claude / "skills" / "vault-loader").is_dir(), \
        "vault-loader skill dir must NOT be rmtree'd"
    assert (claude / "skills" / "summarize-session").is_dir(), \
        "summarize-session skill dir must NOT be rmtree'd"

    # --- Non-runtime file inside skill dir MUST survive ---
    assert (claude / "skills" / "vault-loader" / "SKILL.md").exists(), \
        "SKILL.md (author source) inside vault-loader must NOT be deleted"

    # --- User's knowledge-vault MUST be fully preserved ---
    assert (claude / "knowledge-vault").is_dir(), \
        "knowledge-vault directory must NOT be deleted"
    assert (claude / "knowledge-vault" / "my-note.md").exists(), \
        "user note inside knowledge-vault must NOT be deleted"

    # --- A message about vault preservation must be printed ---
    captured = capsys.readouterr()
    assert "knowledge vault was NOT removed" in captured.out or \
           "NOT removed" in captured.out, \
        "Should print a message that knowledge-vault was not removed"
