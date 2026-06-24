# tests/test_wrapper.py
import os, shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

WRAPPER = ROOT / "hooks/run-hook.cmd"


def _run_cmd(args: list[str], env: dict) -> subprocess.CompletedProcess:
    """Invoke run-hook.cmd via cmd.exe (Windows native path)."""
    return subprocess.run([str(WRAPPER)] + args, capture_output=True, env=env)


def _run_sh(args: list[str], env: dict):
    """Invoke run-hook.cmd via sh (polyglot sh path). Returns None if sh unavailable."""
    sh = shutil.which("sh")
    if sh is None:
        return None
    return subprocess.run([sh, str(WRAPPER)] + args, capture_output=True, env=env)


def test_wrapper_missing_script_exits_zero():
    """Missing script: wrapper must exit 0 (fail-open) on both cmd.exe and sh paths."""
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT)}
    args = ["hooks/does_not_exist.py"]

    if os.name == "nt":
        r = _run_cmd(args, env)
        assert r.returncode == 0, f"cmd.exe: exit {r.returncode}, stderr={r.stderr!r}"

    # Polyglot sh path — works on both Windows (Git Bash) and Unix
    r_sh = _run_sh(args, env)
    if r_sh is not None:
        assert r_sh.returncode == 0, f"sh: exit {r_sh.returncode}, stderr={r_sh.stderr!r}"


def test_child_nonzero_exits_zero(tmp_path):
    """Wrapper must exit 0 even when child script exits 1 (fail-open)."""
    fake_root = tmp_path / "plugin_root"
    fake_root.mkdir()
    (fake_root / "exit1.py").write_text("import sys; sys.exit(1)\n", encoding="utf-8")
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(fake_root)}
    args = ["exit1.py"]

    if os.name == "nt":
        r = _run_cmd(args, env)
        assert r.returncode == 0, (
            f"cmd.exe: Wrapper exited {r.returncode} — fail-open violated.\n"
            f"stdout: {r.stdout!r}\nstderr: {r.stderr!r}"
        )

    # Polyglot sh path
    r_sh = _run_sh(args, env)
    if r_sh is not None:
        assert r_sh.returncode == 0, (
            f"sh: Wrapper exited {r_sh.returncode} — fail-open violated.\n"
            f"stdout: {r_sh.stdout!r}\nstderr: {r_sh.stderr!r}"
        )


def test_real_script_runs_stdout_passes(tmp_path):
    """A real script that prints to stdout: output must reach caller (both paths)."""
    fake_root = tmp_path / "plugin_root"
    fake_root.mkdir()
    (fake_root / "hello.py").write_text("print('HELLO_FROM_HOOK')\n", encoding="utf-8")
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(fake_root)}
    args = ["hello.py"]

    if os.name == "nt":
        r = _run_cmd(args, env)
        assert r.returncode == 0, f"cmd.exe exit={r.returncode}"
        assert b"HELLO_FROM_HOOK" in r.stdout, f"cmd.exe stdout: {r.stdout!r}"

    # Polyglot sh path
    r_sh = _run_sh(args, env)
    if r_sh is not None:
        assert r_sh.returncode == 0, f"sh exit={r_sh.returncode}"
        assert b"HELLO_FROM_HOOK" in r_sh.stdout, f"sh stdout: {r_sh.stdout!r}"
