"""fail-open 硬约束守卫：hook 在任何情况下都不得阻断会话（退出码恒 0）。

项目 CLAUDE.md 把「所有 hook fail-open」列为不变量，两个 hook 入口也都在
`if __name__ == "__main__":` 里包了 try/except → exit 0。但那层兜底**盖不住顶层 import**
——`import` 语句在该 try 之外执行，导入期异常会直接冒泡成 exit 1 + traceback。

这不是假想：任何一个被 import 的模块出现导入期副作用失败（读配置、探路径、初始化状态），
或分发时漏了某个文件，都会让**每一次** prompt / 每一次开会话产生一个 hook 错误。
本文件把「导入失败也必须 exit 0」钉成可执行守卫（F6）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

VL_ROOT = Path(__file__).resolve().parent.parent
HOOKS = ["prompt_submit_load.py", "session_start_load.py"]


def _utf8_env() -> dict:
    """子进程环境：把 stdio 编码钉成 UTF-8，不依赖平台默认（M-1）。

    此前本用例在 Windows 上恒红，断言拿到的是 `妯″潡鍔犺浇澶辫触`。实测定位（2026-08-05，
    Python 3.14.6 / locale `cp936`）：

    - 子进程 stderr 的**原始字节**是 `e6 a8 a1 …`，即 UTF-8——**设不设 `PYTHONUTF8`
      都一样**，该分发版的子进程默认就以 UTF-8 写 stdio；
    - 父进程 `text=True` 却按 `locale.getencoding()`（cp936）解码，于是 UTF-8 字节被
      当 GBK 读 → 乱码。

    所以**根因在父进程解码侧**，必要修复是各调用点传 `encoding="utf-8"`。
    `PYTHONUTF8=1` 在本机是冗余的，保留它是**防御性**的：子进程 stdio 编码取决于
    Python 版本与平台默认（PEP 540/686 一路在变），钉死一侧比依赖默认稳。

    > 评审 BP-5 曾断言「只在父进程加 encoding 的修法在干净机器上会再次失效，
    > 须同时钉 PYTHONUTF8」。本机变异验证**不支持**该断言：去掉 `PYTHONUTF8` 后
    > 4 个用例仍全绿。故此处不再复述那条因果，只保留防御理由。

    做法与 `integration/_run` 一致。
    """
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    return env


def _make_broken_pkg(tmp_path: Path, hook_name: str) -> Path:
    """构造一个残缺的 scripts 包：只有 __init__ 与被测 hook 本体，其余同级模块全部缺失。

    hook 内的 `sys.path.insert(0, Path(__file__).parent.parent)` 会让 `scripts` 解析到
    这个临时包，于是它的第一条 `from scripts._config_loader import ...` 必然 ImportError
    —— 正是我们要覆盖的「导入期异常」。
    """
    pkg = tmp_path / "scripts"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    src = (VL_ROOT / "scripts" / hook_name).read_text(encoding="utf-8")
    target = pkg / hook_name
    target.write_text(src, encoding="utf-8")
    return target


@pytest.mark.parametrize("hook_name", HOOKS)
def test_import_failure_still_exits_zero(tmp_path: Path, hook_name: str) -> None:
    """顶层 import 失败时仍须 exit 0（改动前实测 exit 1）。"""
    script = _make_broken_pkg(tmp_path, hook_name)
    r = subprocess.run(
        [sys.executable, str(script)],
        input="{}", capture_output=True, text=True,
        encoding="utf-8", env=_utf8_env(), timeout=60,
    )
    assert r.returncode == 0, (
        f"{hook_name} 导入失败时退出码为 {r.returncode}，违反 fail-open 硬约束。\n"
        f"stderr:\n{r.stderr}"
    )
    assert "模块加载失败" in r.stderr, (
        f"应在 stderr 留下可诊断痕迹，实际 stderr:\n{r.stderr}"
    )
    assert r.stdout.strip() == "", "导入失败时不得产生 stdout（会被当作 hook 输出解析）"


@pytest.mark.parametrize("hook_name", HOOKS)
def test_import_failure_is_visible_to_tests(tmp_path: Path, hook_name: str) -> None:
    """反向守卫：以**模块**身份被 import 时，导入错误必须原样抛出。

    否则 `except: sys.exit(0)` 会让测试进程在 collect 阶段静默退出，真实的导入错误
    永久隐身。区分靠 `__name__ != "__main__"` 时 re-raise。
    """
    script = _make_broken_pkg(tmp_path, hook_name)
    mod = script.stem
    probe = (
        "import sys; sys.path.insert(0, r'%s')\n"
        "try:\n"
        "    __import__('scripts.%s')\n"
        "    print('NO_RAISE')\n"
        "except SystemExit:\n"
        "    print('WRONG_SYSTEMEXIT')\n"
        "except ImportError:\n"
        "    print('RAISED_OK')\n"
    ) % (str(tmp_path), mod)
    # stdin=DEVNULL 是必需的，不是防御性写法：不传 stdin 时子进程继承父进程的，
    # 而在 Claude Code 的 Bash 工具环境下那个句柄无效，DuplicateHandle 会抛
    # `OSError: [WinError 6] 句柄无效`——本仓历史上被记成「偶发环境失败」的一类，
    # 根因就在这里。上面那组用例因为传了 input= 而隐含建了 stdin pipe，才没撞上。
    r = subprocess.run(
        [sys.executable, "-c", probe],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", env=_utf8_env(), timeout=60,
    )
    assert "RAISED_OK" in r.stdout, (
        f"以模块身份 import 时应抛 ImportError，实际输出：{r.stdout!r} / {r.stderr!r}"
    )
