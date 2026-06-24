"""install_scheduler.py — cross-platform daily timer installer (opt-in).

Registers a daily scheduled task that calls run_auto_summary.py.

Supported platforms:
  win32  — Windows Task Scheduler (schtasks)
  linux  — systemd user timer unit (or crontab fallback)
  darwin — launchd plist + launchctl load

Usage:
  python scripts/install_scheduler.py [--when HH:MM] [--yes]

The installer prints a RISK WARNING and requires confirmation before
registering anything, unless --yes is passed.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Path to run_auto_summary.py (sibling in scripts/)
_SCRIPT_DIR = Path(__file__).resolve().parent
RUN_SCRIPT = _SCRIPT_DIR / "run_auto_summary.py"

# launchd plist label
_LAUNCHD_LABEL = "com.claude-vault.auto"
_LAUNCHD_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
)

# Windows task name
_WIN_TASK_NAME = "claude-vault-auto"

# systemd service/timer names
_SYSTEMD_SERVICE = "claude-vault-auto.service"
_SYSTEMD_TIMER = "claude-vault-auto.timer"

# Risk warning text — shown before any registration
RISK_WARNING = """\
=== RISK WARNING ===
This will register a daily scheduled task that:
  1. Calls the paid `claude` CLI — consumes API credits on each run.
  2. Reads session transcripts and sends them to an LLM (Claude API).
  3. Executes `git commit` in your Vault directory.
The task runs automatically (no user interaction) at the scheduled time.
====================
"""


# --------------------------------------------------------------------------- #
# build_command — pure, testable, no side effects
# --------------------------------------------------------------------------- #

def build_command(platform: str, when: str = "02:30") -> list[str]:
    """Return the OS command list that registers the daily timer.

    Args:
        platform: sys.platform value — "win32", "linux", or "darwin".
        when:     HH:MM time string for daily execution.

    Returns:
        list[str] command ready for subprocess.run().

    Raises:
        ValueError: if platform is not one of the three supported values.
    """
    py = _python_exe()
    script = str(RUN_SCRIPT)

    if platform == "win32":
        return _build_windows(py, script, when)
    elif platform == "linux":
        return _build_linux(py, script, when)
    elif platform == "darwin":
        return _build_darwin(py, script, when)
    else:
        raise ValueError(
            f"Unsupported platform: {platform!r}. "
            "Expected 'win32', 'linux', or 'darwin'."
        )


def _python_exe() -> str:
    """Return the absolute path to the current Python interpreter."""
    return sys.executable or "python3"


def _build_windows(py: str, script: str, when: str) -> list[str]:
    """schtasks /Create — runs daily at `when`."""
    tr = f'"{py}" "{script}"'
    return [
        "schtasks",
        "/Create",
        "/SC", "DAILY",
        "/TN", _WIN_TASK_NAME,
        "/TR", tr,
        "/ST", when,
        "/F",          # overwrite existing task without prompt
    ]


def _build_linux(py: str, script: str, when: str) -> list[str]:
    """systemd user timer (preferred); no-systemd raises a clear error.

    When systemctl is present, returns the systemctl command with informational
    "--", py, script entries appended (stripped before execution in install()).

    Format (systemctl path):
      ["systemctl", "--user", "enable", "--now", <timer>, "--", <py>, <script>]
    The extra "--", py, script entries are informational (build_command contract)
    and are stripped before actual subprocess execution in install().

    When systemctl is absent, raises RuntimeError with instructions for the user.
    """
    if shutil.which("systemctl"):
        return [
            "systemctl",
            "--user",
            "enable",
            "--now",
            _SYSTEMD_TIMER,
            # informational references (stripped in install before exec)
            "--",
            py,
            script,
        ]
    else:
        hour, minute = when.split(":")
        cron_line = f"{minute} {hour} * * * {py} {script}"
        raise RuntimeError(
            "systemd is not available on this system.\n"
            "To schedule the task manually, add the following line to your crontab"
            " (run `crontab -e`):\n"
            f"  {cron_line}"
        )


def _build_darwin(py: str, script: str, when: str) -> list[str]:
    """launchd — write plist then load it.

    Returns 'launchctl load <plist_path> -- <py> <script>' command.
    The extra "--", py, script entries are informational (build_command contract)
    and are stripped before actual subprocess execution in install().
    """
    return [
        "launchctl",
        "load",
        "-w",
        str(_LAUNCHD_PLIST),
        # informational references (stripped in install before exec)
        "--",
        py,
        script,
    ]


# --------------------------------------------------------------------------- #
# Platform-specific helper: write unit files before executing command
# --------------------------------------------------------------------------- #

def _write_linux_units(
    py: str,
    script: str,
    when: str,
    unit_dir: Path | None = None,
) -> None:
    """Write systemd user unit files to ~/.config/systemd/user/.

    Only called when systemctl is available (guarded in install()).

    Args:
        py:       Path to the Python interpreter.
        script:   Path to run_auto_summary.py.
        when:     HH:MM time string.
        unit_dir: Override output directory (for testing).
    """
    import subprocess

    if unit_dir is None:
        unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)

    hour, minute = when.split(":")

    service_content = f"""\
