# -*- coding: utf-8 -*-
"""测试脚手架。

**刻意不建 `scripts/__init__.py`**：vault-loader 的 `scripts/` 是 regular package
（有 `__init__.py`），两个 skill 同时声明顶层包名 `scripts` 时，归谁取决于 sys.path
顺序——实测会让 `scripts._config_loader` 直接 ImportError。沿用 summarize-session
的做法（conftest 加 sys.path + 裸 import），顶层包名之争就不存在。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    """无条件隔离 HOME。

    本 skill 读写的正是用户真实 `~/.context-vault/config.json`，测试绝不能碰它。
    Windows 上 `Path.home()` 取 `USERPROFILE` 而非 `HOME`，两者都要设——只设 HOME
    在 Windows 上完全无效（仓库既有教训）。
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home
