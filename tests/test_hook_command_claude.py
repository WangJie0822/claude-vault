"""按 Claude Code 的方式执行 `hooks/hooks.json` 里的完整 `command`：核对脚本被找到、
stdin 到达脚本，脚本缺失时仍 exit 0（所有 hook fail-open）。

命令串以 `/bin/sh <wrapper> <脚本>` 执行 wrapper，所以 wrapper 在三个平台上走的都是 sh 段：
Windows 上是 Git 自带的 sh，不再经 cmd.exe。batch 段由 `tests/test_wrapper.py` 直接经 cmd.exe 覆盖。
宿主在 macOS / Linux 上用 `/bin/sh -c` 执行整条命令串；本文件用 bash 执行外层命令串，wrapper
由命令串里的 `/bin/sh` 执行（Debian 系为 dash）。Windows 主机上 dash 由 `test_hook_command_under_dash`
经 WSL 覆盖。

hook 声明 `"shell": "bash"`：没装 Git Bash 的 Windows 不支持（此前同样不能运行，只是
PowerShell 报解析错误，full-review P4A-E1）。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]


def _find_bash() -> str | None:
    """Claude Code 在 Windows 上用的是 Git Bash，不是 PATH 上第一个 bash。

    Windows 常规终端里 PATH 上第一个 bash 是 System32 的 WSL 启动器。所以这里从 git 的
    安装位置往上找 `bin\\bash.exe`（`...\\Git\\cmd\\git.exe` 或 `...\\Git\\mingw64\\bin\\git.exe`），
    找不到就返回 None 让整组用例跳过，绝不退回 PATH 查找。
    """
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    if git:
        for base in list(Path(git).resolve().parents)[:3]:
            cand = base / "bin" / "bash.exe"
            if cand.is_file():
                return str(cand)
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="没有 bash，无法按 Claude Code 的方式执行 command")

_ECHO_SCRIPT = "import sys\nprint('STDIN_REACHED:' + sys.stdin.read())\n"


def test_both_hook_commands_are_collected():
    """参数化列表为空时 pytest 只会静默跳过下面整组用例。"""
    assert len(_commands()) == 2


def _commands() -> list[tuple[str, str]]:
    return [(event, hook["command"]) for event, entries in HOOKS.items()
            for entry in entries for hook in entry["hooks"]]


def _fake_root(base: Path, *, with_scripts: bool = True) -> Path:
    """复制 wrapper；两个入口位置放回显 stdin 的替身脚本。"""
    (base / "hooks").mkdir(parents=True)
    shutil.copy2(ROOT / "hooks" / "run-hook.cmd", base / "hooks" / "run-hook.cmd")
    if with_scripts:
        scripts = base / "skills" / "vault-loader" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("session_start_load.py", "prompt_submit_load.py"):
            (scripts / name).write_text(_ECHO_SCRIPT, encoding="utf-8")
    return base


def _run(command: str, root: Path, env_root: str | None, *,
         cwd: Path | None = None, path: str | None = None) -> subprocess.CompletedProcess:
    cmd = command.replace("${CLAUDE_PLUGIN_ROOT}", root.as_posix())
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("NODEFAULTCURRENTDIRECTORYINEXEPATH", "PATH")}
    env["PATH"] = os.environ.get("PATH", "") if path is None else path
    env.pop("PLUGIN_ROOT", None)
    if env_root is None:
        env.pop("CLAUDE_PLUGIN_ROOT", None)
    else:
        env["CLAUDE_PLUGIN_ROOT"] = env_root
    return subprocess.run([BASH, "-c", cmd], input='{"probe": 1}', capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=60, cwd=cwd)


@needs_bash
@pytest.mark.parametrize("event,command", _commands())
def test_claude_command_reaches_script_with_stdin(tmp_path, event, command):
    root = _fake_root(tmp_path / "plugin")
    r = _run(command, root, str(root))
    assert r.returncode == 0, r.stderr
    assert 'STDIN_REACHED:{"probe": 1}' in r.stdout, (event, r.stdout, r.stderr)


@needs_bash
@pytest.mark.parametrize("event,command", _commands())
def test_claude_command_survives_space_and_cjk_in_root(tmp_path, event, command):
    """Review Focus 4：Windows 用户目录常含空格与中文。"""
    root = _fake_root(tmp_path / "插件 根")
    r = _run(command, root, str(root))
    assert r.returncode == 0, r.stderr
    assert "STDIN_REACHED:" in r.stdout, (event, r.stdout, r.stderr)


@needs_bash
@pytest.mark.parametrize("event,command", _commands())
def test_claude_command_falls_back_to_wrapper_dir_without_env(tmp_path, event, command):
    """Review Focus 5：hook 子进程环境里没有 CLAUDE_PLUGIN_ROOT 时回落到 wrapper 自身位置。"""
    root = _fake_root(tmp_path / "plugin")
    r = _run(command, root, None)
    assert r.returncode == 0, r.stderr
    assert "STDIN_REACHED:" in r.stdout, (event, r.stdout, r.stderr)


@needs_bash
def test_missing_script_still_exits_zero(tmp_path):
    root = _fake_root(tmp_path / "plugin", with_scripts=False)
    _, command = _commands()[0]
    r = _run(command, root, str(root))
    assert r.returncode == 0
    assert "hook script not found" in r.stderr


@pytest.mark.skipif(os.name != "nt", reason="WSL 启动器只出现在 Windows 的 PATH 上")
def test_find_bash_never_picks_the_wsl_launcher(monkeypatch):
    """Windows 常规终端里 PATH 上第一个 bash 是 System32 的 WSL 启动器，
    不是 Git Bash；拿它执行 `D:/...` 路径必然失败，整组用例会误报红。"""
    real_which = shutil.which
    wsl = r"C:\Windows\System32\bash.exe"
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **k: wsl if name == "bash" else real_which(name, *a, **k))
    found = _find_bash()
    if real_which("git") is None:
        assert found is None
    else:
        assert found and "system32" not in found.lower() and Path(found).is_file(), found


@needs_bash
@pytest.mark.parametrize("event,command", _commands())
def test_missing_plugin_root_is_visible_but_non_blocking(tmp_path, event, command):
    """插件根不存在：退出码必须非 0（让 Claude Code 显示 hook 错误）且非 2（2 会拦截提交）。"""
    missing = tmp_path / "no-such-plugin"
    r = _run(command, missing, str(missing))
    assert r.returncode == 1, (event, r.returncode, r.stderr)


_ENV_SCRIPT = ("import os\n"
               "print('VAR=' + str(os.environ.get('NoDefaultCurrentDirectoryInExePath')))\n")


@needs_bash
@pytest.mark.parametrize("event,command", _commands())
def test_wrapper_hardens_child_exe_search(tmp_path, event, command):
    """wrapper 的子进程（Python 脚本及其再起的 git 等）必须带上该变量：Windows 上它让
    CreateProcess 不先搜当前目录；POSIX 上无作用。宿主环境里本来没有它时也要成立。"""
    root = _fake_root(tmp_path / "plugin")
    for name in ("session_start_load.py", "prompt_submit_load.py"):
        (root / "skills" / "vault-loader" / "scripts" / name).write_text(_ENV_SCRIPT, encoding="utf-8")
    r = _run(command, root, str(root))
    assert "VAR=1" in r.stdout, (event, r.stdout, r.stderr)


@needs_bash
@pytest.mark.parametrize("shape", ["clean", "dot_last", "dot_first", "empty_last", "relative_last"])
def test_wrapper_ignores_py_planted_via_relative_path_entry(tmp_path, shape):
    """full-review P2A-M1：PATH 里的空条目、`.` 与相对条目都指向当前目录（不可信仓库）；`py`
    在 macOS / Linux 上通常不存在，所以这类条目排在末尾也会命中仓库里预置的 `py`。
    `clean` 是对照：PATH 里没有这类条目时真脚本照常执行。"""
    root = _fake_root(tmp_path / "plugin")
    evil = tmp_path / "evil"
    (evil / "node_modules" / ".bin").mkdir(parents=True)
    for p in (evil / "py", evil / "node_modules" / ".bin" / "py"):
        p.write_bytes(b"#!/bin/sh\necho PLANTED_RAN\n")
        p.chmod(0o755)
    py_dir = Path(sys.executable).parent.as_posix()
    if os.name == "nt":   # 只放 python.exe 所在目录、不放 py 启动器；Git Bash 认 /c/... 形式
        py_dir = "/" + py_dir[0].lower() + py_dir[2:]
    sysp = "/usr/bin:/bin:" + py_dir
    path = {"clean": sysp, "dot_last": sysp + ":.", "dot_first": ".:" + sysp,
            "empty_last": sysp + ":", "relative_last": sysp + ":./node_modules/.bin"}[shape]
    _, command = _commands()[0]
    r = _run(command, root, str(root), cwd=evil, path=path)
    assert "PLANTED_RAN" not in r.stdout, (shape, r.stdout, r.stderr)
    assert "STDIN_REACHED:" in r.stdout, (shape, r.stdout, r.stderr)


def _wsl_has_dash() -> bool:
    if os.name != "nt" or shutil.which("wsl") is None:
        return False
    try:
        r = subprocess.run(["wsl", "-e", "sh", "-c", "readlink -f /bin/sh; command -v python3"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    lines = r.stdout.split()
    return r.returncode == 0 and len(lines) == 2 and lines[0].endswith("dash")


def _to_wsl(p: Path) -> str:
    s = p.resolve().as_posix()
    return f"/mnt/{s[0].lower()}{s[2:]}"


@pytest.fixture(scope="module")
def wsl_dash():
    """惰性探测：只在这组用例真要跑时才启动 WSL，不在收集期付这笔开销。"""
    if not _wsl_has_dash():
        pytest.skip("没有带 dash 与 python3 的 WSL 发行版")


@pytest.mark.parametrize("event,command", _commands())
@pytest.mark.parametrize("case", ["ok", "no_env", "missing_root", "dot_path"])
def test_hook_command_under_dash(wsl_dash, tmp_path, event, command, case):
    """macOS / Linux 上 Claude Code 用 `/bin/sh -c` 执行命令串，Debian / Ubuntu 的 /bin/sh 是 dash；
    本机（Windows）的 bash 与 Git 自带的 sh 都是 bash，看不到 bashism 与 dash 的退出码
    （full-review T-H1）。WSL 的 `/mnt/<盘符>` 把文件显示为 777，看不到可执行位那类回归——
    那一类由 `test_hooks_json.py` 的命令形态守卫负责。"""
    root = _fake_root(tmp_path / "plugin")
    target = _to_wsl(root) if case != "missing_root" else _to_wsl(tmp_path) + "/no-such-plugin"
    cmd = command.replace("${CLAUDE_PLUGIN_ROOT}", target)
    envs = [] if case == "no_env" else [f"CLAUDE_PLUGIN_ROOT={target}"]
    path = "/usr/bin:/bin"
    if case == "dot_path":
        evil = tmp_path / "evil"
        evil.mkdir()
        (evil / "py").write_bytes(b"#!/bin/sh\necho PLANTED_RAN\n")
        path += ":."
        cmd = f"cd {shlex.quote(_to_wsl(evil))} && {cmd}"
    r = subprocess.run(["wsl", "-e", "env", "-i", f"PATH={path}", "HOME=/tmp", *envs, "/bin/sh", "-c", cmd],
                       input='{"probe": 1}', capture_output=True, text=True, encoding="utf-8", timeout=120)
    if case == "missing_root":
        assert r.returncode == 1, (event, r.returncode, r.stderr)
    else:
        assert r.returncode == 0, (event, case, r.returncode, r.stderr)
        assert 'STDIN_REACHED:{"probe": 1}' in r.stdout, (event, case, r.stdout, r.stderr)
        assert "PLANTED_RAN" not in r.stdout, (event, case, r.stdout)
