"""Windows 上按程序名起子进程时不从当前目录解析（CWE-427）的两个 helper。"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_vault import exe_search as es  # noqa: E402

_VAR = "NoDefaultCurrentDirectoryInExePath"
win_only = pytest.mark.skipif(sys.platform != "win32", reason="当前目录优先搜索只在 Windows 上存在")


def _strip_var(monkeypatch):
    """先 setenv 再 delenv：变量原本不存在时，单独 delenv(raising=False) 不登记复原，
    harden_exe_search 随后写进去的值会泄漏到整个 pytest 会话（full-review P1-L7）。"""
    monkeypatch.setenv(_VAR, "x")
    monkeypatch.delenv(_VAR)


def _plant(d: Path, name: str = "claude.cmd") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"@echo off\r\n")
    return p


def _py311_which(name, *a, **k):
    """还原 Python 3.9–3.11 的 shutil.which：Windows 上无条件先搜当前目录、不认该变量。"""
    return os.path.join(os.curdir, name + ".CMD")


def test_harden_sets_var_on_windows(monkeypatch):
    _strip_var(monkeypatch)
    monkeypatch.setattr(es.sys, "platform", "win32")
    es.harden_exe_search()
    assert os.environ.get(_VAR) == "1"


def test_harden_keeps_existing_value(monkeypatch):
    """Windows 只看变量是否存在、不看取值，所以不覆盖调用方的值。"""
    _strip_var(monkeypatch)
    monkeypatch.setenv(_VAR, "0")
    monkeypatch.setattr(es.sys, "platform", "win32")
    es.harden_exe_search()
    assert os.environ.get(_VAR) == "0"


def test_harden_noop_off_windows(monkeypatch):
    _strip_var(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")       # 宿主 PATH 是 Windows 形式，不能交给 POSIX 分支
    monkeypatch.setattr(es.sys, "platform", "linux")
    es.harden_exe_search()
    assert os.environ.get(_VAR) is None
    assert os.environ["PATH"] == "/usr/bin:/bin"


def test_posix_safe_path_drops_empty_and_relative_entries():
    """POSIX 上空条目与 `.` 表示当前目录，相对条目相对当前目录（full-review P2A-M1）。"""
    assert es._posix_safe_path(":/usr/bin::./node_modules/.bin:/bin:.:rel/x:") == "/usr/bin:/bin"


def test_posix_safe_path_keeps_order_and_falls_back_when_nothing_left():
    assert es._posix_safe_path("/opt/b:/usr/bin") == "/opt/b:/usr/bin"
    assert es._posix_safe_path(".:rel") == "/usr/bin:/bin"
    assert es._posix_safe_path("") == "/usr/bin:/bin"


def test_harden_filters_path_off_windows(monkeypatch):
    _strip_var(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin:.:/bin:")
    monkeypatch.setattr(es.sys, "platform", "linux")
    es.harden_exe_search()
    assert os.environ["PATH"] == "/usr/bin:/bin"


def test_which_no_cwd_is_plain_which_off_windows(monkeypatch):
    monkeypatch.setattr(es.sys, "platform", "linux")
    monkeypatch.setattr(es.shutil, "which", lambda n, *a, **k: "./" + n)
    assert es.which_no_cwd("claude") == "./claude"


@win_only
def test_which_no_cwd_skips_cwd_hit_like_py311(tmp_path, monkeypatch):
    evil, good = tmp_path / "evil", tmp_path / "good"
    _plant(evil)
    real = _plant(good)
    monkeypatch.chdir(evil)
    monkeypatch.setenv("PATH", str(good))
    monkeypatch.setattr(es.shutil, "which", _py311_which)
    assert os.path.normcase(es.which_no_cwd("claude")) == os.path.normcase(str(real))


@win_only
def test_which_no_cwd_returns_none_when_only_cwd_has_it(tmp_path, monkeypatch):
    evil = tmp_path / "evil"
    _plant(evil)
    monkeypatch.chdir(evil)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(es.shutil, "which", _py311_which)
    assert es.which_no_cwd("claude") is None


@win_only
def test_which_no_cwd_with_real_which_and_no_var(tmp_path, monkeypatch):
    """不打桩：变量缺席时 3.12+ 的 which 同样先搜当前目录。"""
    _strip_var(monkeypatch)
    evil, good = tmp_path / "evil", tmp_path / "good"
    _plant(evil)
    real = _plant(good)
    monkeypatch.chdir(evil)
    monkeypatch.setenv("PATH", str(good))
    assert os.path.normcase(es.which_no_cwd("claude")) == os.path.normcase(str(real))


@win_only
def test_which_no_cwd_keeps_cwd_when_cwd_is_on_path(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    real = _plant(d)
    monkeypatch.chdir(d)
    monkeypatch.setenv("PATH", str(d))
    assert os.path.normcase(es.which_no_cwd("claude")) == os.path.normcase(str(real))


@win_only
def test_which_no_cwd_skips_relative_path_entries(tmp_path, monkeypatch):
    _plant(tmp_path / "evil")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["evil", ""]))
    assert es.which_no_cwd("claude") is None


@win_only
def test_which_no_cwd_accepts_rooted_result(tmp_path, monkeypatch):
    """`\\tools\\x` 相对的是盘符根而不是当前目录；Python 3.13 起 ntpath.isabs 对它返回 False，
    不能因此当成当前目录里的结果。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(es.shutil, "which", lambda n, *a, **k: "\\tools\\" + n + ".CMD")
    assert es.which_no_cwd("claude") == "\\tools\\claude.CMD"


