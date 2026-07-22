# -*- coding: utf-8 -*-
"""排序 gold 集：基线固化 + 自证区分力。

自证区分力是本文件的核心：若 gold 集对「随机 / 纯 mtime / 纯长度」这三种
已知很差的打分器给出接近基线的分数，说明它没有区分力、本身是空网，必须重做。
"""
from __future__ import annotations

import random

from scripts._config_loader import DEFAULT_CONFIG
from scripts._scorer import Signals, topical_score
from scripts._signal_collect import collect_signal_j_prompt_keywords
from tests._metrics import ndcg_at_k, mrr, recall_at_k
from tests.fixtures.gold_corpus import build_gold_corpus

# 直接取生产配置而非硬编码副本：Task 8 将修改
# DEFAULT_CONFIG["scoring"]["prompt_keyword_hit"]（3→5），若此处仍是字面量硬编码副本，
# 本文件的基线守卫会永远验证过时权重、Task 8 的改动不会被此回归守卫覆盖（见 test_gold_corpus.py
# 同款写法）。
WEIGHTS = DEFAULT_CONFIG["scoring"]

# 生产算法在 gold 集上的基线（Task 6 Step 3 用实测值填入，作为回归下界）。
# 刻意用 None 而非 0.0：占位 0.0 会让 `>= BASELINE` 恒真，成为空网断言；
# None 则在未填入时直接让测试失败，逼出「忘记填基线」这一失误。
# 实测（2026-07-21）：ndcg@10=0.9255 mrr=0.9420 recall@5=0.9783（实测值 - 0.02，留浮动余量）。
BASELINE_NDCG = 0.90
BASELINE_MRR = 0.92
BASELINE_RECALL = 0.95


def rank_with(scorer_fn, corpus, prompt):
    """用 scorer_fn(entry, kws) 打分并降序排序，返回 path 列表。"""
    kws = collect_signal_j_prompt_keywords(prompt, max_keywords=30)
    scored = [(scorer_fn(e, kws), e) for e in corpus]
    # 稳定 tie-break：分数相同按 mtime 新→旧（镜像生产 prompt_submit_load 的排序键）
    scored.sort(key=lambda x: (-x[0], -(x[1].mtime or 0)))
    return [e.path for _, e in scored]


def _production_scorer(entry, kws):
    return topical_score(entry, Signals(prompt_keywords=set(kws)), WEIGHTS)


def _eval(scorer_fn) -> dict:
    corpus, queries = build_gold_corpus()
    n = len(queries)
    tot_ndcg = tot_mrr = tot_rec = 0.0
    for q in queries:
        ranked = rank_with(scorer_fn, corpus, q.prompt)
        tot_ndcg += ndcg_at_k(ranked, q.relevant, 10)
        tot_mrr += mrr(ranked, q.relevant)
        tot_rec += recall_at_k(ranked, q.relevant, 5)
    return {"ndcg@10": tot_ndcg / n, "mrr": tot_mrr / n, "recall@5": tot_rec / n}


def test_gold_set_has_discriminating_power() -> None:
    """gold 集必须能杀掉三种已知很差的打分器。不通过则 gold 集本身是空网。"""
    baseline = _eval(_production_scorer)["ndcg@10"]

    rng = random.Random(20260721)
    degraded = {
        "random": _eval(lambda e, kws: rng.random()),
        "mtime_only": _eval(lambda e, kws: float(e.mtime or 0) / 1e10),
        "length_only": _eval(lambda e, kws: float(len(e.summary))),
    }

    # 实测间隔备忘（2026-07-21）：baseline≈0.9255，三个劣化器≈0.0055/0.0000/0.0000，
    # 与 0.15 阈值之间还有约 0.77 的未用余量——中度退化（间隔从 0.92 降到 0.16）会静默
    # 通过。0.15 是「灾难性崩塌」的绊线，不是细粒度回归探测器；本次刻意不收紧
    # （brief 明令禁止调整阈值），后续维护者若想做更敏感的回归检测需另加断言，而非改这条。
    for name, m in degraded.items():
        assert m["ndcg@10"] < baseline - 0.15, (
            f"劣化打分器 {name} 的 nDCG@10={m['ndcg@10']:.3f} 未显著低于基线 "
            f"{baseline:.3f}（要求间隔 >0.15）。gold 集区分力不足，"
            f"应加强干扰项设计，而不是调松本阈值。"
        )


