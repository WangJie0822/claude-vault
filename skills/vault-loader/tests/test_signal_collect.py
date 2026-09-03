"""_signal_collect 单测：信号 A/B/I（无 git 依赖部分）。"""
from __future__ import annotations

from pathlib import Path

from scripts._signal_collect import (
    collect_signal_a_project_dir,
    collect_signal_b_keyword_map,
    collect_signal_i_project_claude_md,
)


# ===== 信号 A：项目笔记目录直接命中 =====

def test_signal_a_basename_match(tmp_vault: Path) -> None:
    proj_dir = tmp_vault / "项目笔记" / "myproj"
    proj_dir.mkdir(parents=True)
    (proj_dir / "note1.md").write_text("foo")
    (proj_dir / "note2.md").write_text("bar")

    paths = collect_signal_a_project_dir(
        project_root=Path("/Users/x/work/myproj"),
        vault_path=tmp_vault,
        extra_paths=[],
    )
    assert "项目笔记/myproj/note1.md" in paths
    assert "项目笔记/myproj/note2.md" in paths


def test_signal_a_nested_subdir(tmp_vault: Path) -> None:
    proj_dir = tmp_vault / "项目笔记" / "myproj" / "subsystem"
    proj_dir.mkdir(parents=True)
    (proj_dir / "deep.md").write_text("x")

    paths = collect_signal_a_project_dir(
        project_root=Path("/some/myproj"), vault_path=tmp_vault, extra_paths=[]
    )
    assert "项目笔记/myproj/subsystem/deep.md" in paths


def test_signal_a_no_project_dir(tmp_vault: Path) -> None:
    paths = collect_signal_a_project_dir(
        project_root=Path("/some/missing"), vault_path=tmp_vault, extra_paths=[]
    )
    assert paths == set()


def test_signal_a_extra_paths(tmp_vault: Path) -> None:
    extra_dir = tmp_vault / "ProjectA" / "specs"
    extra_dir.mkdir(parents=True)
    (extra_dir / "x.md").write_text("y")

    paths = collect_signal_a_project_dir(
        project_root=Path("/x/noproj"),
        vault_path=tmp_vault,
        extra_paths=["ProjectA/specs/"],
    )
    assert "ProjectA/specs/x.md" in paths


# ===== 信号 B：cwd 关键词 → tag 映射 =====

def test_signal_b_keyword_substring_match() -> None:
    mapping = {
        "assistant": ["车载", "android"],
        "cashbook": ["ProjectA"],
    }
    tags = collect_signal_b_keyword_map(
        cwd=Path("/Users/x/Work/projectb-assistant2.0"), keyword_to_tags=mapping
    )
    assert tags == {"车载", "android"}


def test_signal_b_multiple_keywords_merge() -> None:
    mapping = {
        "claude": ["claude-code"],
        "skill": ["skill"],
    }
    tags = collect_signal_b_keyword_map(
        cwd=Path("/Users/x/.claude/skills/foo"), keyword_to_tags=mapping
    )
    assert tags == {"claude-code", "skill"}


def test_signal_b_no_match() -> None:
    tags = collect_signal_b_keyword_map(
        cwd=Path("/random/path"), keyword_to_tags={"foo": ["bar"]}
    )
    assert tags == set()


def test_signal_b_case_insensitive() -> None:
    tags = collect_signal_b_keyword_map(
        cwd=Path("/X/CashBook/work"), keyword_to_tags={"cashbook": ["ProjectA"]}
    )
    assert tags == {"ProjectA"}


# ===== 信号 I：项目 CLAUDE.md vault-loader 注释 =====

