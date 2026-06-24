"""共享 pytest fixture：临时 HOME、临时 Vault、临时 git 仓库。"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

import pytest


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 HOME，自动隔离 ~/.claude 和 ~/Vault。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    (home / "Vault").mkdir()
    (home / "Vault" / ".meta").mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Windows 上 Path.home() 取 USERPROFILE 而非 HOME（POSIX 取 HOME、忽略此行），
    # 不补则 subprocess/进程内测试读到真实 home，tmp 隔离失效。
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def tmp_vault(tmp_home: Path) -> Path:
    """临时 Vault 路径，已带 .meta 子目录。"""
    return tmp_home / "Vault"


@pytest.fixture
def write_frontmatter_cache(tmp_vault: Path) -> Callable[[dict], Path]:
    """写入 frontmatter-cache.json，返回路径。"""

    def _write(entries: dict) -> Path:
        cache_path = tmp_vault / ".meta" / "frontmatter-cache.json"
        payload = {"_version": 1, "entries": entries}
        # 显式 utf-8：Windows write_text 默认 GBK，含中文路径/摘要时与 load_cache 的
        # utf-8 读不匹配 → 解析失败返 {}。
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return cache_path

    return _write


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """初始化一个临时 git 仓库，含 main 分支和初始 commit。

    显式隔离 core.hooksPath 与用户身份，避免被用户全局 git hook（如 commit-msg
    强制格式校验）干扰测试逻辑。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    # 隔离全局 hooksPath（指向不存在的目录即可禁用）
    subprocess.run(
        ["git", "config", "core.hooksPath", str(tmp_path / "_no_hooks")],
        cwd=repo, check=True,
    )
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo
