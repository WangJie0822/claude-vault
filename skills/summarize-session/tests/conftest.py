from __future__ import annotations
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