def test_production_baseline_is_recorded() -> None:
    """固化当前生产算法的基线数值；Task 8 改权重后不得低于此值。

    基线数值在 Task 6 首次实施时实测填入，之后作为回归下界。
    """
    assert BASELINE_NDCG is not None and BASELINE_MRR is not None \
        and BASELINE_RECALL is not None, (
        "基线常量仍是 None——请先跑 test_report_gold_metrics 拿到实测值，"
        "再把三个常量替换为「实测值 - 0.02」。占位值会让本测试变成空网断言。")
    m = _eval(_production_scorer)
    assert m["ndcg@10"] >= BASELINE_NDCG, f"nDCG@10 回归: {m['ndcg@10']:.3f} < {BASELINE_NDCG}"
    assert m["mrr"] >= BASELINE_MRR, f"MRR 回归: {m['mrr']:.3f} < {BASELINE_MRR}"
    assert m["recall@5"] >= BASELINE_RECALL, f"Recall@5 回归: {m['recall@5']:.3f} < {BASELINE_RECALL}"


def test_tag_idf_improves_or_holds_baseline() -> None:
    """新权重（tag-IDF + keywords=5）在 gold 集上不得低于旧基线。"""
    from scripts._scorer import build_tag_df

    corpus, queries = build_gold_corpus()
    entries = {e.path: e for e in corpus}
    df = build_tag_df(entries)
    n = len(entries)
    new_w = dict(WEIGHTS, prompt_keyword_hit=5)

    def new_scorer(e, kws):
        return topical_score(e, Signals(prompt_keywords=set(kws)), new_w,
                             tag_df=df, n_docs=n, tag_idf_floor=0.5)

    tot = 0.0
    for q in queries:
        tot += ndcg_at_k(rank_with(new_scorer, corpus, q.prompt), q.relevant, 10)
    new_ndcg = tot / len(queries)
    print(f"\n[tag-idf] ndcg@10={new_ndcg:.4f}  baseline={BASELINE_NDCG}")
    assert new_ndcg >= BASELINE_NDCG, (
        f"tag-IDF 使 nDCG@10 劣化: {new_ndcg:.4f} < {BASELINE_NDCG}")


def test_tag_idf_measurably_beats_flat_on_gold() -> None:
    """tag-IDF 必须在 gold 集上可测量地优于 flat 权重（tag_df=None）。
    否则 tag-IDF 退化成 no-op 时无红测——test_tag_idf_*_baseline 只测 >=地板、
    对 +0.009 的实际贡献盲。delta 阈值 0.005 < 实测 0.009，留余量但足以杀 no-op。"""
    from scripts._scorer import build_tag_df

    corpus, queries = build_gold_corpus()
    entries = {e.path: e for e in corpus}
    df = build_tag_df(entries)
    n = len(entries)

    def with_idf(e, k):
        return topical_score(e, Signals(prompt_keywords=set(k)), WEIGHTS,
                             tag_df=df, n_docs=n, tag_idf_floor=0.5)

    def flat(e, k):
        return topical_score(e, Signals(prompt_keywords=set(k)), WEIGHTS)  # tag_df=None

    def nd(fn):
        return sum(ndcg_at_k(rank_with(fn, corpus, q.prompt), q.relevant, 10)
                   for q in queries) / len(queries)

    d = nd(with_idf) - nd(flat)
    print(f"\n[tag-idf delta] with_idf={nd(with_idf):.4f} flat={nd(flat):.4f} delta={d:.4f}")
    assert d > 0.005, f"tag-IDF 须可测量优于 flat 权重，实际 delta={d:.4f}（no-op 会使其≈0）"


def test_report_gold_metrics() -> None:
    """打印当前指标供实施者填基线；同时断言 gold 集本身没有完全失效。

    （不写成无断言的打印函数：那会被代码评审判为「测试什么都不验证」，
    而且真出问题时不会红。这里的下界 0 是最弱的健全性检查，不是基线。）
    """
    m = _eval(_production_scorer)
    print(f"\n[gold baseline] ndcg@10={m['ndcg@10']:.4f} "
          f"mrr={m['mrr']:.4f} recall@5={m['recall@5']:.4f}")
    assert m["ndcg@10"] > 0, "生产算法在 gold 集上 nDCG 为 0 —— 语料或查询标注有误"
    assert m["mrr"] > 0, "生产算法在 gold 集上 MRR 为 0 —— 语料或查询标注有误"
