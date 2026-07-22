# -*- coding: utf-8 -*-
from tests.fixtures.gold_corpus import build_gold_corpus, BROAD_TAGS
from scripts._config_loader import DEFAULT_CONFIG
from scripts._scorer import Signals, topical_score
from scripts._signal_collect import collect_signal_j_prompt_keywords


def test_corpus_size_and_query_count():
    corpus, queries = build_gold_corpus()
    assert len(corpus) >= 200, f"语料应 ≥200 篇，实际 {len(corpus)}"
    assert len(queries) >= 20, f"查询应 ≥20 条，实际 {len(queries)}"


def test_paths_unique():
    corpus, _ = build_gold_corpus()
    paths = [e.path for e in corpus]
    assert len(paths) == len(set(paths)), "语料 path 必须唯一"


def test_relevant_paths_exist_in_corpus():
    corpus, queries = build_gold_corpus()
    known = {e.path for e in corpus}
    for q in queries:
        for p in q.relevant:
            assert p in known, f"查询 {q.prompt!r} 标注了不存在的 path: {p}"


def test_tag_distribution_is_power_law():
    """必须复现真实 Vault 的 tag 幂律：少数泛 tag 覆盖大量笔记 + 大量 singleton。"""
    corpus, _ = build_gold_corpus()
    df = {}
    for e in corpus:
        for t in e.tags:
            df[t] = df.get(t, 0) + 1
    singleton = sum(1 for c in df.values() if c == 1)
    assert singleton / len(df) > 0.5, "singleton tag 应占多数（真实 Vault 为 66%）"
    assert max(df[t] for t in BROAD_TAGS) >= 10, "应存在覆盖大量笔记的泛 tag"


def test_d1_broad_tag_noise_is_real():
    """评审 Finding 1 守卫：D1 泛 tag 干扰必须真正参与排序竞争，不能对全部查询恒得分 0。

    用生产真实链路验证（非孤立断言）：真实 collect_signal_j_prompt_keywords 切词
    + 真实 topical_score 打分 + 生产 DEFAULT_CONFIG["scoring"] 权重。
    断言：至少一条查询下，`干扰/broad-*.md`（D1）里有 >=10 篇获得非零 topical 得分——
    数量门槛（而非仅 >0）确保命中的是「泛 tag 竞争」这一规模化噪声，不是偶然的单篇巧合。
    """
    corpus, queries = build_gold_corpus()
    weights = DEFAULT_CONFIG["scoring"]

    max_d1_hits = 0
    for q in queries:
        kws = collect_signal_j_prompt_keywords(q.prompt, max_keywords=30)
        sig = Signals(prompt_keywords=kws)
        d1_hits = sum(
            1 for e in corpus
            if e.path.startswith("干扰/broad-") and topical_score(e, sig, weights) > 0
        )
        max_d1_hits = max(max_d1_hits, d1_hits)

    assert max_d1_hits >= 10, (
        f"应至少有一条查询让 >=10 篇 D1 泛 tag 干扰笔记（干扰/broad-*.md）获得非零 topical "
        f"得分，实际最多一条查询命中 {max_d1_hits} 篇——D1 未真正参与排序竞争"
    )
