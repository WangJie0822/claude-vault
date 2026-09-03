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


def test_plugin_root_takes_precedence(tmp_path):
    """两个候选根都含该脚本时，PLUGIN_ROOT 优先。"""
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    codex_root.mkdir()
    claude_root.mkdir()
    (codex_root / "probe.py").write_text("print('CODEX_ROOT')\n", encoding="utf-8")
    (claude_root / "probe.py").write_text("print('CLAUDE_ROOT')\n", encoding="utf-8")
    env = {**os.environ, "PLUGIN_ROOT": str(codex_root),
           "CLAUDE_PLUGIN_ROOT": str(claude_root)}
    if os.name == "nt":
        result = _run_cmd(["probe.py"], env)
        assert result.returncode == 0
        assert b"CODEX_ROOT" in result.stdout
        assert b"CLAUDE_ROOT" not in result.stdout
    result_sh = _run_sh(["probe.py"], env)
    if result_sh is not None:
        assert result_sh.returncode == 0
        assert b"CODEX_ROOT" in result_sh.stdout
        assert b"CLAUDE_ROOT" not in result_sh.stdout


def test_unrelated_plugin_root_falls_back_instead_of_dying_silently(tmp_path):
    """`PLUGIN_ROOT` 指向不含该脚本的目录时必须回落，而不是静默失效。

    `PLUGIN_ROOT` 是个极通用的变量名：任何第三方工具或 shell profile 导出它，都会
    顶掉宿主自己给的 `CLAUDE_PLUGIN_ROOT`。旧写法「取第一个非空变量」选中它之后
    **不再回落**，脚本找不到即 exit 0 —— 零输出、零告警，整个插件对该用户彻底不
    工作，且与「这轮本来就没有相关笔记」完全不可区分。

    上一条用例只覆盖「两个根里都有脚本」，恰好绕过这个危险分支。
    """
    unrelated = tmp_path / "unrelated"      # 别的工具导出的 PLUGIN_ROOT
    claude_root = tmp_path / "claude"
    unrelated.mkdir()
    claude_root.mkdir()
    (claude_root / "probe.py").write_text("print('CLAUDE_ROOT')\n", encoding="utf-8")
    env = {**os.environ, "PLUGIN_ROOT": str(unrelated),
           "CLAUDE_PLUGIN_ROOT": str(claude_root)}
    if os.name == "nt":
        result = _run_cmd(["probe.py"], env)
        assert result.returncode == 0
        assert b"CLAUDE_ROOT" in result.stdout, \
            f"未回落到 CLAUDE_PLUGIN_ROOT，插件静默失效：stdout={result.stdout!r}"
    result_sh = _run_sh(["probe.py"], env)
    if result_sh is not None:
        assert result_sh.returncode == 0
        assert b"CLAUDE_ROOT" in result_sh.stdout, \
            f"未回落到 CLAUDE_PLUGIN_ROOT，插件静默失效：stdout={result_sh.stdout!r}"


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
