# -*- coding: utf-8 -*-
"""负查询 false-injection-rate 守卫：语料中不存在相关笔记的任务指令型 prompt，
决策层理应零注入或极少注入。

自证区分力是本文件的核心（与 test_gold_ranking.py 同一方法论）：若把闸门退化为
"全放行"（admit_all mutant），false-injection 必须显著恶化——否则基线断言对
"闸门被拆掉"这类回归不敏感，是空网断言。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts._config_loader import DEFAULT_CONFIG
from scripts._decision import Decision, EntryDecision, StateView, decide_injection
from scripts._scorer import Signals
from scripts._signal_collect import collect_signal_j_prompt_keywords
from tests.fixtures.gold_corpus import NEGATIVE_QUERIES, build_gold_corpus

# 实测（2026-08-03，fix round 1，见 task-4-report.md）：D1~D4 干扰语料的措辞不含
# 负查询里的高频实词组合，对全部 8 条负查询恒得分 0——round 1 首版把这一「无压力测出
# 的 0」误当「闸门天然拦住」写成基线，被评审 C1 判定为空网守卫（reviewer 探针：
# min_topical 放松到 2 时 7/8 条查询仍无变化，证明当时的 0 与闸门强度无关）。
# fix round 1 补 D5（8 篇内嵌高频实词复合术语的干扰笔记，见 gold_corpus.py 同名段）后
# 重测，生产 decide_injection 在同一 8 条查询上 false injection 总数 = 26，构成
# （逐条 query → 命中的 D5 条目数，全部来自 D5，D1~D4 贡献仍为 0）：
#   Q1(显示颜色改成蓝色字段顺序提交)=6  Q2(间距不一致修改后统一显示)=5
#   Q3(字段后面空格修改提交)=4          Q4(界面显示位置移动)=2
#   Q5(三个文件名字改成一致格式)=2      Q6(表格宽度调大显示不全省略)=2
#   Q7(按钮颜色改深点击状态一致)=3      Q8(注释错别字修改提交)=2
#   合计 6+5+4+2+2+2+3+2=26。写死为 26（生产精度闸门在「语料含真实病理压力」下的
# 真实观测值，非留余量的估计数）；判定余量改由 test_guard_kills_admit_all_mutant
# 的绝对下限覆盖（I1：max(baseline*3, 50)），不在本常量里预留虚假余量——虚假余量
# 会让「闸门轻微退化但仍 <=N」的真实回归被基线掩盖。
FALSE_INJECTION_BASELINE = 26

# ---- 下界与结构守卫（fix 批 C / T1）----
# 单边上界是**可被无声掏空的守卫**：实测把 gold_corpus.py 的 D5 干扰组整组移除
# （`for ... in d5_specs[:0]`），total 由 26 掉到 **0**，本文件 3 例仍全绿；把
# NEGATIVE_QUERIES 由 8 条截到 1 条，total=6，同样全绿。D5 是 26 这个数字的唯一来源
# ——讽刺的是 D5 正是为「制造真实压力」才补进来的，却没有任何守卫钉住它。
# 故补三道：产出下界 + 负查询条数下界 + D5 结构完整性（含每个高频实词的出现面数）。
FALSE_INJECTION_FLOOR = 20
MIN_NEGATIVE_QUERIES = 8
MIN_D5_ENTRIES = 8
# D5 每个高频实词在 tags/keywords 里的最少「出现面」数（实测当前分布，见
# gold_corpus.py D5 段注释：字段/一致/显示 各 2、颜色/修改/提交 各 1）。
# 逐词校验而非只数篇数——只数篇数时把 8 篇换成 8 篇不含实词的空壳仍能蒙混过关。
D5_PRESSURE_WORD_FACES = {"字段": 2, "一致": 2, "显示": 2,
                          "颜色": 1, "修改": 1, "提交": 1}


def _false_injections(decide_fn) -> int:
    corpus, _queries = build_gold_corpus()
    active_entries = {e.path: e for e in corpus}
    total = 0
    for q in NEGATIVE_QUERIES:
        kws = collect_signal_j_prompt_keywords(q, max_keywords=30)
        d = decide_fn(active_entries, Signals(prompt_keywords=kws),
                      DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG, StateView())
        total += len(d.admitted)
    return total


def _admit_all(active_entries, signals, weights, config, state) -> Decision:
    """变异自证：把决策闸门整个退化为"全放行"——不论精度闸门/关键词门槛/去重，
    active_entries 里的每一篇都判定为 admitted。用于证明
    test_false_injection_not_above_baseline 这条基线断言确有区分力：
    闸门被拆掉时 false injection 必须显著恶化，而不是碰巧也是 0。"""
    admitted = [
        EntryDecision(path=path, topical=0.0, total=0.0, hits=[],
                      admitted=True, admit_arm="mutant_admit_all", dedup="")
        for path in active_entries
    ]
    return Decision(admitted=admitted, excluded=[], fulltext_path=None,
                     fulltext_arm="", any_relevant=True, relaxed=False, gate_reason="")


def test_false_injection_not_above_baseline() -> None:
    total = _false_injections(decide_injection)
    print(f"\n[false-injection] total={total} baseline={FALSE_INJECTION_BASELINE}")
    assert total <= FALSE_INJECTION_BASELINE, (
        f"负查询 false injection 总数 {total} 超过基线 {FALSE_INJECTION_BASELINE}——"
        f"任务指令型高频实词把语料中本不相关的笔记推过了精度闸门。")


def test_false_injection_pressure_not_below_floor() -> None:
    """下界守卫：total 跌破下界说明**语料的压力源**被削弱，上界断言已成空网。

    与 `test_false_injection_not_above_baseline` 合起来构成区间守卫：
    上界拦「闸门放松」，下界拦「压力被抽走后上界形同虚设」。
    """
    total = _false_injections(decide_injection)
    print(f"\n[false-injection floor] total={total} floor={FALSE_INJECTION_FLOOR}")
    assert total >= FALSE_INJECTION_FLOOR, (
        f"负查询 false injection 总数 {total} 跌破下界 {FALSE_INJECTION_FLOOR}——"
        f"D5 干扰组或负查询集被削弱，`<= {FALSE_INJECTION_BASELINE}` 那条上界断言"
        f"已成空网（实测：D5 整组移除 → total=0，上界仍绿）。\n"
        f"若这确是有意收紧闸门带来的下降，请同时下调 FALSE_INJECTION_BASELINE 与"
        f"本下界并在此记录新观测值；前提是下面两条结构守卫仍绿（语料压力未被抽走）。")


def test_negative_query_set_not_weakened() -> None:
    """负查询集不得被削减：条数是 false-injection 总数的直接乘数。

    实测截到 1 条时 total 由 26 掉到 6，而上界断言仍绿。
    """
    assert len(NEGATIVE_QUERIES) >= MIN_NEGATIVE_QUERIES, (
        f"负查询只剩 {len(NEGATIVE_QUERIES)} 条（应 >= {MIN_NEGATIVE_QUERIES}）——"
        f"false-injection 基线会随之塌缩，守卫失去意义。")
    assert len(set(NEGATIVE_QUERIES)) == len(NEGATIVE_QUERIES), "负查询存在重复条目"


def test_d5_distractor_group_intact() -> None:
    """D5 干扰组结构完整性：篇数 + 每个高频实词的出现面数。

    D5 是 false-injection 基线的**唯一**压力来源（D1~D4 对全部负查询恒得分 0），
    整组移除后 total=0 且原有断言全绿——本条就是钉住它的守卫。
    """
    corpus, _queries = build_gold_corpus()
    d5 = [e for e in corpus if e.path.startswith("干扰/d5-")]
    assert len(d5) >= MIN_D5_ENTRIES, (
        f"D5 干扰组只剩 {len(d5)} 篇（应 >= {MIN_D5_ENTRIES}）——"
        f"false-injection 基线的唯一压力源被抽走，上界断言随即成为空网。")
    for word, min_faces in D5_PRESSURE_WORD_FACES.items():
        faces = [term for e in d5 for term in (*e.tags, *e.keywords) if word in term]
        assert len(faces) >= min_faces, (
            f"高频实词「{word}」在 D5 的复合术语出现面只剩 {len(faces)} 处"
            f"（应 >= {min_faces}）：{faces}。\n"
            f"这些「泛词 ⊂ 复合术语」正是要复现的 CJK 子串匹配旁路病理，"
            f"抽掉后负查询在语料里重新变成零信号。")


def test_guard_kills_admit_all_mutant() -> None:
    """自证区分力：闸门退化为全放行时，false injection 必须显著恶化
    （>max(基线*3, 50)）。若本测试也接近基线，说明 test_false_injection_not_above_baseline
    是空网断言——对"闸门被整个拆掉"这种最粗暴的回归都测不出来。

    评审 I1：单用 baseline*3 在 baseline 较小时阈值会跟着塌缩（本例 26*3=78 尚可，
    但历史上 baseline=0 时曾令阈值退化为 0、任何 mutant_total>0 都能通过），
    改用 max(baseline*3, 50) 兜底绝对下限，防止基线本身很小时判定式失去区分力。"""
    mutant_total = _false_injections(_admit_all)
    threshold = max(FALSE_INJECTION_BASELINE * 3, 50)
    print(f"\n[admit_all mutant] total={mutant_total} threshold={threshold}")
    assert mutant_total > threshold, (
        f"admit_all 变异下 false injection 总数 {mutant_total} 未显著高于 "
        f"threshold={threshold}——守卫无区分力，需重新设计。")


def test_negative_queries_are_synthetic() -> None:
    """n-gram 不重合守卫：负查询的任意 6 字滑窗片段不得出现在本机真实生产语料中。
    论域=全体负查询的全部 6+ 字滑窗片段。本机（含 CI）通常没有该采样文件，
    此时跳过——文件由 Task 8 的采样流程生成，非本 Task 职责。
    """
    sample = Path(os.environ.get("LOCALAPPDATA", "")) / "claude-vault-eval" / "ups_prompts.jsonl"
    if not sample.exists():
        pytest.skip("本机无生产语料采样文件（CI 环境 / Task 8 尚未生成）")
    corpus_text = sample.read_text(encoding="utf-8", errors="ignore")
    for q in NEGATIVE_QUERIES:
        for i in range(len(q) - 5):
            frag = q[i:i + 6]
            assert frag not in corpus_text, f"负查询片段与真实语料重合：{frag}"
