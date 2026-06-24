"""_scorer 单测：信号叠加、上限、mtime 衰减、prompt 加分。"""
from __future__ import annotations

import time

from scripts._frontmatter_reader import Entry
from scripts._scorer import Signals, score


def _default_weights() -> dict:
    return {
        "exact_project_dir": 5,
        "tag_target_set_hit": 3,
        "commit_keyword_hit": 2,
        "commit_keyword_cap": 6,
        "worklog_cooccur": 2,
        "mtime_recent_30d": 1,
        "mtime_recent_90d": 0.5,
        "prompt_tag_hit": 4,
        "prompt_summary_hit": 2,
    }


def test_zero_signals_zero_score() -> None:
    e = Entry(path="foo.md")
    s = score(e, Signals(), _default_weights())
    assert s == 0


def test_exact_project_dir_hits() -> None:
    e = Entry(path="项目笔记/foo/note.md")
    sigs = Signals(project_dir_paths={"项目笔记/foo/note.md"})
    s = score(e, sigs, _default_weights())
    assert s == 5


def test_target_tag_set_intersection() -> None:
    e = Entry(path="x.md", tags=("android", "test"))
    sigs = Signals(target_tags={"android", "ios"})
    assert score(e, sigs, _default_weights()) == 3


def test_target_tag_no_overlap() -> None:
    e = Entry(path="x.md", tags=("swift",))
    sigs = Signals(target_tags={"android"})
    assert score(e, sigs, _default_weights()) == 0


def test_commit_keyword_hits_capped() -> None:
    e = Entry(
        path="x.md",
        tags=("alpha", "beta", "gamma", "delta", "epsilon"),
    )
    sigs = Signals(commit_keywords={"alpha", "beta", "gamma", "delta", "epsilon"})
    # 上限 +6（cap），不应到 +10
    assert score(e, sigs, _default_weights()) == 6


def test_commit_keyword_dedup_across_fields() -> None:
    e = Entry(path="alpha-note.md", tags=("alpha",), summary="alpha 主题")
    sigs = Signals(commit_keywords={"alpha"})
    # alpha 命中 tags + summary + path，但作为同一个关键词只计一次
    assert score(e, sigs, _default_weights()) == 2


def test_worklog_cooccur() -> None:
    e = Entry(path="hook-design.md", summary="设计 SessionStart hook")
    sigs = Signals(worklog_keywords={"hook", "session"})
    # 至少一个关键词共词 → +2（单次加分，不是累加）
    s = score(e, sigs, _default_weights())
    assert s == 2


def test_mtime_30d_recent() -> None:
    now = int(time.time())
    e = Entry(path="x.md", mtime=now - 5 * 86400)
    s = score(e, Signals(), _default_weights())
    assert s == 1


def test_mtime_90d_exclusive_with_30d() -> None:
    now = int(time.time())
    e = Entry(path="x.md", mtime=now - 60 * 86400)
    s = score(e, Signals(), _default_weights())
    assert s == 0.5


def test_mtime_old_no_score() -> None:
    now = int(time.time())
    e = Entry(path="x.md", mtime=now - 200 * 86400)
    s = score(e, Signals(), _default_weights())
    assert s == 0


def test_prompt_tag_hit_only_when_prompt_mode() -> None:
    e = Entry(path="x.md", tags=("hook",))
    sigs_normal = Signals(prompt_keywords=set())
    sigs_prompt = Signals(prompt_keywords={"hook"})
    assert score(e, sigs_normal, _default_weights()) == 0
    assert score(e, sigs_prompt, _default_weights()) == 4


def test_prompt_summary_hit() -> None:
    e = Entry(path="x.md", summary="关于 hook 的设计")
    sigs = Signals(prompt_keywords={"hook"})
    # +2（summary 命中）
    assert score(e, sigs, _default_weights()) == 2


def test_combined_signals() -> None:
    now = int(time.time())
    e = Entry(
        path="项目笔记/foo/note.md",
        tags=("android",),
        summary="hook 设计",
        mtime=now - 10 * 86400,
    )
    sigs = Signals(
        project_dir_paths={"项目笔记/foo/note.md"},  # +5
        target_tags={"android"},                       # +3
        commit_keywords={"hook"},                      # +2 (命中 summary)
        worklog_keywords={"设计"},                     # +2
        # mtime 30d                                    # +1
        prompt_keywords={"hook"},                      # +4 (命中 summary 的关键词，但 prompt 加分单独看 tags/summary 命中)
    )
    # 详细：5 + 3 + 2 (commit) + 2 (worklog) + 1 (mtime) + 2 (prompt_summary) = 15
    # 注：prompt 关键词 hook 在 tags 中不命中（tags 只有 android），在 summary 中命中 → +2
    s = score(e, sigs, _default_weights())
    assert s == 15


def test_custom_weights_applied() -> None:
    e = Entry(path="项目笔记/foo/note.md")
    sigs = Signals(project_dir_paths={"项目笔记/foo/note.md"})
    weights = _default_weights()
    weights["exact_project_dir"] = 99
    assert score(e, sigs, weights) == 99