[Unit]
Description=Claude Vault auto-summarize session

[Service]
Type=oneshot
ExecStart={py} {script}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""

    timer_content = f"""\
[Unit]
Description=Claude Vault auto-summarize daily timer

[Timer]
OnCalendar=*-*-* {hour}:{minute}:00
Persistent=true

[Install]
WantedBy=timers.target
"""

    (unit_dir / _SYSTEMD_SERVICE).write_text(service_content, encoding="utf-8")
    (unit_dir / _SYSTEMD_TIMER).write_text(timer_content, encoding="utf-8")

    # reload daemon so systemd picks up the new units (non-fatal if it fails)
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        check=False,
    )


def _write_launchd_plist(
    py: str,
    script: str,
    when: str,
    plist_path: Path | None = None,
) -> None:
    """Write the launchd plist file to ~/Library/LaunchAgents/.

    Args:
        py:         Path to the Python interpreter.
        script:     Path to run_auto_summary.py.
        when:       HH:MM time string.
        plist_path: Override output plist path (for testing).
    """
    hour, minute = when.split(":")

    plist_content = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>{script}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{int(hour)}</integer>
        <key>Minute</key>
        <integer>{int(minute)}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{Path.home()}/Library/Logs/claude-vault-auto.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/Library/Logs/claude-vault-auto-err.log</string>
</dict>
</plist>
"""
    target = plist_path if plist_path is not None else _LAUNCHD_PLIST
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(plist_content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# install() — interactive entry point
# --------------------------------------------------------------------------- #

def install(when: str = "02:30", assume_yes: bool = False) -> int:
    """Register the daily timer for the current platform.

    Args:
        when:       HH:MM time for daily execution.
        assume_yes: if True, skip interactive confirmation.

    Returns:
        0 on success, non-zero on failure.
    """
    import subprocess

    platform = sys.platform

    # Print risk warning
    print(RISK_WARNING)
    print(f"Platform : {platform}")
    print(f"Time     : {when} daily")
    print(f"Script   : {RUN_SCRIPT}")
    print()

    if not assume_yes:
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    py = _python_exe()
    script = str(RUN_SCRIPT)

    try:
        if platform == "linux" and shutil.which("systemctl"):
            _write_linux_units(py, script, when)
        elif platform == "darwin":
            _write_launchd_plist(py, script, when)
        # win32: schtasks /TR embeds the command inline, no extra file needed

        cmd = build_command(platform, when)
        # Strip trailing informational "--" + py + script args (linux/darwin)
        exec_cmd = cmd[:cmd.index("--")] if "--" in cmd else cmd
        print(f"Running: {' '.join(exec_cmd)}")
        result = subprocess.run(exec_cmd, check=False)
        if result.returncode != 0:
            print(f"ERROR: command exited with code {result.returncode}", file=sys.stderr)
            return result.returncode

        print("Scheduler registered successfully.")
        return 0

    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: unexpected failure: {exc}", file=sys.stderr)
        return 3


# --------------------------------------------------------------------------- #
# CLI main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Register a daily scheduled task that auto-summarizes Claude sessions."
    )
    parser.add_argument(
        "--when",
        default="02:30",
        metavar="HH:MM",
        help="Daily execution time in HH:MM (default: 02:30)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip interactive confirmation (non-interactive / CI use)",
    )
    args = parser.parse_args(argv)
    return install(when=args.when, assume_yes=args.yes)


if __name__ == "__main__":
    sys.exit(main())
