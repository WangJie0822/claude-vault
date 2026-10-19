# -*- coding: utf-8 -*-
"""首轮候选池：admitted ∪ 项目笔记，顺序、截断、worktree 基名归一、索引排除。"""
from __future__ import annotations

from pathlib import Path

from scripts._candidate_pool import (WORKLOG_CATEGORY, build_topic_pool, is_system_index,
                                     is_worklog, main_repo_basename, project_note_prefixes,
                                     safe_int)
from scripts._frontmatter_reader import Entry
from scripts._signal_collect import collect_signal_a_project_dir


def _e(path, mtime=0, summary="s"):
    return Entry(path=path, tags=(), category="", summary=summary, mtime=mtime, updated="", keywords=())


def test_is_worklog_by_path_or_category():
    """spec 2.4.0 W1：路径首段为工作日志，或 category 含它（含列表形态）。"""
    assert WORKLOG_CATEGORY == "工作日志"
    assert is_worklog("工作日志/2026年/10月/2026-10-14.md", Entry(path="x"))
    assert is_worklog("项目笔记/a.md", Entry(path="x", category="工作日志"))
    assert is_worklog("项目笔记/a.md", Entry(path="x", categories=("技术笔记", "工作日志")))
    assert not is_worklog("项目笔记/a.md", Entry(path="x", category="技术笔记"))
    assert not is_worklog("项目笔记/工作日志.md", Entry(path="x"))
    assert not is_worklog("工作日志.md", Entry(path="x"))
    assert not is_worklog("技术笔记/工作日志/a.md", Entry(path="x"))


def test_is_worklog_fail_open():
    assert is_worklog("项目笔记/a.md", None) is False
    assert is_worklog(None, Entry(path="x")) is False


def test_main_repo_basename_plain_checkout(tmp_path):
    root = tmp_path / "claude-vault"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert main_repo_basename(root) == "claude-vault"


def test_main_repo_basename_worktree_gitfile(tmp_path):
    """worktree 的 .git 是文件：gitdir: <main>/.git/worktrees/<wt> ⇒ 取 <main> 的名。"""
    main = tmp_path / "claude-vault"
    wt = main / ".claude" / "worktrees" / "llm-rerank"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main.as_posix()}/.git/worktrees/llm-rerank\n", encoding="utf-8")
    assert main_repo_basename(wt) == "claude-vault"


