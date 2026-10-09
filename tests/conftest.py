"""仓库根 tests/ 的 pytest 配置。"""
from __future__ import annotations

import os


def pytest_configure(config):
    """剥离继承来的全部 `GIT_*`（大小写不敏感）。

    `git rebase -x` 等由 git 派生的子进程会带着 `GIT_DIR` 进来；本根的守卫用
    `git ls-files` 取论域，跟随它就会扫到别的仓库（2026-10-08 实证）。
    守卫见同目录 `test_git_env_isolation.py`。
    """
    for key in [k for k in os.environ if k.upper().startswith("GIT_")]:
        del os.environ[key]
