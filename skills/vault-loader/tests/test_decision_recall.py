# -*- coding: utf-8 -*-
"""正向决策面守卫：23 条 gold 查询过 decide_injection 的召回率基线 + 自证区分力。

与 test_gold_ranking.py（排序质量）、test_false_injection.py（负查询零注入）互补：
本文件校验决策面 decide_injection 本身——即精度闸门 + 去重 + 全文选择这一整条真实
生产链路——对正样本的召回率，而非孤立调用 topical_score() 绕过闸门逻辑。

自证区分力方法论同上述两个姊妹文件：若把精度闸门调到几乎全拒绝
（min_topical_score=99）后 recall 仍接近基线，说明基线断言对"闸门被调死"这类
回归不敏感，是空网守卫。
"""
from __future__ import annotations

import copy

from scripts._config_loader import DEFAULT_CONFIG
from scripts._decision import decide_injection, StateView
from scripts._scorer import Signals, is_archived
from scripts._signal_collect import collect_signal_j_prompt_keywords
from tests.fixtures.gold_corpus import build_gold_corpus

# 实测（2026-08-03）：decide_injection 在 220 篇 gold 语料 23 条查询上 recall=0.9783
# （见 test_gold_recall_through_decision_face 的 print 输出）。写死为「实测值 - 0.02」
# 并按 test_gold_ranking.py:26-29 先例向下取 2 位小数 → 0.95（同 0.9783-0.02=0.9583
# 更保守，留更大浮动余量）。
# 刻意用 None 而非 0.0：占位 0.0 会让 `>=` 恒真成为空网断言；None 则在未填入时
# 直接让测试失败，逼出"忘记填基线"这一失误。
#
# fix round 1（F1）：补第1层 archived 预过滤后重跑，数值不变（仍 0.9783/0.6304）——
# gold_corpus.py 当前没有 tags 恰为 "archived" 的条目（grep 核实：唯一相关的
# "archive" 主题组 tag 实际是"归档过滤"，非"archived"），故过滤前后语料集合相同。
DECISION_RECALL_BASELINE = 0.95


def _recall_at_admitted(config: dict) -> float:
    """对 23 条 gold 查询跑 decide_injection，逐条统计 relevant 命中 admitted 的
    比例（宏平均召回率）。

    与 tests/_metrics.py::recall_at_k 同语义但不做 top-k 截断——decide_injection
    的 admitted 是精度闸门后的候选全集，不含 max_notes 渲染层截断（那是
    prompt_submit_load 的展示层逻辑，不属于决策面职责，本守卫只测决策面）。

    第1层 archived 预过滤（fix round 1，F1）：生产 prompt_submit_load.py:311-312
    在调 decide_injection 前先 `{p: e for p, e in entries.items() if not
    is_archived(e, exclude_tags)}`——active_entries 须由调用方完成该过滤
    （_decision.py:102 docstring 亦有此约定）。此处逐字对齐同一取法
    （exclude_tags = 小写化的 exclude_note_tags 集合），使 tag_df/n_docs 与生产
    同口径：archived 笔记不应抬高分母，也不该参与排序竞争。
    """
    corpus, queries = build_gold_corpus()
    weights = config["scoring"]
    rel_cfg = config["relevance"]
    exclude_tags = {t.lower() for t in rel_cfg.get("exclude_note_tags", [])}
    active_entries = {e.path: e for e in corpus if not is_archived(e, exclude_tags)}

    total = 0.0
    for q in queries:
        kws = collect_signal_j_prompt_keywords(
            q.prompt,
            rel_cfg.get("strip_slash_command", True),
            rel_cfg.get("split_english_token", True),
            rel_cfg.get("en_subtoken_min", 4),
            split_cjk_bigram=rel_cfg.get("split_cjk_bigram", True),
            max_keywords=rel_cfg.get("max_prompt_keywords", 30),
        )
        signals = Signals(prompt_keywords=kws)
        decision = decide_injection(active_entries, signals, weights, config, StateView())
        admitted_paths = {ed.path for ed in decision.admitted}
        hit = sum(1 for p in q.relevant if p in admitted_paths)
        total += hit / len(q.relevant)
    return total / len(queries)


def test_gold_recall_through_decision_face() -> None:
    recall = _recall_at_admitted(DEFAULT_CONFIG)
    print(f"\n[decision recall] recall={recall:.4f} baseline={DECISION_RECALL_BASELINE}")
    assert DECISION_RECALL_BASELINE is not None, (
        "DECISION_RECALL_BASELINE 仍是 None——请先跑本测试拿到实测值（见上方 print），"
        "再把常量替换为「实测值 - 0.02」。占位 None 会让本测试直接失败，逼出"
        "「忘记填基线」这一失误。")
    assert recall >= DECISION_RECALL_BASELINE, (
        f"decision-face recall 回归: {recall:.4f} < {DECISION_RECALL_BASELINE}")


def test_recall_guard_kills_admit_none_mutant() -> None:
    """自证区分力：min_topical_score 调到 99 必须让 recall 显著低于基线（>0.15）。

    注：min_topical=99 并非纯粹的"全不放行"——decide_injection 里 keyword_bypass
    分支（entry.keywords 命中且 has_keyword_hit=True）不受 min_topical 门槛约束
    （见 scripts/_decision.py 第161-165行注释：该旁路仅当 prompt_keyword_hit<
    min_topical 时才复活；min_topical=99 时 prompt_keyword_hit=5<99 恒成立，旁路
    复活），故仍有部分强 keywords 命中的条目被放行。但 gold 语料里只有"-high"
    条目填了 keywords 字段，"-mid"条目 keywords=()（见 gold_corpus.py:92），
    故所有中相关（标 1 分）的"-mid"条目必被拒收；且"-high"条目若无 query 词
    命中其 keywords 具体内容，也一样被拒收。recall 仍应显著恶化。

    实测（2026-08-03）：mutant recall=0.6304，与基线 0.95 间隔 0.3196，远超 0.15
    阈值——守卫确有区分力（非"碰巧也不太低"）。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["relevance"]["min_topical_score"] = 99
    recall = _recall_at_admitted(cfg)
    print(f"\n[min_topical=99 mutant] recall={recall:.4f}")
    assert DECISION_RECALL_BASELINE is not None, "先完成基线填入（见上一条测试）"
    assert recall < DECISION_RECALL_BASELINE - 0.15, (
        f"min_topical=99 变异未显著恶化 recall（{recall:.4f} vs baseline "
        f"{DECISION_RECALL_BASELINE}）——正向守卫空网，需重新设计变异或指标。")