def test_main_repo_basename_gitfile_without_worktree_marker_falls_back(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/sub\n", encoding="utf-8")
    assert main_repo_basename(sub) == "sub"


def test_main_repo_basename_relative_gitdir_falls_back(tmp_path):
    """相对 gitdir 命中 worktree marker，但 `target[:i]` 退化成 `..`：
    `Path("..").name == ".."`，不得原样返回，须回落 `root.name`（M-5）。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/worktrees/x\n", encoding="utf-8")
    assert main_repo_basename(sub) == "sub"


def test_main_repo_basename_non_git(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert main_repo_basename(d) == "plain"


def test_is_system_index():
    assert is_system_index("项目笔记/x/INDEX.md")
    assert is_system_index("项目笔记/x/未分类 索引.md")
    assert is_system_index("项目笔记/x/x 索引.md")
    assert not is_system_index("项目笔记/x/y 索引.md")
    assert not is_system_index("项目笔记/x/a.md")


def test_project_note_prefixes_with_extra_paths():
    assert project_note_prefixes("cv", ["技术笔记/召回", "/杂项/"]) == ["项目笔记/cv/", "技术笔记/召回/", "杂项/"]
    assert project_note_prefixes("", []) == []


def test_pool_keeps_admitted_then_project_by_mtime(tmp_path):
    root = tmp_path / "cv"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    entries = {
        "a.md": _e("a.md"), "b.md": _e("b.md"),
        "项目笔记/cv/old.md": _e("项目笔记/cv/old.md", mtime=1),
        "项目笔记/cv/new.md": _e("项目笔记/cv/new.md", mtime=9),
        "项目笔记/cv/a.md": _e("项目笔记/cv/a.md", mtime=5),
        "项目笔记/cv/INDEX.md": _e("项目笔记/cv/INDEX.md", mtime=99),
        "项目笔记/other/z.md": _e("项目笔记/other/z.md", mtime=99),
    }
    pool = build_topic_pool(["b.md", "a.md", "项目笔记/cv/a.md"], entries, root, [], top_n=2, project_top_k=2, pool_max=10)
    paths = [p for p, _ in pool]
    assert paths[:2] == ["b.md", "a.md"]                       # admitted 前 top_n 原序全保留
    assert paths[2:] == ["项目笔记/cv/new.md", "项目笔记/cv/a.md"]  # 项目笔记按 mtime 降序补 top_k
    assert "项目笔记/cv/INDEX.md" not in paths
    assert "项目笔记/other/z.md" not in paths


def test_pool_dedups_and_caps(tmp_path):
    root = tmp_path / "cv"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    entries = {f"项目笔记/cv/n{i}.md": _e(f"项目笔记/cv/n{i}.md", mtime=i) for i in range(10)}
    adm = ["项目笔记/cv/n9.md", "项目笔记/cv/n8.md"]
    pool = build_topic_pool(adm, entries, root, [], top_n=30, project_top_k=20, pool_max=4)
    paths = [p for p, _ in pool]
    assert len(paths) == 4 and len(set(paths)) == 4
    assert paths[:2] == adm


def test_pool_summary_truncated_and_missing_admitted_skipped(tmp_path):
    root = tmp_path / "cv"
    root.mkdir()
    entries = {"a.md": _e("a.md", summary="x" * 500)}
    pool = build_topic_pool(["a.md", "ghost.md"], entries, root, [], 30, 20, 50)
    assert pool == [("a.md", "x" * 120)]


def test_pool_uses_main_repo_name_in_worktree(tmp_path):
    main = tmp_path / "claude-vault"
    wt = main / ".claude" / "worktrees" / "wt1"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main.as_posix()}/.git/worktrees/wt1\n", encoding="utf-8")
    entries = {"项目笔记/claude-vault/x.md": _e("项目笔记/claude-vault/x.md", mtime=1)}
    pool = build_topic_pool([], entries, wt, [], 30, 20, 50)
    assert [p for p, _ in pool] == ["项目笔记/claude-vault/x.md"]


def test_pool_never_raises_on_garbage_args(tmp_path):
    """fail-open 契约：垃圾参数只能退化，不能抛（review Task 4 Important #1）。"""
    root = tmp_path / "cv"
    root.mkdir()
    entries = {"a.md": _e("a.md", summary="S")}
    assert build_topic_pool(["a.md"], entries, root, [], "not-a-number", 2, 10) == []   # 垃圾 top_n → 0 → 空池
    assert build_topic_pool(["a.md"], None, root, [], 30, 20, 50) == []
    assert build_topic_pool(None, entries, root, [], 30, 20, 50) == []
    assert build_topic_pool(["a.md"], entries, root, [], True, "x", None) == []   # bool/str/None 上限 → 0


def test_index_exclusion_parity_with_signal_a(tmp_path):
    """1A-M4 / 1B-M-7：候选池与 SessionStart 信号 A 的索引排除必须给出**相同**的裁决。
    两边吃的载体不同（cache 键 vs 文件系统 rglob），无法共用调用点，故用同一组样本对跑：
    真建文件让信号 A 走真实 rglob，再把同一批相对路径喂 `is_system_index`，两侧保留集合必须相等。
    变异：任一侧把 `INDEX.md`/`未分类 索引.md`/`<父目录名> 索引.md` 三条中的一条去掉即红。"""
    vault = tmp_path / "vault"
    rels = [
        "项目笔记/x/a.md",
        "项目笔记/x/INDEX.md",
        "项目笔记/x/未分类 索引.md",
        "项目笔记/x/x 索引.md",            # 文件名 == 父目录名 + ' 索引.md'
        "项目笔记/x/y 索引.md",            # 不等于父目录名 ⇒ 普通笔记
        "项目笔记/x/sub/sub 索引.md",      # 嵌套一层，父目录名是 sub
        "项目笔记/x/sub/x 索引.md",        # 嵌套后父目录名不再是 x ⇒ 普通笔记
        "项目笔记/x/sub/b.md",
    ]
    for r in rels:
        f = vault / r
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# t\n", encoding="utf-8")
    kept_by_signal_a = collect_signal_a_project_dir(Path("x"), vault, [])
    kept_by_pool = {r for r in rels if not is_system_index(r)}
    assert kept_by_signal_a == kept_by_pool
    # 阳性对照：样本确实同时含「被排除」与「保留」两类，判据不是恒真
    assert "项目笔记/x/x 索引.md" not in kept_by_pool and "项目笔记/x/y 索引.md" in kept_by_pool
    assert len(kept_by_pool) == 4


def test_safe_int_public_contract():
    """1A-L8：`safe_int` 是对外契约（三个配置读取点用它）。bool 视为垃圾——与 `_topic.effective_pin_max`
    刻意相反（那边 `false → 0` 篇）。"""
    assert safe_int(3, 0) == 3 and safe_int("3", 0) == 3 and safe_int(2.9, 0) == 2
    assert safe_int(True, 7) == 7 and safe_int(False, 7) == 7
    assert safe_int(None, 7) == 7 and safe_int("x", 7) == 7 and safe_int([], 7) == 7
