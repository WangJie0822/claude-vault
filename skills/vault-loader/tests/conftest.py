"""共享 pytest fixture：临时 HOME、临时 Vault、临时 git 仓库。"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch):
    """**无条件**把 HOME/USERPROFILE 指向临时目录 —— 测试永远不该写真实主目录。

    为什么不能只靠 `_reset_module_globals` 复位 `_RUNTIME`：那只决定写 canonical
    还是 legacy **两个子树中的哪一个**，而两者都以 `Path.home()` 为根 ——
    `state_path_for_cwd` 的 legacy 分支是 `~/.claude/projects/<hash>/`，canonical 分支
    是 `~/.context-vault/state/<runtime>/<hash>.json`。隔离必须在 HOME 这一层做。

    实测（2026-09-10，一次全套跑的增量）：
        只复位 _RUNTIME     → canonical +0，legacy **+36**
        无条件隔离 HOME     → canonical +0，legacy  +0
    历史积累：legacy 侧已有 2658 个 `vault-loader-state.json`，而其中的 topic 条目
    只有 18 条属真实会话，其余全是历次测试残留。

    显式请求 `tmp_home` 的用例照常工作：它自己的 `monkeypatch.setenv` 后执行、覆盖
    本 fixture 的值。本 fixture 兜住的是**没有**请求 `tmp_home` 的那些用例。
    """
    h = tmp_path_factory.mktemp("autohome")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    yield


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """每个用例前后复位三处模块级全局：emit 守卫、`_metrics` 与 `_state` 的命名空间。

    **emit 守卫**：生产上一个 hook 进程只跑一次 `main()`、只 emit 一次，守卫置位后
    不需要复位；但单测在**同一进程**内跨用例反复调用 emit，不复位的话第二个用例起
    就被守卫静默拦掉，表现为「stdout 空」的假失败。

    **`_state` 的 `_RUNTIME`**（2026-09-10 补，此前只复位了 `_metrics`）：它同样是
    模块级全局，而 `state_path_for_cwd` 用它 + `Path.home()` 拼落点。用例调过
    `configure_context("claude")` / `adopt_runtime("claude")` 之后若不复位，它会泄漏到
    后续用例；那些用 `tmp_path`（而非 `tmp_home`）的 state 用例于是把文件写进**真实
    主目录**。实证：一次全套跑在 `~/.context-vault/state/claude/` 下留下 **252 个**
    测试文件（210 个带 `sess-A`/`s0` 这类测试 sid + 42 个用例故意造的畸形数据），
    而该目录真实数据只有 16 个。危害有限（那些 cwd hash 永不被真实项目命中，且真实
    文件未被覆盖），但这是「测试逃出沙箱写用户真实数据目录」，不能留。
    守卫见 `test_topic_namespace.py` 末尾成对的那两条用例。
    """
    from scripts import _metrics, _state
    from scripts._output import reset_emit_guard

    _metrics.configure_context("unknown")
    _state.configure_context("unknown")
    reset_emit_guard()
    yield
    _metrics.configure_context("unknown")
    _state.configure_context("unknown")
    reset_emit_guard()


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
