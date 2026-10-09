# tests/test_wrapper.py
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

import pytest

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


def test_plugin_root_is_never_a_candidate(tmp_path):
    """2.0.0 起 PLUGIN_ROOT 不是候选根：它是任何工具都可能导出的通用变量名，而 2.0.0 起
    没有受支持的宿主会为本插件设置它。即使那里有同名脚本也不得执行。"""
    stale_root = tmp_path / "stale"
    claude_root = tmp_path / "claude"
    stale_root.mkdir()
    claude_root.mkdir()
    (stale_root / "probe.py").write_text("print('STALE_ROOT')\n", encoding="utf-8")
    (claude_root / "probe.py").write_text("print('CLAUDE_ROOT')\n", encoding="utf-8")
    env = {**os.environ, "PLUGIN_ROOT": str(stale_root),
           "CLAUDE_PLUGIN_ROOT": str(claude_root)}
    if os.name == "nt":
        result = _run_cmd(["probe.py"], env)
        assert result.returncode == 0
        assert b"CLAUDE_ROOT" in result.stdout
        assert b"STALE_ROOT" not in result.stdout
    result_sh = _run_sh(["probe.py"], env)
    if result_sh is not None:
        assert result_sh.returncode == 0
        assert b"CLAUDE_ROOT" in result_sh.stdout
        assert b"STALE_ROOT" not in result_sh.stdout


def test_plugin_root_alone_is_ignored(tmp_path):
    """只有 PLUGIN_ROOT 指向含该脚本的目录时同样不执行；回落后找不到就在 stderr 出声。"""
    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    (stale_root / "probe_only_here.py").write_text("print('STALE_ROOT')\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["PLUGIN_ROOT"] = str(stale_root)
    if os.name == "nt":
        result = _run_cmd(["probe_only_here.py"], env)
        assert result.returncode == 0
        assert b"STALE_ROOT" not in result.stdout
        assert b"hook script not found" in result.stderr
    result_sh = _run_sh(["probe_only_here.py"], env)
    if result_sh is not None:
        assert result_sh.returncode == 0
        assert b"STALE_ROOT" not in result_sh.stdout
        assert b"hook script not found" in result_sh.stderr


def _copied_plugin(base: Path) -> Path:
    """拷一份 wrapper 到临时插件根，探针脚本只放在这个插件根里。"""
    (base / "hooks").mkdir(parents=True)
    shutil.copy2(WRAPPER, base / "hooks" / "run-hook.cmd")
    (base / "probe.py").write_text("print('SELF_ROOT_RAN')\n", encoding="utf-8")
    return base / "hooks" / "run-hook.cmd"


def _stale_claude_root_env(tmp_path: Path) -> dict:
    stale = tmp_path / "stale"
    stale.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")}
    env["CLAUDE_PLUGIN_ROOT"] = str(stale)
    return env


# full-review P3A-M2：`CLAUDE_PLUGIN_ROOT` 非空却不含该脚本时，必须回落到 wrapper 自身所在的
# 插件根。择根规则是「取第一个含有该脚本的候选根」，不是「取第一个非空变量」；按后者写，
# 脚本找不到即 exit 0——零输出、零告警，整个插件对该用户不工作，且与「这轮本来就没有
# 相关笔记」不可区分。上面几条用例的 CLAUDE_PLUGIN_ROOT 要么含脚本、要么不存在，都走不到
# 这个回落分支。
@pytest.mark.skipif(os.name != "nt", reason="batch 段只在 Windows 上经 cmd.exe 执行")
def test_stale_claude_plugin_root_falls_back_to_wrapper_dir_batch(tmp_path):
    wrapper = _copied_plugin(tmp_path / "plugin")
    r = subprocess.run([str(wrapper), "probe.py"], capture_output=True,
                       env=_stale_claude_root_env(tmp_path), timeout=60)
    assert r.returncode == 0, r.stderr
    assert b"SELF_ROOT_RAN" in r.stdout, f"未回落到 wrapper 所在插件根：stderr={r.stderr!r}"


def test_stale_claude_plugin_root_falls_back_to_wrapper_dir_sh(tmp_path):
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("没有 sh")
    wrapper = _copied_plugin(tmp_path / "plugin")
    r = subprocess.run([sh, str(wrapper), "probe.py"], capture_output=True,
                       env=_stale_claude_root_env(tmp_path), timeout=60)
    assert r.returncode == 0, r.stderr
    assert b"SELF_ROOT_RAN" in r.stdout, f"未回落到 wrapper 所在插件根：stderr={r.stderr!r}"


def test_missing_script_is_reported_on_stderr(tmp_path):
    """所有候选根都不含该脚本时仍 exit 0（fail-open），但必须在 stderr 出声。

    完全静默是最难排查的失败形态——用户看到的只是「插件不工作」，没有任何线索。
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {**os.environ, "PLUGIN_ROOT": str(empty), "CLAUDE_PLUGIN_ROOT": str(empty)}
    if os.name == "nt":
        result = _run_cmd(["nope.py"], env)
        assert result.returncode == 0, "缺脚本不得阻断会话"
        assert b"hook script not found" in result.stderr, \
            f"应在 stderr 出声：stderr={result.stderr!r}"
    result_sh = _run_sh(["nope.py"], env)
    if result_sh is not None:
        assert result_sh.returncode == 0
        assert b"hook script not found" in result_sh.stderr


def test_batch_section_is_pure_ascii():
    """batch 段必须纯 ASCII。

    cmd.exe 按 OEM 代码页读取 `.cmd`，UTF-8 的非 ASCII 注释会乱码成命令被执行
    （实测：报 `'xxx' is not recognized as an internal or external command`），
    还会把 `@echo off` 一起弄失效、把整段路径解析顶歪。本条钉的是这个约束——
    sh 段同样受益，因为 cmd 解析错位时可能越界读到它。
    """
    raw = WRAPPER.read_bytes()
    batch = raw.split(b"\nBATCH\n", 1)[0]
    bad = [(i, b) for i, b in enumerate(batch) if b > 127]
    assert not bad, f"batch 段含非 ASCII 字节（偏移, 值）: {bad[:5]}"
