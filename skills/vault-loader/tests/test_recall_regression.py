# -*- coding: utf-8 -*-
"""召回回归 gold 集（2026-07-02 spec §9）：脱敏合成条目 + 代表性查询。
数字锚点来自真实 862 篇 PoC（docs/superpowers/poc/，作者本地），此处以
行为不变量形式固化：正样本可召回 / 负样本零注入 / 弱泛词不升全文。"""
from scripts._frontmatter_reader import Entry
from scripts._scorer import (Signals, topical_score, has_keyword_hit,
                             has_strong_evidence, is_archived)
from scripts._signal_collect import collect_signal_j_prompt_keywords

WEIGHTS = {"exact_project_dir": 5, "tag_target_set_hit": 3, "commit_keyword_hit": 2,
           "commit_keyword_cap": 6, "worklog_cooccur": 2, "mtime_recent_30d": 1,
           "mtime_recent_90d": 0.5, "prompt_tag_hit": 4, "prompt_summary_hit": 2,
           "prompt_keyword_hit": 3}
MIN_TOPICAL = 4

FIXTURE = [
    Entry(path="n/budget.md", tags=("记账", "预算管理"), summary="预算管理功能实施与月周期配置"),
    Entry(path="n/crash.md", tags=("崩溃定位",), summary="空指针崩溃排查与堆栈分析"),
    Entry(path="n/build.md", tags=("gradle", "构建"), summary="gradle 构建内存与代理配置"),
    Entry(path="n/log.md", tags=("日志",), summary="日志目录结构与轮转策略"),
    Entry(path="n/arch.md", tags=("spec", "archived", "预算管理"), summary="已归档的预算管理设计文档"),
]


def _recall(prompt: str, exclude_archived: bool = True):
    kws = collect_signal_j_prompt_keywords(prompt, max_keywords=30)
    sig = Signals(prompt_keywords=kws)
    out = []
    for e in FIXTURE:
        if exclude_archived and is_archived(e, {"archived"}):
            continue
        t = topical_score(e, sig, WEIGHTS)
        if t >= MIN_TOPICAL or has_keyword_hit(e, kws):
            out.append((t, e))
    return kws, sorted(out, key=lambda x: -x[0])


def test_positive_sentence_queries_recall() -> None:
    for prompt, expect_path in [("实施预算管理", "n/budget.md"),
                                ("gradle 构建内存不够怎么办", "n/build.md"),
                                ("如何排查空指针崩溃", "n/crash.md")]:
        _, cands = _recall(prompt)
        assert any(e.path == expect_path for _, e in cands), prompt


def test_negative_queries_zero_injection() -> None:
    # 含既往回归样本：剥 slash 后不得有任何候选
    for prompt in ("/superpowers:brainstorming 当前提示浮层高度会折叠bugid",
                   "今天天气怎么样", "帮我写个贪吃蛇游戏"):
        _, cands = _recall(prompt)
        assert cands == [], prompt


def test_archived_excluded_but_present_without_filter() -> None:
    _, with_filter = _recall("预算管理设计")
    assert all(e.path != "n/arch.md" for _, e in with_filter)
    _, without = _recall("预算管理设计", exclude_archived=False)
    assert any(e.path == "n/arch.md" for _, e in without)


def test_weak_bigram_hits_never_strong_evidence() -> None:
    """单个 CJK bigram 命中（链数=1）不构成全文强证据；连续原词命中（多链佐证）构成。"""
    from scripts.prompt_submit_load import _hit_keywords

    kws_weak = collect_signal_j_prompt_keywords("看看日志输出")
    hits_weak = _hit_keywords(FIXTURE[3], kws_weak)          # n/log.md
    assert not has_strong_evidence(hits_weak)

    kws_strong = collect_signal_j_prompt_keywords("实施预算管理")
    hits_strong = _hit_keywords(FIXTURE[0], kws_strong)      # n/budget.md 含连续词「预算管理」
    assert has_strong_evidence(hits_strong)
