"""tests/test_install_scheduler.py

Tests for install_scheduler.build_command() — pure command construction,
no actual registration happens.

Also tests _write_linux_units() and _write_launchd_plist() to verify
run_auto_summary.py appears in the generated unit/plist files.
"""
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import install_scheduler as s


def test_build_command_windows():
    cmd = s.build_command("win32", when="02:30")
    assert "schtasks" in cmd[0] and "/Create" in cmd
    assert "run_auto_summary.py" in " ".join(cmd)


def test_build_command_linux_with_systemctl(monkeypatch):
    """build_command for linux returns systemctl command when systemctl present."""
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/systemctl" if x == "systemctl" else None)
    cmd = s.build_command("linux", when="02:30")
    assert "systemctl" in " ".join(cmd)
    assert "run_auto_summary.py" in " ".join(cmd)


def test_build_command_linux_no_systemctl_raises(monkeypatch):
    """build_command for linux raises RuntimeError when systemctl absent."""
    monkeypatch.setattr(shutil, "which", lambda x: None)
    with pytest.raises(RuntimeError, match="systemd is not available"):
        s.build_command("linux", when="02:30")


def test_build_command_macos():
    cmd = s.build_command("darwin", when="02:30")
    assert "launchctl" in " ".join(cmd) or "plist" in " ".join(cmd).lower()


# ---- additional shape assertions ----

def test_build_command_windows_task_name():
    cmd = s.build_command("win32", when="03:00")
    cmd_str = " ".join(cmd)
    assert "claude-vault-auto" in cmd_str
    assert "/ST" in cmd


def test_build_command_windows_daily():
    cmd = s.build_command("win32", when="02:30")
    assert "/SC" in cmd and "DAILY" in cmd


def test_build_command_windows_time():
    cmd = s.build_command("win32", when="04:15")
    # time value must appear after /ST
    idx = cmd.index("/ST")
    assert cmd[idx + 1] == "04:15"


def test_build_command_linux_references_script(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/systemctl" if x == "systemctl" else None)
    cmd = s.build_command("linux", when="02:30")
    assert "run_auto_summary.py" in " ".join(cmd)


def test_build_command_macos_references_script():
    cmd = s.build_command("darwin", when="02:30")
    assert "run_auto_summary.py" in " ".join(cmd)


def test_build_command_unknown_platform_raises():
    try:
        s.build_command("freebsd", when="02:30")
        assert False, "Should have raised"
    except ValueError:
        pass


# ---- unit-file content tests ----

def test_write_linux_units_contains_script(tmp_path, monkeypatch):
    """_write_linux_units writes ExecStart referencing run_auto_summary.py."""
    # Suppress actual daemon-reload call
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)

    py = "/usr/bin/python3"
    script = "/home/user/scripts/run_auto_summary.py"
    s._write_linux_units(py, script, "02:30", unit_dir=tmp_path)

    service_file = tmp_path / s._SYSTEMD_SERVICE
    assert service_file.exists(), "Service unit file not written"
    service_text = service_file.read_text(encoding="utf-8")
    assert "run_auto_summary.py" in service_text, \
        f"run_auto_summary.py not found in ExecStart:\n{service_text}"
    assert f"ExecStart={py} {script}" in service_text

    timer_file = tmp_path / s._SYSTEMD_TIMER
    assert timer_file.exists(), "Timer unit file not written"
    timer_text = timer_file.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 02:30:00" in timer_text


def test_write_launchd_plist_contains_script(tmp_path):
    """_write_launchd_plist writes ProgramArguments containing run_auto_summary.py."""
    py = "/usr/bin/python3"
    script = "/Users/user/scripts/run_auto_summary.py"
    plist_path = tmp_path / "com.claude-vault.auto.plist"
    s._write_launchd_plist(py, script, "03:15", plist_path=plist_path)

    assert plist_path.exists(), "Plist file not written"
    plist_text = plist_path.read_text(encoding="utf-8")
    assert "run_auto_summary.py" in plist_text, \
        f"run_auto_summary.py not found in ProgramArguments:\n{plist_text}"
    assert f"<string>{script}</string>" in plist_text
    # Verify time is encoded correctly
    assert "<integer>3</integer>" in plist_text   # Hour
    assert "<integer>15</integer>" in plist_text  # Minute
