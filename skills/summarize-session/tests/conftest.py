from __future__ import annotations
import os
import sys
from pathlib import Path

import pytest

# 允许 tests/ 直接导入 scripts/obsidian_cli
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """临时 Vault 目录（不注册到 Obsidian，仅用于降级路径文件 I/O 测试）。"""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".meta").mkdir()
    return vault


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: 需要真实 Obsidian CLI 与 GUI 的端到端测试")
    # 剥离继承来的全部 GIT_*（大小写不敏感）：git 派生的子进程（如 `git rebase -x`）带着
    # GIT_DIR 进来时，用例里在临时目录跑的 git 会改写真实仓库（2026-10-08 实证）。
    # 守卫见仓库根 tests/test_git_env_isolation.py。
    for key in [k for k in os.environ if k.upper().startswith("GIT_")]:
        del os.environ[key]