def test_signal_i_tags_comment(tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(
        "# project\n\n<!-- vault-loader: tags=[ProjectA, SwiftUI, 账本] -->\n\nbody",
        encoding="utf-8",
    )

    result = collect_signal_i_project_claude_md(tmp_path)
    assert result.tags == {"ProjectA", "SwiftUI", "账本"}
    assert result.extra_paths == []
    assert result.disabled is False


def test_signal_i_extra_paths_comment(tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(
        "<!-- vault-loader: extra_paths=[foo/bar/, baz/] -->"
    )

    result = collect_signal_i_project_claude_md(tmp_path)
    assert result.extra_paths == ["foo/bar/", "baz/"]


def test_signal_i_disable_comment(tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("<!-- vault-loader: disable -->")

    result = collect_signal_i_project_claude_md(tmp_path)
    assert result.disabled is True


def test_signal_i_no_claude_md(tmp_path: Path) -> None:
    result = collect_signal_i_project_claude_md(tmp_path)
    assert result.tags == set()
    assert result.extra_paths == []
    assert result.disabled is False


def test_signal_i_no_vault_loader_comment(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# normal claude.md")
    result = collect_signal_i_project_claude_md(tmp_path)
    assert result.tags == set()
    assert result.disabled is False


def test_signal_i_multiple_comments(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(
        "<!-- vault-loader: tags=[a, b] -->\n"
        "<!-- vault-loader: extra_paths=[x/] -->"
    )
    result = collect_signal_i_project_claude_md(tmp_path)
    assert result.tags == {"a", "b"}
    assert result.extra_paths == ["x/"]


# ===== 信号 D：commit 关键词 =====

import subprocess


def _git_commit(repo: Path, msg: str) -> None:
    (repo / f"f_{abs(hash(msg))}.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)


def test_signal_d_structured_commit_extracts_module(tmp_git_repo: Path) -> None:
    from scripts._signal_collect import collect_signal_d_commit_keywords

    _git_commit(tmp_git_repo, "[fix|vault-loader|hook][公共]修复 SessionStart 注入格式")

    keywords = collect_signal_d_commit_keywords(tmp_git_repo)
    assert "vault-loader" in keywords
    assert "hook" in keywords


def test_signal_d_plain_title_tokenize(tmp_git_repo: Path) -> None:
    from scripts._signal_collect import collect_signal_d_commit_keywords

    _git_commit(tmp_git_repo, "refactor backup mechanism for crash recovery")

    keywords = collect_signal_d_commit_keywords(tmp_git_repo)
    assert "refactor" in keywords
    assert "backup" in keywords
    assert "crash" in keywords
    # 短词应被过滤
    assert "for" not in keywords


def test_signal_d_chinese_token_extraction(tmp_git_repo: Path) -> None:
    from scripts._signal_collect import collect_signal_d_commit_keywords

    _git_commit(tmp_git_repo, "[feat|wakeup|custom][公共]修复自定义唤醒词配置")

    keywords = collect_signal_d_commit_keywords(tmp_git_repo)
    assert "wakeup" in keywords
    assert "custom" in keywords
    # 中文 token 至少 3 字
    assert any("唤醒" in k for k in keywords)


def test_signal_d_non_git_repo(tmp_path: Path) -> None:
    from scripts._signal_collect import collect_signal_d_commit_keywords

    keywords = collect_signal_d_commit_keywords(tmp_path)
    assert keywords == set()


def test_signal_d_empty_repo(tmp_path: Path) -> None:
    from scripts._signal_collect import collect_signal_d_commit_keywords

    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    keywords = collect_signal_d_commit_keywords(repo)
    assert keywords == set()


# ===== 信号 F：近 7 天工作日志 =====

import time


def _write_worklog(vault: Path, date: str, project: str, body_extra: str = "") -> Path:
    year, month = date[:4], date[5:7]
    worklog_dir = vault / "工作日志" / f"{year}年" / f"{month}月"
    worklog_dir.mkdir(parents=True, exist_ok=True)
    p = worklog_dir / f"{date}.md"
    p.write_text(
        f"---\n"
        f"tags: [工作日志]\n"
        f"category: 工作日志\n"
        f"created: {date}\n"
        f"summary: \"{date} 工作记录\"\n"
        f"---\n\n"
        f"# {date} 工作日志\n\n"
        f"## 10:00 ~ 11:00 | 1h | 修复 hook 触发顺序\n\n"
        f"**项目**: {project}\n"
        f"**分支**: main\n\n"
        f"{body_extra}\n",
        encoding="utf-8",
    )
    return p


def test_signal_f_finds_recent_worklog_by_project(tmp_vault: Path) -> None:
    from scripts._signal_collect import collect_signal_f_recent_worklogs

    today = time.strftime("%Y-%m-%d")
    p = _write_worklog(tmp_vault, today, "myproj")

    result = collect_signal_f_recent_worklogs(
        project_root=Path("/x/myproj"), vault_path=tmp_vault, days=7
    )
    assert any("工作日志" in path for path in result.paths)
    # 关键词应包含 hook
    assert "hook" in result.keywords


def test_signal_f_excludes_old_worklog(tmp_vault: Path) -> None:
    from scripts._signal_collect import collect_signal_f_recent_worklogs

    p = _write_worklog(tmp_vault, "2020-01-01", "myproj")
    import os
    old_ts = time.time() - 100 * 86400
    os.utime(p, (old_ts, old_ts))

    result = collect_signal_f_recent_worklogs(
        project_root=Path("/x/myproj"), vault_path=tmp_vault, days=7
    )
    assert result.paths == []
    assert result.keywords == set()


def test_signal_f_filters_other_project(tmp_vault: Path) -> None:
    from scripts._signal_collect import collect_signal_f_recent_worklogs

    today = time.strftime("%Y-%m-%d")
    _write_worklog(tmp_vault, today, "OTHERPROJ")

    result = collect_signal_f_recent_worklogs(
        project_root=Path("/x/myproj"), vault_path=tmp_vault, days=7
    )
    assert result.paths == []


def test_signal_f_no_worklog_dir(tmp_vault: Path) -> None:
    from scripts._signal_collect import collect_signal_f_recent_worklogs

    result = collect_signal_f_recent_worklogs(
        project_root=Path("/x/myproj"), vault_path=tmp_vault, days=7
    )
    assert result.paths == []
    assert result.keywords == set()


# ===== 信号 J：prompt 关键词 =====

def test_signal_j_extracts_english_tokens() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws = collect_signal_j_prompt_keywords("Please review the hook implementation")
    assert "hook" in kws
    assert "review" in kws
    assert "implementation" in kws
    assert "the" not in kws


def test_signal_j_extracts_chinese_tokens() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws = collect_signal_j_prompt_keywords("帮我看一下事实优先约束的实现")
    # bigram 语义（2026-07-02 spec）：4 字词以相邻 bigram 形式出现；功能词（帮我/一下）被停用表滤除
    assert "事实" in kws and "实优" in kws and "优先" in kws
    assert "实现" in kws
    assert "帮我" not in kws and "一下" not in kws


def test_signal_j_truncates_long_prompt() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    # 超过 4 KB 的 prompt
    long_prompt = "vault-loader " + ("xxx " * 2000) + "hook"
    kws = collect_signal_j_prompt_keywords(long_prompt)
    assert "vault-loader" in kws or "vault" in kws or "loader" in kws
    # 末尾的 hook 应被截断丢弃
    assert "hook" not in kws


def test_signal_j_empty_prompt() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    assert collect_signal_j_prompt_keywords("") == set()


def test_signal_j_short_words_filtered() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws = collect_signal_j_prompt_keywords("if go to do")
    assert kws == set()  # 所有 < 4 字母英文 + 0 中文长 token


def test_signal_j_strips_slash_command() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws = collect_signal_j_prompt_keywords(
        "/superpowers:brainstorming 当前提示浮层高度会折叠bugid")
    assert "superpowers" not in kws       # slash 命令名被剥
    assert "brainstorming" not in kws
    assert "bugid" in kws                  # 正文话题词保留
    assert any("浮层" in k for k in kws)


def test_signal_j_strips_plain_slash_command() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws = collect_signal_j_prompt_keywords("/commit fix the hook bug")
    assert "commit" not in kws
    assert "hook" in kws


def test_signal_j_pure_slash_command_no_body() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    assert collect_signal_j_prompt_keywords("/help") == set()


def test_signal_j_strip_disabled_keeps_command() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws = collect_signal_j_prompt_keywords("/commit hook bug", strip_slash_command=False)
    assert "commit" in kws


def test_signal_j_non_slash_unaffected() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws = collect_signal_j_prompt_keywords("review the hook implementation")
    assert "review" in kws and "hook" in kws and "implementation" in kws


# ===== 信号 J：英文 token 切分（治路径碎片黏连） =====

def test_signal_j_splits_english_token() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws = collect_signal_j_prompt_keywords("处理 analyze_bugs 模块")
    assert "analyze_bugs" in kws   # 并集保留原 token（零回归）
    assert "analyze" in kws        # 切出子片
    assert "bugs" in kws


def test_signal_j_split_filters_short_subtoken() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    # fix-login-bug：login(5)保留，fix(3)/bug(3) < en_subtoken_min=4 被滤（防召回灾难）
    kws = collect_signal_j_prompt_keywords("分支 fix-login-bug 的问题")
    assert "fix-login-bug" in kws
    assert "login" in kws
    assert "fix" not in kws
    assert "bug" not in kws


def test_signal_j_split_filters_pure_digit() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws = collect_signal_j_prompt_keywords("文件 harvest_201718_20260622 是什么")
    assert "harvest" in kws
    assert "201718" not in kws
    assert "20260622" not in kws


def test_signal_j_split_disabled_keeps_whole() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws = collect_signal_j_prompt_keywords("处理 analyze_bugs 模块", split_english_token=False)
    assert "analyze_bugs" in kws
    assert "analyze" not in kws


def test_signal_j_split_en_subtoken_min_configurable() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    # 放宽到 3 时 bug 子片才出现（默认 4 不出，本测试验证参数生效）
    kws = collect_signal_j_prompt_keywords("分支 fix-login-bug 的问题", en_subtoken_min=3)
    assert "bug" in kws
    assert "fix" in kws


def test_signal_j_flattened_path_token_not_split() -> None:
    """B1：含空段的 flattened 路径/畸形 token（如项目目录 d--work-...-cashbook）不切分，
    避免切出 cashbook/owner 等常见词假命中（task-notification 路径碎片污染根因）。"""
    from scripts._signal_collect import collect_signal_j_prompt_keywords
    kws = collect_signal_j_prompt_keywords("路径 d--work-workspace-owner-cashbook 是什么")
    assert "cashbook" not in kws    # 不被切出
    assert "owner" not in kws
    assert "workspace" not in kws


def test_signal_j_split_still_works_for_normal_compound() -> None:
    """B1 不误伤正常 compound：harvest_201718_... 仍切出 harvest（无空段）。"""
    from scripts._signal_collect import collect_signal_j_prompt_keywords
    kws = collect_signal_j_prompt_keywords("文件 harvest_201718_20260622_112207")
    assert "harvest" in kws
    kws2 = collect_signal_j_prompt_keywords("处理 analyze_bugs 模块")
    assert "analyze" in kws2 and "bugs" in kws2


def test_signal_j_filters_hash_uuid_tokens() -> None:
    """B2：hash/UUID 型 token 过滤——hex+数字≥8、混合字母数字≥16（task-notification 的
    会话UUID/tool-id 碎片不当关键词）。"""
    from scripts._signal_collect import collect_signal_j_prompt_keywords
    kws = collect_signal_j_prompt_keywords(
        "会话 a9ee6be0 与 abd40d47a666e 还有 toolu_01jlfopjalhp6zzsumd6wjtl")
    assert "a9ee6be0" not in kws                       # 8 位 hex 含数字
    assert "abd40d47a666e" not in kws                  # 13 位 hex 含数字（首字符字母才被完整提取）
    assert "01jlfopjalhp6zzsumd6wjtl" not in kws       # 24 位混合
    assert "toolu_01jlfopjalhp6zzsumd6wjtl" not in kws # 30 位混合原 token


def test_signal_j_noise_filter_preserves_real_words() -> None:
    """B2 不误杀真实词：纯 hex 但无数字（deadbeef）、长英文词（implementation）保留。"""
    from scripts._signal_collect import collect_signal_j_prompt_keywords
    kws = collect_signal_j_prompt_keywords("review deadbeef implementation design")
    assert "deadbeef" in kws        # 纯 hex 无数字 → 非 hash 片段，保留
    assert "implementation" in kws  # 14 字符无数字 → 保留


def test_signal_d_commit_keywords_not_split(tmp_git_repo: Path) -> None:
    """范围守护：英文切分仅作用于 J 信号，D（commit）信号共用 _EN_TOKEN_RE 不得被切分。"""
    from scripts._signal_collect import collect_signal_d_commit_keywords
    import subprocess
    (tmp_git_repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "improve analyze_bugs handler"],
                   cwd=tmp_git_repo, check=True)
    kws = collect_signal_d_commit_keywords(tmp_git_repo)
    assert "analyze_bugs" in kws       # D 保留整 token
    assert "analyze" not in kws        # 未被切分
    assert "bugs" not in kws


# ===== collect_recent_commits：近期提交原始展示 =====

def test_collect_recent_commits_basic(tmp_git_repo: Path) -> None:
    from scripts._signal_collect import collect_recent_commits

    commits = collect_recent_commits(tmp_git_repo, 5)
    assert len(commits) == 1
    assert "init" in commits[0]


def test_collect_recent_commits_non_git(tmp_path: Path) -> None:
    from scripts._signal_collect import collect_recent_commits

    assert collect_recent_commits(tmp_path, 5) == []


def test_collect_recent_commits_zero_cap(tmp_git_repo: Path) -> None:
    from scripts._signal_collect import collect_recent_commits

    assert collect_recent_commits(tmp_git_repo, 0) == []


# ===== 索引排除：extra_paths 指向 category 顶层时索引不被注入 =====

def test_signal_a_excludes_index(tmp_path):
    vault = tmp_path
    proj = vault / '项目笔记' / 'demo'
    proj.mkdir(parents=True)
    (proj / 'note.md').write_text('# n', encoding='utf-8')
    # extra_paths 指向 category 顶层,其索引不应被注入
    cat = vault / '缺陷全链路'
    cat.mkdir()
    (cat / '缺陷全链路 索引.md').write_text('# 缺陷全链路 索引', encoding='utf-8')
    (cat / 'real.md').write_text('# r', encoding='utf-8')
    paths = collect_signal_a_project_dir(
        project_root=Path('/x/demo'), vault_path=vault, extra_paths=['缺陷全链路'])
    assert '缺陷全链路/缺陷全链路 索引.md' not in paths
    assert '缺陷全链路/real.md' in paths
    assert '项目笔记/demo/note.md' in paths


def test_signal_a_excludes_root_index(tmp_path):
    # 根索引 未分类 索引.md(父目录名≠未分类)也不应被当普通笔记注入(M3)
    vault = tmp_path
    (vault / '未分类 索引.md').write_text('# 未分类 索引', encoding='utf-8')
    (vault / '游离.md').write_text('# 游离', encoding='utf-8')
    paths = collect_signal_a_project_dir(
        project_root=Path('/x/demo'), vault_path=vault, extra_paths=['.'])
    assert '未分类 索引.md' not in paths
    assert '游离.md' in paths


# ===== CJK bigram 切分（第0层）=====

def test_split_cjk_bigrams_sliding_window() -> None:
    from scripts._signal_collect import _split_cjk_bigrams

    assert _split_cjk_bigrams("唤醒词") == ["唤醒", "醒词"]
    assert _split_cjk_bigrams("预算管理") == ["预算", "算管", "管理"]


def test_split_cjk_bigrams_two_char_run_is_itself() -> None:
    from scripts._signal_collect import _split_cjk_bigrams

    assert _split_cjk_bigrams("崩溃") == ["崩溃"]


def test_split_cjk_bigrams_filters_function_words() -> None:
    from scripts._signal_collect import _split_cjk_bigrams

    # 怎么/么配 中只有 '怎么' 在停用表
    assert _split_cjk_bigrams("怎么配") == ["么配"]
    # 全功能词 → 空
    assert _split_cjk_bigrams("怎么") == []


def test_cjk_function_bigrams_table_is_spec_appendix_a() -> None:
    from scripts._signal_collect import _CJK_FUNCTION_BIGRAMS

    assert len(_CJK_FUNCTION_BIGRAMS) == 108
    assert all(len(w) == 2 for w in _CJK_FUNCTION_BIGRAMS)
    for w in ("怎么", "一下", "继续", "好的", "问题", "看看", "出来"):
        assert w in _CJK_FUNCTION_BIGRAMS
    for w in ("崩溃", "测试", "日志"):  # 内容词绝不入表
        assert w not in _CJK_FUNCTION_BIGRAMS


def test_is_pure_cjk_keywords() -> None:
    from scripts._signal_collect import is_pure_cjk_keywords

    assert is_pure_cjk_keywords({"崩溃"})
    assert is_pure_cjk_keywords({"崩溃", "日志"})
    assert not is_pure_cjk_keywords({"崩溃", "gradle"})
    assert not is_pure_cjk_keywords(set())


def test_signal_j_two_char_cjk_word_tokenized() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    assert collect_signal_j_prompt_keywords("崩溃") == {"崩溃"}
    assert collect_signal_j_prompt_keywords("app闪退") == {"闪退"}  # 'app' <4 字母仍丢弃


def test_signal_j_split_cjk_bigram_off_restores_legacy() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws = collect_signal_j_prompt_keywords("如何优化召回相关性", split_cjk_bigram=False)
    assert kws == {"如何优化召回相关性"}  # 旧 mega-token（{3,}）行为
    assert collect_signal_j_prompt_keywords("崩溃", split_cjk_bigram=False) == set()


def test_signal_j_max_keywords_head_tail_truncation() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    # 长粘贴 + 末尾提问（reverse R1 反例）：头尾截断须保留末尾提问词
    paste = "这是一段很长的构建日志输出内容用于模拟用户粘贴大段文本的场景" * 40
    prompt = paste + "\n请问：如何优化召回相关性以及分词效果"
    kws = collect_signal_j_prompt_keywords(prompt, max_keywords=30)
    assert len(kws) <= 30
    for tail_kw in ("召回", "分词", "优化", "相关"):
        assert tail_kw in kws


def test_signal_j_mixed_doc_order_cap_deterministic() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    kws1 = collect_signal_j_prompt_keywords("gradle 构建 " + "中文填充内容测试样本 " * 60, max_keywords=10)
    kws2 = collect_signal_j_prompt_keywords("gradle 构建 " + "中文填充内容测试样本 " * 60, max_keywords=10)
    assert kws1 == kws2 and len(kws1) <= 10
    assert "gradle" in kws1  # 头部 EN token 保留


def test_signal_j_max_keywords_one_still_caps() -> None:
    """FIX-1 回归：max_keywords=1 时 tail = ordered[-(1//2):] == ordered[-0:] == 整表，
    截断反向失效。修后 tn=0 时 tail 必须为空，最终硬兜底 [:1] 保证只出 1 个词。"""
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    prompt = "gradle 构建 中文填充内容测试样本 召回 分词 相关性 优化"
    kws = collect_signal_j_prompt_keywords(prompt, max_keywords=1)
    assert len(kws) == 1


def test_signal_j_max_keywords_two_caps_at_two() -> None:
    from scripts._signal_collect import collect_signal_j_prompt_keywords

    prompt = "gradle 构建 中文填充内容测试样本 召回 分词 相关性 优化"
    kws = collect_signal_j_prompt_keywords(prompt, max_keywords=2)
    assert len(kws) <= 2


def test_global_cn_token_re_untouched_guard() -> None:
    """D/F 信号守护：全局 _CN_TOKEN_RE 必须保持 {3,}（改它会外溢 D/F 信号）。"""
    from scripts._signal_collect import _CN_TOKEN_RE

    assert _CN_TOKEN_RE.pattern == r"[一-鿿]{3,}"


def test_signal_i_reads_agents_md_for_codex(tmp_path):
    """项目指令文件也可能是 AGENTS.md（Codex 的约定）。

    只读 CLAUDE.md 时，README 承诺的四个逃生阀里「项目注释 disable」那一个在
    Codex 上永远不生效，信号 I 的 tags/extra_paths 同样恒空——且毫无提示，
    用户只会以为自己写错了注释语法。
    """
    from scripts._signal_collect import collect_signal_i_project_claude_md

    (tmp_path / "AGENTS.md").write_text(
        "# AGENTS\n<!-- vault-loader: disable -->\n", encoding="utf-8")
    assert collect_signal_i_project_claude_md(tmp_path).disabled is True


def test_signal_i_merges_both_instruction_files(tmp_path):
    """两个文件同时存在时取并集——本仓库自己就是这个形态。"""
    from scripts._signal_collect import collect_signal_i_project_claude_md

    (tmp_path / "CLAUDE.md").write_text(
        "<!-- vault-loader: tags=[a, b] -->\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "<!-- vault-loader: tags=[c] -->\n", encoding="utf-8")
    result = collect_signal_i_project_claude_md(tmp_path)
    assert result.tags == {"a", "b", "c"}
    assert result.disabled is False


def test_signal_i_disable_in_either_file_wins(tmp_path):
    """停用意图写在任一文件里都应生效。"""
    from scripts._signal_collect import collect_signal_i_project_claude_md

    (tmp_path / "CLAUDE.md").write_text("# nothing special\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "<!-- vault-loader: disable -->\n", encoding="utf-8")
    assert collect_signal_i_project_claude_md(tmp_path).disabled is True
