"""推送守卫存在性门禁（full-review DO-H1）。

推送策略是防止**开发谱系 git 历史**泄露的唯一防线：脱敏闸门
（`packaging/build_plugin.py`）只扫工作树、不扫 git 历史，clone 之后仍可经
`git show <旧commit>:<file>` 取回历史版本内容——闸门在原理上拦不住这类泄露，
只有推送守卫能。

守卫本体是 `.git/hooks/pre-push`。git hook **从不经 git 协议传输**（git 设计
使然），其源副本也不在分发集内（见 CLAUDE.md「分发边界」），因此「这次 clone
从来没手工装过守卫」是一个**完全静默**的状态：本地一切看起来正常，直到某次
推送把开发历史送上公开远端。本文件把那个状态变成一条红色测试。

**判据只在风险真实存在时生效**：仅当本地存在与 `origin/master` 无共同祖先的
分支（即开发谱系）时才要求守卫。发布 clone 只有 master 谱系，本用例自动跳过，
不会误伤插件安装者。

同一 clone 内的所有 worktree **共享**同一份 hook（`git rev-parse --git-path`
在 worktree 内解析到主仓库的 `.git/hooks/`），故 worktree 不是缺口，也不需要
逐 worktree 安装。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# 守卫内容必须出现的判据关键字。存在性之外再钉一层**形态**：空文件、占位文件、
# 被别的 hook 覆盖掉的文件都能通过「文件存在」却起不到任何作用。
GUARD_TOKENS = ("origin/master", "merge-base")


def _clean_git_env() -> dict:
    """剥掉继承来的 `GIT_*` 环境变量。

    在 git hook 里跑 pytest 时 `GIT_DIR` / `GIT_INDEX_FILE` / `GIT_WORK_TREE`
    真实存在，会把 git 子进程重定向到别的仓库或索引，令谱系判定与 hook 路径
    解析双双失真——而「从 hook 里跑测试」恰恰是本守卫最该生效的场景。
    （与 `test_no_corpus_in_repo.py` 同一处理，理由见该文件。）
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, env=_clean_git_env(),
        # Windows 下 pytest 捕获输出时父进程 stdin 句柄可能已失效，子进程继承它
        # 会让 CreateProcess 报 [WinError 6] 句柄无效。显式给有效句柄消除该 flaky。
        stdin=subprocess.DEVNULL,
    )


def _out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "surrogateescape").strip()


def _require_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git 不可用，无法核验推送守卫")


def _local_branches(repo: Path) -> list[str]:
    proc = _git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=repo)
    if proc.returncode != 0:
        return []
    return [b for b in _out(proc).splitlines() if b]


def branches_without_common_ancestor(repo: Path, base_ref: str) -> list[str]:
    """返回与 `base_ref` **无共同祖先**的本地分支。

    这正是推送守卫判据 2 的判定式：发布分支是孤儿提交，开发谱系与它无共同
    祖先，`git merge-base` 失败即可识别。按谱系而非按分支名判定，是因为把
    开发分支改名成发布分支名同样会泄露历史——名字判据拦不住，谱系判据能。
    """
    return [b for b in _local_branches(repo)
            if _git("merge-base", base_ref, b, cwd=repo).returncode != 0]


def _hook_path(repo: Path) -> Path | None:
    """解析本仓库实际生效的 pre-push 路径（worktree 内会指向主仓库的 .git）。"""
    proc = _git("rev-parse", "--git-path", "hooks/pre-push", cwd=repo)
    if proc.returncode != 0:
        return None
    raw = _out(proc)
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else (repo / p)


def _dev_lineage_or_skip() -> list[str]:
    """返回开发谱系分支；风险不存在或无从判定时 skip。"""
    _require_git()
    if _git("rev-parse", "--verify", "--quiet", "origin/master", cwd=ROOT).returncode != 0:
        pytest.skip("本地无 origin/master，无从判定谱系（守卫本身对此 fail-closed）")
    risky = branches_without_common_ancestor(ROOT, "origin/master")
    if not risky:
        pytest.skip("本 clone 只有发布谱系，无开发历史可泄露，不要求推送守卫")
    return risky


