"""测试套件必须对继承来的 GIT_* 环境变量自我防护。

2026-10-08 实证：在 `git rebase -x` 里跑测试时，git 给 exec 子进程注入了
`GIT_DIR=<worktree gitdir>`（同时还有 GIT_EDITOR / GIT_EXEC_PATH / GIT_PREFIX /
GIT_SEQUENCE_EDITOR）。测试里在临时目录执行的 `git init` / `git config` / `git commit`
遵从 `GIT_DIR`，于是把提交、分支以及 `core.bare=true`、`core.hookspath`、`user.*`
写进了真实仓库共享的 `.git/config`；被改写的 `core.hooksPath` 还会让 pre-push
推送守卫静默失效。

各 pytest 根的 conftest 在 `pytest_configure` 里剥离全部 `GIT_*`。本文件造出差异：
把 `GIT_DIR` 指向一个受害仓库，用子进程跑会调 git 的测试文件，断言它们照常通过、
受害仓库的 config 与 ref 一字不变。只在开发机本进程里断言「没有 GIT_DIR」是零判别力的
——开发机上本来就没设它。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# (pytest 根目录, 该根下一个会调 git 的测试文件)
CASES = [
    pytest.param(".", "tests/test_no_secrets_in_tree.py", id="repo-tests"),
    pytest.param("skills/vault-loader", "tests/integration/test_session_start_injected_set.py",
                 id="vault-loader"),
    pytest.param("skills/summarize-session", "tests/test_git_commit_vault.py",
                 id="summarize-session"),
]


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=_clean_env(),
                          check=True).stdout


def _snapshot(repo: Path) -> tuple[bytes, str]:
    return ((repo / ".git" / "config").read_bytes(),
            _git(repo, "for-each-ref", "--format=%(refname) %(objectname)"))


@pytest.mark.parametrize("root,target", CASES)
def test_suite_ignores_inherited_git_dir(tmp_path, root, target):
    victim = tmp_path / "victim"
    victim.mkdir()
    _git(victim, "init", "-q")
    _git(victim, "config", "user.email", "victim@example.invalid")
    _git(victim, "config", "user.name", "victim")
    (victim / "f.txt").write_text("x\n", encoding="utf-8")
    _git(victim, "add", "f.txt")
    _git(victim, "-c", "core.hooksPath=", "commit", "-q", "-m", "init")
    before = _snapshot(victim)

    env = _clean_env()
    env.update({"GIT_DIR": str(victim / ".git"), "PYTHONDONTWRITEBYTECODE": "1"})
    r = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "--color=no", target], cwd=ROOT / root, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=600)

    assert _snapshot(victim) == before, "测试跟随了继承来的 GIT_DIR，改写了受害仓库"
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
