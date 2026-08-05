"""分发边界守卫（spec §3.6 / S-1）。

召回优化用的评测语料含 100 个真实会话的用户 prompt 原文与本机 cwd 路径。这些数据靠
`.gitignore`（`docs/superpowers/`）排除，但 gitignore **可被 `git add -f` 绕过**，且一旦入仓
即随 clone 永久留存——脱敏闸门 `packaging/build_plugin.py` 不扫 git 历史。

故本守卫断言的是**结果**（`git ls-files` 实际跟踪清单）而非 gitignore 规则本身：
只要不该分发的东西真的进了索引，无论经由什么路径，这里都会红。

判据是 **allowlist**（`ALLOWED_*`）而非黑名单：黑名单只能拦已经想到的那几类
（本文件早期版本只断言「不以 `docs/superpowers/` 开头、不以 `.jsonl` 结尾」），
对 `packaging/`、`.superpowers/`、`.full-review/`、`*.log`、`config.json`、
`summarized-sessions.json`、`__pycache__/` 等同样不分发的东西完全无感。
CLAUDE.md 已把分发边界写死，这里直接按那份清单校验，任何新目录误入即红。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _clean_git_env() -> dict:
    """剥掉继承来的 `GIT_*` 环境变量。

    `GIT_INDEX_FILE` / `GIT_DIR` / `GIT_WORK_TREE` 会把 git 子进程重定向到别的索引或
    仓库——在 git hook 里跑 pytest 时它们真实存在。不剥掉的话：本文件对真实仓库的断言
    会读到 hook 的临时索引（判据失真），临时仓库用例还会把测试文件写进那个共享索引
    （实测：带 `GIT_INDEX_FILE` 跑时，临时仓库的 `git add` 令本仓库 `ls-files`
    多出一条 `docs/superpowers/召回语料.jsonl`）。
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

ROOT = Path(__file__).resolve().parent.parent

# ---- 分发边界 allowlist（与 CLAUDE.md「分发边界（重要）」节同源）----
# 目录前缀：这些目录下的内容随插件分发。
ALLOWED_DIR_PREFIXES = (
    ".claude-plugin/",
    "hooks/",
    "skills/",
    "commands/",
    "scripts/",
    "tests/",
    "images/",
)
# 精确文件：`docs/` 只分发 MIGRATION.md 一个文件，其余（如 docs/superpowers/ 设计文档）
# 一律不分发，故这里给的是**具体文件**而非 `docs/` 前缀。
ALLOWED_FILES = frozenset({
    "docs/MIGRATION.md",
    "README.md",
    "CLAUDE.md",
    "CHANGELOG.md",
    ".gitattributes",
    ".gitignore",
})


