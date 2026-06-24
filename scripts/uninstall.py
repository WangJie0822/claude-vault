"""uninstall.py — cross-platform idempotent timer removal for claude-vault.

Removes the daily scheduled task registered by install_scheduler.py.
"Task / unit / plist not found" is silently ignored — uninstall is idempotent.

Supported platforms:
  win32  — Windows Task Scheduler (schtasks /Delete)
  linux  — systemd user timer (systemctl --user disable/stop + rm unit files)
  darwin — launchd (launchctl unload/bootout + rm plist)

Usage:
  python scripts/uninstall.py [--remove-data]

Options:
  --remove-data   Also delete auto-created vault directory, generated config,
                  state files, auto-queue, run logs and the .vault-loader-disabled
                  marker file.  DEFAULT: off (timer only).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# ---- Names must match install_scheduler.py exactly ---- #
_WIN_TASK_NAME = "claude-vault-auto"
_SYSTEMD_TIMER = "claude-vault-auto.timer"
_SYSTEMD_SERVICE = "claude-vault-auto.service"
_LAUNCHD_LABEL = "com.claude-vault.auto"
_LAUNCHD_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
)

# ---- Systemd unit directory ---- #
_SYSTEMD_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

# ---- Data paths created by the plugin at runtime ---- #
# NOTE: we store individual file/subdir targets — never rmtree a whole skill dir.
# ~/.claude/skills/vault-loader/ and ~/.claude/skills/summarize-session/ may
# contain the plugin author's git-tracked source; only runtime-generated
# artifacts inside them are removed.
_CLAUDE_DIR = Path.home() / ".claude"
_SKILL_ROOT_VAULT_LOADER = _CLAUDE_DIR / "skills" / "vault-loader"
_SKILL_ROOT_SUMMARIZE = _CLAUDE_DIR / "skills" / "summarize-session"
_DEFAULT_VAULT_PATH = _CLAUDE_DIR / "knowledge-vault"
_PROJECTS_DIR = _CLAUDE_DIR / "projects"

# Precise runtime artifacts to remove (file-by-file or single runtime subdir).
# Confirmed from skills/vault-loader/scripts/_state.py: per-project state file
# is named "vault-loader-state.json" under ~/.claude/projects/<cwd-hash>/.
_RUNTIME_FILES: tuple[Path, ...] = (
    _SKILL_ROOT_VAULT_LOADER / "config.json",
    _SKILL_ROOT_SUMMARIZE / "config.json",
    _SKILL_ROOT_SUMMARIZE / "auto-queue.jsonl",
    _SKILL_ROOT_SUMMARIZE / "summarized-sessions.json",
    _SKILL_ROOT_SUMMARIZE / ".auto-paused",
    _CLAUDE_DIR / ".vault-loader-disabled",
)
# Runtime subdirectory (logs only) — rmtree of THIS subdir is safe.
_RUNTIME_AUTO_RUNS_DIR = _SKILL_ROOT_SUMMARIZE / "auto-runs"


# --------------------------------------------------------------------------- #
# build_remove_command — pure, testable, no side effects
# --------------------------------------------------------------------------- #

def build_remove_command(platform: str) -> list[str]:
    """Return the OS command list that removes the daily timer.

    Args:
        platform: sys.platform value — "win32", "linux", or "darwin".

    Returns:
        list[str] command ready for subprocess.run().

    Raises:
        ValueError: if platform is not one of the three supported values.
    """
    if platform == "win32":
        return _build_windows_remove()
    elif platform == "linux":
        return _build_linux_remove()
    elif platform == "darwin":
        return _build_darwin_remove()
    else:
        raise ValueError(
            f"Unsupported platform: {platform!r}. "
            "Expected 'win32', 'linux', or 'darwin'."
        )


def _build_windows_remove() -> list[str]:
    """schtasks /Delete — removes named task, /F suppresses confirmation prompt."""
    return [
        "schtasks",
        "/Delete",
        "/TN", _WIN_TASK_NAME,
        "/F",
    ]


def _build_linux_remove() -> list[str]:
    """systemctl --user disable --now — stops and disables the timer unit."""
    return [
        "systemctl",
        "--user",
        "disable",
        "--now",
        _SYSTEMD_TIMER,
    ]


def _build_darwin_remove() -> list[str]:
    """launchctl unload — deregisters the plist (bootout for newer macOS)."""
    return [
        "launchctl",
        "unload",
        "-w",
        str(_LAUNCHD_PLIST),
    ]


# --------------------------------------------------------------------------- #
# _run_idempotent — execute command, swallow "not found" errors
# --------------------------------------------------------------------------- #

# Substrings in stderr / stdout that indicate "task doesn't exist" — not errors.
_NOT_FOUND_PHRASES = (
    "the system cannot find",       # schtasks Windows
    "does not exist",               # schtasks / launchctl
    "could not find",               # generic
    "failed to disable unit",       # systemd (unit file missing)
    "no such file",                 # launchctl / rm
    "not loaded",                   # launchctl
    "error: 3:",                    # schtasks error code 3 = task not found
    "error: no such file",
)


def _is_not_found(output: str) -> bool:
    """Return True if output indicates the resource simply didn't exist."""
    lower = output.lower()
    return any(phrase in lower for phrase in _NOT_FOUND_PHRASES)