# ===== topical_score：仅 prompt 命中、不含 context =====

from scripts._scorer import topical_score


def test_topical_score_no_prompt_keywords_zero() -> None:
    e = Entry(path="x.md", tags=("android",), summary="hook 设计")
    sigs = Signals(target_tags={"android"})   # 有 context 但无 prompt 命中
    assert topical_score(e, sigs, _default_weights()) == 0


def test_topical_score_tag_only() -> None:
    e = Entry(path="x.md", tags=("hook",), summary="无关")
    sigs = Signals(prompt_keywords={"hook"})
    assert topical_score(e, sigs, _default_weights()) == 4


def test_topical_score_summary_only() -> None:
    e = Entry(path="x.md", tags=("android",), summary="关于 hook 的设计")
    sigs = Signals(prompt_keywords={"hook"})
    assert topical_score(e, sigs, _default_weights()) == 2


def test_topical_score_tag_and_summary() -> None:
    e = Entry(path="x.md", tags=("hook",), summary="hook 实现")
    sigs = Signals(prompt_keywords={"hook"})
    assert topical_score(e, sigs, _default_weights()) == 6


def test_topical_score_excludes_context() -> None:
    """关键：target_tags 命中 + 近期 mtime，但无 prompt 命中 → topical 仍 0。"""
    import time
    now = int(time.time())
    e = Entry(path="x.md", tags=("claude-code",), summary="无关摘要", mtime=now)
    sigs = Signals(target_tags={"claude-code"}, prompt_keywords={"浮层"})
    assert topical_score(e, sigs, _default_weights()) == 0


# ===== A：ASCII 关键词词边界命中（防 release⊂demo-release 跨连字符/下划线误命中）=====

def test_ascii_keyword_no_match_inside_compound_tag() -> None:
    """通用词 release 不应命中复合标识 tag demo-release（紧邻 '-' 非独立词）。"""
    e = Entry(path="x.md", tags=("demo-release", "skill"), summary="无关摘要内容")
    sigs = Signals(prompt_keywords={"release"})
    assert topical_score(e, sigs, _default_weights()) == 0


def test_ascii_keyword_no_match_inside_compound_summary() -> None:
    """release 不应命中 summary 中 demo-release 的子串（紧邻 '-'）。"""
    e = Entry(path="x.md", tags=("foo",), summary="从零搭建 demo-release skill 全流程")
    sigs = Signals(prompt_keywords={"release"})
    assert topical_score(e, sigs, _default_weights()) == 0


def test_release_does_not_match_demo_release_note() -> None:
    """根因回归：单词 release 不再命中 demo-release 笔记（tag+summary 均含该复合词）→ topical 0。"""
    e = Entry(
        path="项目笔记/demo-release/x.md",
        tags=("demo-release", "skill"),
        summary="从零搭建 demo-release skill：车载项目发版提测全流程自动化",
    )
    sigs = Signals(prompt_keywords={"release"})
    assert topical_score(e, sigs, _default_weights()) == 0


def test_ascii_keyword_exact_tag_still_hits() -> None:
    """精确等于 tag 仍命中（边界两侧为串首/尾）。"""
    e = Entry(path="x.md", tags=("release",), summary="无关")
    sigs = Signals(prompt_keywords={"release"})
    assert topical_score(e, sigs, _default_weights()) == 4


def test_ascii_keyword_standalone_word_in_summary_hits() -> None:
    """release 作为独立词（空白分隔）在 summary 命中。"""
    e = Entry(path="x.md", tags=("foo",), summary="执行 release 发布流程")
    sigs = Signals(prompt_keywords={"release"})
    assert topical_score(e, sigs, _default_weights()) == 2


def test_ascii_keyword_followed_by_cjk_hits_summary() -> None:
    """release 后紧邻 CJK（非 [A-Za-z0-9_-]）仍属独立词 → 命中。"""
    e = Entry(path="x.md", tags=("foo",), summary="release流程上线")
    sigs = Signals(prompt_keywords={"release"})
    assert topical_score(e, sigs, _default_weights()) == 2


def test_cjk_keyword_substring_preserved_tag() -> None:
    """CJK 子概念命中保留：语音 ⊂ 语音助手（无分隔符语言不退化为精确匹配）。"""
    e = Entry(path="x.md", tags=("语音助手",), summary="无关摘要内容")
    sigs = Signals(prompt_keywords={"语音"})
    assert topical_score(e, sigs, _default_weights()) == 4


def test_cjk_keyword_substring_preserved_summary() -> None:
    e = Entry(path="x.md", tags=("foo",), summary="车载语音助手设计说明")
    sigs = Signals(prompt_keywords={"语音"})
    assert topical_score(e, sigs, _default_weights()) == 2


def test_empty_keyword_does_not_match() -> None:
    """防御契约：空串关键词不命中任何文本（_kw_in_text 兜底；fullmatch('')=None 否则落子串恒 True）。"""
    from scripts._scorer import _kw_in_text
    assert _kw_in_text("", "任意文本 abc") is False
    assert _kw_in_text("", "") is False