def _tracked_files(root: Path = ROOT) -> list[str]:
    """返回 git 跟踪清单（仓库内相对 posix 路径）。

    必须用 `-z`：`core.quotepath` 默认 `true`，非 ASCII 路径在默认输出里会被整体套上
    双引号并做八进制转义（`"docs/superpowers/\\345\\217\\254..."`），令下游
    `startswith("docs/superpowers/")` 与 `endswith(".jsonl")` **双双失效**——本项目
    面向中文笔记工作流，语料文件名用中文是常态，恰恰最该拦的那类会整类逃逸。
    `-z` 同时消除路径含换行时按行切分错位的问题。
    """
    if shutil.which("git") is None:
        pytest.skip("git 不可用，无法核验跟踪清单")
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True, cwd=root, env=_clean_git_env(),
        # Windows 下 pytest 捕获输出时父进程 stdin 句柄可能已失效，子进程继承它会让
        # CreateProcess 报 [WinError 6] 句柄无效（实测本机约 50% 概率随机红）。
        # 显式给一个有效句柄消除这一 flaky。
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        pytest.skip(f"非 git 仓库或 git 调用失败：{err}")
    # 按 NUL 切分；不用 text=True，避免本机 locale 影响非 ASCII 路径解码
    files = [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    # 空清单说明没真正查到东西，宁可跳过也不要"真空通过"制造虚假安全感
    if not files:
        pytest.skip("git ls-files 返回空，跳过（非有效 checkout）")
    return files


def _outside_allowlist(paths: list[str]) -> list[str]:
    return [p for p in paths
            if p not in ALLOWED_FILES and not p.startswith(ALLOWED_DIR_PREFIXES)]


def test_tracked_files_within_distribution_allowlist():
    """跟踪清单必须整体落在分发边界内——任何新目录/新文件误入即红。

    这一条覆盖面远超原先的两条黑名单断言：`packaging/`（作者发布工具，含脱敏
    正则原文与源机路径）、`.superpowers/`、`.full-review/`（开发/评审草稿）、
    `*.log`、`config.json`、`summarized-sessions.json`、`__pycache__/` 等
    误入索引都会在这里被拦下，而不必逐类往黑名单里补规则。
    """
    unexpected = _outside_allowlist(_tracked_files())
    assert unexpected == [], (
        f"以下文件被 git 跟踪但不在分发边界内：{unexpected[:10]}\n"
        f"若确属应分发内容，请同步更新 CLAUDE.md「分发边界」节与本文件的 "
        f"ALLOWED_DIR_PREFIXES / ALLOWED_FILES；否则从索引移除。")


def test_no_corpus_files_tracked():
    """语料类文件一律不得被 git 跟踪（allowlist 之外的定向补充）。

    `docs/superpowers/` 已被 allowlist 覆盖；`.jsonl` 这条仍单列，是因为
    allowlist 放行的目录内部（如 `tests/fixtures/`）也不允许出现评测语料。
    """
    for p in _tracked_files():
        assert not p.startswith("docs/superpowers/"), f"gitignore 被绕过：{p}"
        assert not p.endswith(".jsonl"), f"语料类文件被跟踪：{p}"


def test_allowlist_rejects_known_non_distributed_paths():
    """自证区分力：allowlist 判据必须能拦下已知的不分发路径。

    若哪次误把 `docs/` 整个加进前缀表、或漏掉判定分支，本用例会红——
    避免 allowlist 退化成「什么都放行」的空网。
    """
    should_be_rejected = [
        "packaging/build_plugin.py",
        ".superpowers/sdd/task-1.md",
        ".full-review/00-scope.md",
        "docs/superpowers/spec.md",
        "docs/superpowers/召回语料.jsonl",
        "config.json",
        "summarized-sessions.json",
        "debug.log",
        ".claude/worktrees/w/README.md",
    ]
    assert _outside_allowlist(should_be_rejected) == should_be_rejected
    # 反向：真实分发内容必须放行，否则守卫会把正常提交拦成红
    should_pass = [
        ".claude-plugin/plugin.json", "hooks/run-hook.cmd",
        "skills/vault-loader/scripts/_scorer.py", "commands/vault.md",
        "scripts/x.py", "tests/test_no_corpus_in_repo.py",
        "images/cc_preview.png", "docs/MIGRATION.md",
        "README.md", "CLAUDE.md", ".gitattributes", ".gitignore",
    ]
    assert _outside_allowlist(should_pass) == []


def test_guard_catches_cjk_named_corpus(tmp_path):
    """自证区分力：中文命名的语料文件也必须被拦下。

    `-z` 之前本守卫对这类文件**完全失效**（实测：只把 `docs/superpowers/召回语料.jsonl`
    加进索引时守卫仍全绿，因为默认输出是 `"docs/superpowers/\\345\\217\\254..."`，
    既不以 `docs/superpowers/` 开头也不以 `.jsonl` 结尾）。

    用独立临时仓库而非污染本仓库索引：本 worktree 可能有并发会话在 add/commit，
    往共享索引里塞语料文件再摘掉存在把它带进真实 commit 的风险。git 的 quotepath
    行为与仓库无关，临时仓库能等价复现（下面显式把 `core.quotepath` 设成默认值
    `true`，避免开发者全局配置关掉它后本用例退化成空跑）。
    """
    if shutil.which("git") is None:
        pytest.skip("git 不可用")
    env = _clean_git_env()

    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, env=env,
                       stdin=subprocess.DEVNULL, capture_output=True)

    _git("init", "-q")
    _git("config", "core.quotepath", "true")
    (tmp_path / "docs" / "superpowers").mkdir(parents=True)
    corpus = tmp_path / "docs" / "superpowers" / "召回语料.jsonl"
    corpus.write_text('{"prompt": "x"}\n', encoding="utf-8")
    _git("add", "-f", "docs/superpowers/召回语料.jsonl")

    tracked = _tracked_files(tmp_path)
    assert tracked == ["docs/superpowers/召回语料.jsonl"], (
        f"路径未原样取回（quotepath 转义未消除）：{tracked}")
    # 守卫的三条判据都必须命中这条路径
    p = tracked[0]
    assert p.startswith("docs/superpowers/") and p.endswith(".jsonl")
    assert _outside_allowlist(tracked) == tracked