def test_push_guard_installed_when_dev_lineage_present():
    """有开发谱系就必须有推送守卫——把「没装」从静默变成响亮失败。"""
    risky = _dev_lineage_or_skip()
    hook = _hook_path(ROOT)
    assert hook is not None and hook.is_file(), (
        f"检测到开发谱系分支 {risky[:3]}，但推送守卫未安装：{hook}\n"
        f"git hook 不随 clone 传输，换机 / 重新 clone 后必须手工安装；"
        f"未安装时把开发分支推向公开或内网远端会永久泄露其 git 历史。\n"
        f"安装：python packaging/install_hooks.py"
        f"（发布工具目录，不在分发集内，需从作者备份恢复）")

    text = hook.read_text(encoding="utf-8", errors="replace")
    missing = [t for t in GUARD_TOKENS if t not in text]
    assert not missing, (
        f"推送守卫已安装但内容缺少判据关键字 {missing}——"
        f"文件可能是占位、空壳，或被别的 pre-push hook 覆盖：{hook}")


def test_push_guard_is_executable_on_posix():
    """POSIX 上 hook 必须带执行位，否则 git 静默跳过它（等于没装）。"""
    _dev_lineage_or_skip()
    if os.name == "nt":
        pytest.skip("Windows 无 POSIX 执行位语义，git for Windows 按 shebang 直接执行")
    hook = _hook_path(ROOT)
    if hook is None or not hook.is_file():
        pytest.skip("守卫未安装，由 test_push_guard_installed_when_dev_lineage_present 报出")
    assert os.access(hook, os.X_OK), (
        f"推送守卫缺执行位，git 会静默跳过：{hook}\n修复：chmod +x '{hook}'")


def test_installed_guard_matches_source_when_release_tooling_present():
    """作者检出里，已装守卫必须与源副本逐字节一致。

    源改了没重装同样是静默失效：读源码以为改动生效，实际跑的还是旧守卫。
    分发 clone 无发布工具目录，自动跳过。

    判据是**字节相等**而非文本相等：源副本是 `#!/bin/sh` 脚本，被 CRLF 化后
    shebang 尾部带 `\\r`，POSIX shell 直接解析失败——那是「装了但跑不起来」，
    必须能被这条用例区分出来。
    """
    _dev_lineage_or_skip()
    src = ROOT / "packaging" / "hooks" / "pre-push"
    if not src.is_file():
        pytest.skip("无发布工具目录（此检出不是作者主检出），跳过源一致性核验")
    hook = _hook_path(ROOT)
    if hook is None or not hook.is_file():
        pytest.skip("守卫未安装，由 test_push_guard_installed_when_dev_lineage_present 报出")
    assert hook.read_bytes() == src.read_bytes(), (
        f"已装守卫与源副本不一致：\n  已装 {hook}\n  源   {src}\n"
        f"重新安装：python packaging/install_hooks.py")


# ---- 自证区分力：判据必须真的按谱系判断，不是恒真 / 恒假 ----

def _mkrepo(root: Path) -> None:
    """建最小 git 仓库；禁用签名与外部 hooksPath，避免受本机全局配置影响。"""
    env = _clean_git_env()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, env=env,
                       stdin=subprocess.DEVNULL, capture_output=True)

    run("init", "-q")
    # 不用 `git init -b <name>`（需 git>=2.28），改用 symbolic-ref 设未出生的 HEAD
    run("symbolic-ref", "HEAD", "refs/heads/base")
    empty_hooks = root / ".empty-hooks"
    empty_hooks.mkdir()
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 ("commit.gpgsign", "false"), ("core.hooksPath", str(empty_hooks))):
        run("config", k, v)


def test_lineage_detector_distinguishes_orphan_from_descendant(tmp_path):
    """孤儿分支必须被报出，后代分支必须不被误报。

    若判据退化成恒真，`desc` 会被误报、发布 clone 上的门禁全变红；若退化成
    恒假，`stray` 漏报、守卫缺失重新变回静默——两个方向都由本用例钉住。
    """
    _require_git()
    _mkrepo(tmp_path)
    env = _clean_git_env()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, env=env,
                       stdin=subprocess.DEVNULL, capture_output=True)

    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    run("add", "a.txt")
    run("commit", "-q", "-m", "base")
    run("checkout", "-q", "-b", "desc")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    run("add", "b.txt")
    run("commit", "-q", "-m", "desc")
    # 孤儿分支：无父提交，与 base 无共同祖先
    run("checkout", "-q", "--orphan", "stray")
    run("commit", "-q", "-m", "stray")

    detected = branches_without_common_ancestor(tmp_path, "base")
    assert detected == ["stray"], (
        f"谱系判据失准：期望只报孤儿分支 stray，实际 {detected}")
