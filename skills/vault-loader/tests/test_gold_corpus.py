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


# ---- D6 灰区条目结构守卫（Task 4）----
# 与 D5 的 MIN_D5_ENTRIES 系列同一意图：钉住语料形态，让「改动这组」必须先面对
# 它承担的职责，而不是改完发现某条基线莫名其妙变了却不知道为什么。

MIN_D6_ENTRIES = 80
D6_EXACT_SCORE = 4.0        # == min_topical_score，灰区的定义就是「恰好卡在闸门线」


def _d6(corpus):
    return [e for e in corpus if e.path.startswith("干扰/gray-")]


def test_d6_entry_count():
    corpus, _ = build_gold_corpus()
    assert len(_d6(corpus)) >= MIN_D6_ENTRIES, (
        f"D6 灰区条目应 >= {MIN_D6_ENTRIES} 篇，实际 {len(_d6(corpus))}——"
        f"篇数直接决定 admitted 规模，减篇会让 admitted_k 截断重新测不到")


def test_d6_never_scores_above_the_gate():
    """**本组最要紧的一条**：灰区得分不得超过闸门值。

    灰区的作用是撑起 admitted 规模，不是参与排序竞争。一旦某条灰区拿到 5 分
    （keywords 命中）或 6 分（tag+summary），它就会与真正相关的笔记同分甚至反超
    ——实测有三条查询的 ground truth 本身只有 5 分。初版 D6 正是走 keywords 命中，
    导致 nDCG@10 从 0.90 掉到 0.855、tag-IDF 相对 flat 的改进比掉到 2.7%，
    test_gold_ranking.py 三条基线全红。

    所以这里钉的是**上界**：任何查询下都不得 > 4.0。summary 蹭到查询词、
    keywords 被填回内容、tag 重复导致 IDF 变化，三类改动都会在这里被拦下。
    """
    corpus, queries = build_gold_corpus()
    rel = DEFAULT_CONFIG["relevance"]
    weights = DEFAULT_CONFIG["scoring"]
    worst = 0.0
    offender = None
    for q in queries:
        sig = Signals(prompt_keywords=collect_signal_j_prompt_keywords(q.prompt))
        for e in _d6(corpus):
            s = topical_score(e, sig, weights, rel, corpus)
            if s > worst:
                worst, offender = s, (e.path, q.prompt)
    assert worst <= D6_EXACT_SCORE, (
        f"D6 灰区条目得分越过闸门线 {D6_EXACT_SCORE}：{offender} 得 {worst}。"
        f"灰区只应 tag 命中（4×IDF 1.0）；summary(+2) 或 keywords(+5) 命中都会让它"
        f"反超真正相关的笔记并压垮 test_gold_ranking.py 的排序基线。")


def test_d6_actually_passes_the_gate_for_every_query():
    """反向：灰区必须真的进得来，否则这组等于没加（只是 80 篇不参与竞争的死重）。"""
    corpus, queries = build_gold_corpus()
    rel = DEFAULT_CONFIG["relevance"]
    weights = DEFAULT_CONFIG["scoring"]
    min_topical = rel["min_topical_score"]
    for q in queries:
        sig = Signals(prompt_keywords=collect_signal_j_prompt_keywords(q.prompt))
        n = sum(1 for e in _d6(corpus)
                if topical_score(e, sig, weights, rel, corpus) >= min_topical)
        assert n >= 20, (
            f"查询 {q.prompt!r} 下只有 {n} 篇灰区条目越过闸门（应 >=20）——"
            f"词池覆盖不均，该查询的 admitted 规模仍是病态的个位数")


def test_admitted_scale_exceeds_truncation_threshold():
    """admitted 规模必须超过 `admitted_k`，否则落盘截断与截断前聚合永远测不到。

    这是 D6 存在的**硬指标**。加入 D6 之前实测 median admitted = 2 / 220 篇
    （excluded 99.1%），而真实 Vault 是 excluded 79.4%——`admitted_k=20` 的截断、
    `arm_counts` 的截断前聚合在 gold 侧一次都触发不到。

    注：本组把 median excluded 压到约 87%，仍未对齐 79.4%。这是算术硬约束不是没做完：
    设灰区 G 篇且对全部查询都命中，占比 = (2+G)/(220+G)，要到 20% 需 G≈53 且每篇
    覆盖全部主题（≈20 个 tag/keywords），那种笔记现实中不存在。完全对齐要重构查询与
    条目的词汇密度，属 plan 层议题。故这里断言的是**可达且有意义**的那条线。
    """
    import statistics

    corpus, queries = build_gold_corpus()
    rel = DEFAULT_CONFIG["relevance"]
    weights = DEFAULT_CONFIG["scoring"]
    min_topical = rel["min_topical_score"]
    admitted_counts = []
    for q in queries:
        sig = Signals(prompt_keywords=collect_signal_j_prompt_keywords(q.prompt))
        admitted_counts.append(sum(
            1 for e in corpus
            if topical_score(e, sig, weights, rel, corpus) >= min_topical))
    med = statistics.median(admitted_counts)
    assert med > 20, (
        f"median admitted={med} 未超过 admitted_k=20——落盘截断与截断前 arm_counts "
        f"聚合在 gold 语料上仍然测不到。各查询 admitted: {sorted(admitted_counts)}")
    assert min(admitted_counts) >= 20, (
        f"存在 admitted 仍为个位数的查询（最小 {min(admitted_counts)}），"
        f"分布不均：{sorted(admitted_counts)}")