def _run_idempotent(cmd: list[str]) -> int:
    """Run cmd; return 0 if it succeeded OR if the resource wasn't found.

    Uses check=False so a non-zero exit does not raise.  We inspect stderr+stdout
    for "not found" language and treat it as success (idempotent uninstall).
    """
    result = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    combined = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0 or _is_not_found(combined):
        return 0
    # Non-zero, non-"not-found" failure — report but don't raise
    print(
        f"WARNING: command exited {result.returncode}: {' '.join(cmd)}\n"
        f"  stderr: {result.stderr.strip()!r}",
        file=sys.stderr,
    )
    return result.returncode


# --------------------------------------------------------------------------- #
# _remove_linux_unit_files — clean up systemd unit files after disabling
# --------------------------------------------------------------------------- #

def _remove_linux_unit_files() -> None:
    """Delete systemd user unit files written by install_scheduler."""
    for name in (_SYSTEMD_TIMER, _SYSTEMD_SERVICE):
        path = _SYSTEMD_UNIT_DIR / name
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort
    # daemon-reload so systemd forgets the removed units
    if shutil.which("systemctl"):
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


# --------------------------------------------------------------------------- #
# _remove_data_dirs — optional data cleanup
# --------------------------------------------------------------------------- #

def _remove_data_dirs() -> None:
    """Remove ONLY the precise runtime artifacts created by the plugin.

    Cleaned paths (fail-safe / ignore-if-absent):
      ~/.claude/skills/vault-loader/config.json
      ~/.claude/skills/summarize-session/config.json
      ~/.claude/skills/summarize-session/auto-queue.jsonl
      ~/.claude/skills/summarize-session/summarized-sessions.json
      ~/.claude/skills/summarize-session/auto-runs/      (runtime-log subdir only)
      ~/.claude/skills/summarize-session/.auto-paused
      ~/.claude/.vault-loader-disabled
      ~/.claude/projects/*/vault-loader-state.json       (per-project state files)

    NOT removed:
      ~/.claude/skills/vault-loader/    (may contain author's git-tracked source)
      ~/.claude/skills/summarize-session/  (same; only runtime artifacts above deleted)
      ~/.claude/knowledge-vault/           (user's notes — see message printed below)
    """
    # 1. Individual runtime files inside skill dirs (never rmtree the whole dir)
    for path in _RUNTIME_FILES:
        _unlink_safe(path)

    # 2. Runtime auto-runs log subdir (rmtree of this subdir only is safe)
    _rmtree_safe(_RUNTIME_AUTO_RUNS_DIR)

    # 3. Per-project vault-loader state files
    if _PROJECTS_DIR.is_dir():
        for state_file in _PROJECTS_DIR.glob("*/vault-loader-state.json"):
            _unlink_safe(state_file)

    # 4. Knowledge vault: DO NOT delete — it contains the user's notes.
    #    Print a message so the user knows they can remove it manually.
    print(
        f"\nNOTE: Your knowledge vault was NOT removed because it contains your notes:\n"
        f"  {_DEFAULT_VAULT_PATH}\n"
        "If you want to delete it, remove it manually."
    )


def _unlink_safe(path: Path) -> None:
    """Delete a single file; silently ignore missing or permission errors."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _rmtree_safe(path: Path) -> None:
    """Remove a directory tree; silently ignore missing or permission errors."""
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# uninstall() — main entry point
# --------------------------------------------------------------------------- #

def uninstall(remove_data: bool = False) -> int:
    """Remove the daily scheduled task for the current platform.

    Args:
        remove_data: if True, also delete auto-created plugin data directories.

    Returns:
        0 on full success, non-zero if the timer removal failed unexpectedly.
    """
    platform = sys.platform
    rc = 0

    print(f"Removing claude-vault scheduled task (platform: {platform}) ...")

    try:
        cmd = build_remove_command(platform)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Running: {' '.join(cmd)}")
    rc = _run_idempotent(cmd)

    # Linux: also remove unit files after disabling
    if platform == "linux":
        _remove_linux_unit_files()

    # macOS: also remove the plist file after unloading
    if platform == "darwin":
        try:
            _LAUNCHD_PLIST.unlink(missing_ok=True)
        except OSError:
            pass

    if rc == 0:
        print("Scheduled task removed (or was not registered).")
    else:
        print(f"WARNING: timer removal exited with code {rc}.", file=sys.stderr)

    if remove_data:
        print("Removing plugin runtime artifacts ...")
        _remove_data_dirs()
        print("Plugin runtime artifacts removed.")

    return rc


# --------------------------------------------------------------------------- #
# CLI main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Uninstall the claude-vault daily scheduled task. "
            "Safe to run even if the task was never registered."
        )
    )
    parser.add_argument(
        "--remove-data",
        action="store_true",
        default=False,
        help=(
            "Also remove auto-created vault, config, state and log directories. "
            "Default: off (timer removal only)."
        ),
    )
    args = parser.parse_args(argv)
    return uninstall(remove_data=args.remove_data)


if __name__ == "__main__":
    sys.exit(main())