def _defs(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [ast.dump(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))]


def test_two_copies_are_identical():
    """summarize-session 的脚本不保证能导入 context_vault，所以有两份；改一份必须同步另一份。"""
    a = _defs(ROOT / "context_vault" / "exe_search.py")
    b = _defs(ROOT / "skills" / "summarize-session" / "scripts" / "_exe_search.py")
    assert a and a == b


_BARE_WHICH_ALLOWED = {
    ("context_vault/exe_search.py", "which_no_cwd"): "实现本身",
    ("skills/summarize-session/scripts/_exe_search.py", "which_no_cwd"): "实现本身",
    ("skills/summarize-session/scripts/obsidian_cli.py", "probe"):
        "tasklist / pgrep 只判存在，按名执行走 CreateProcess（受 harden_exe_search 约束）",
    ("skills/vault-loader/scripts/_topic.py", "_call_model"):
        "提炼子进程的 cwd 固定为插件目录（spawn_topic_extraction），且不得导入 context_vault",
    ("skills/vault-loader/scripts/_topic.py", "spawn_topic_extraction"): "只判 claude 是否存在，结果不被执行",
}


def test_bare_shutil_which_only_at_reviewed_sites():
    """「which 后执行」一律用 which_no_cwd；每处裸 shutil.which 都必须登记理由（full-review P1-M1）。
    用集合相等：新增调用点、或白名单里留着已删除的条目，都会转红。"""
    if shutil.which("git") is None:
        pytest.skip("没有 git，无法按跟踪清单枚举")
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"], capture_output=True,
                           text=True, encoding="utf-8", check=True).stdout.split()
    if not files:
        pytest.skip("git ls-files 为空（不是本仓库自己的 checkout）")
    found = set()
    for rel in files:
        if rel.startswith(("tests/", "packaging/")) or "/tests/" in rel:
            continue
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    isinstance(c, ast.Call) and ast.unparse(c.func) == "shutil.which" for c in ast.walk(fn)):
                found.add((rel, fn.name))
    assert found == set(_BARE_WHICH_ALLOWED), found ^ set(_BARE_WHICH_ALLOWED)


SS_SCRIPTS = ROOT / "skills" / "summarize-session" / "scripts"
HOOK_ENTRIES = [ROOT / "skills" / "vault-loader" / "scripts" / n
                for n in ("session_start_load.py", "prompt_submit_load.py")]


def _main_block(path: Path):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.If) and ast.unparse(node.test).replace("'", '"') == '__name__ == "__main__"':
            return node
    return None


def _first_stmt_calling(block, names) -> int | None:
    for i, stmt in enumerate(block.body):
        calls = {ast.unparse(c.func) for c in ast.walk(stmt) if isinstance(c, ast.Call)}
        if calls & names:
            return i
    return None


def _entry_scripts() -> list[Path]:
    found = [p for p in sorted(SS_SCRIPTS.glob("*.py")) if _main_block(p) is not None]
    return found + HOOK_ENTRIES


def test_entry_script_discovery_is_not_empty():
    """下界钉死：发现逻辑一旦失效（零命中），下面的参数化会静默跳过。"""
    assert len(_entry_scripts()) >= 15


@pytest.mark.parametrize("path", _entry_scripts(), ids=lambda p: p.name)
def test_main_block_hardens_before_main(path):
    """在 `__main__` 块而不是 `main()` 里调：测试直接调 `main()` 时不改写 pytest 进程环境。"""
    block = _main_block(path)
    h = _first_stmt_calling(block, {"harden_exe_search"})
    m = _first_stmt_calling(block, {"main", "run_cli"})
    assert h is not None and m is not None and h < m, path.name


def _prefix_source(path: Path) -> str:
    """`__main__` 块里调 main / run_cli 之前的那几条语句。"""
    body = []
    for stmt in _main_block(path).body:
        calls = {ast.unparse(c.func) for c in ast.walk(stmt) if isinstance(c, ast.Call)}
        if calls & {"main", "run_cli"}:
            break
        body.append(stmt)
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


@pytest.mark.parametrize("path", _entry_scripts(), ids=lambda p: p.name)
def test_main_block_prefix_really_hardens(path, tmp_path):
    """真正执行 `__main__` 块的前缀（sys.path 按生产形态给出）：上一条 AST 守卫只看调用名，
    import 写错时它照绿——CLI 会启动即 ModuleNotFoundError，hook 入口会被 try/except 吞掉
    （full-review T-M2）。`sys.platform='win32'` 让 POSIX 主机上同样能验证接线。"""
    first = str(path.parent) if path.parent == SS_SCRIPTS else str(ROOT)
    probe = ("import os, sys\n" f"sys.path.insert(0, {first!r})\n" "sys.platform = 'win32'\n"
             + _prefix_source(path) + "\n"
             "print('VAR=' + str(os.environ.get('NoDefaultCurrentDirectoryInExePath')))\n")
    env = {k: v for k, v in os.environ.items() if k.upper() != _VAR.upper()}
    r = subprocess.run([sys.executable, "-I", "-c", probe], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "VAR=1" in r.stdout, (path.name, r.stdout, r.stderr)
