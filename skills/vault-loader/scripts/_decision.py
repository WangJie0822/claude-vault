"""UserPromptSubmit 候选决策：纯函数，无 IO。

把 prompt_submit_load.py 原主循环（min_keyword_count/relaxed 闸门、精度闸门、
去重语义、全文主候选选择）抽出，供生产主流程与测试/回放共用同一决策面。
本模块不做任何 stdin/cache/state 读写、不做渲染——只消费 active_entries/signals/
weights/config/state，产出结构化 Decision。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from scripts._frontmatter_reader import Entry
from scripts._scorer import (
    Signals, score, topical_score, has_keyword_hit, has_strong_evidence,
    build_tag_df, _keyword_hits_tags, _keyword_hits_summary, _keyword_hits_keywords,
)
from scripts._signal_collect import is_pure_cjk_keywords


@dataclass
class StateView:
    fulltext_injected: set[str] = field(default_factory=set)
    candidate_injected: set[str] = field(default_factory=set)


@dataclass
class EntryDecision:
    path: str
    topical: float
    total: float
    hits: list[str]        # 该篇命中的查询词（S1 渲染与臂归因共用）
    admitted: bool
    admit_arm: str         # "topical" | "keyword_bypass" | "upgrade_candidate" | ""
    dedup: str             # "" | "fulltext_injected" | "candidate_injected"
    # dedup 可达性（F3 校准，供 Task 10 回放归因）：
    # - "fulltext_injected"：恒 admitted=False——该 dedup 桶从不放行，只出现在 Decision.excluded。
    # - "candidate_injected"：admitted=True（topical 升到 fulltext_topical_threshold，落
    #   Decision.admitted，admit_arm="upgrade_candidate"）或 admitted=False（未达升级阈值，
    #   落 Decision.excluded，admit_arm=""）——两种去重结果都用同一 dedup 值标记「曾以弱
    #   候选注入」这一事实，admitted 字段区分本轮结果。
    # - ""：admitted=True（新篇过精度闸门，admit_arm="topical"/"keyword_bypass"，落
    #   Decision.admitted）或 admitted=False（新篇未过精度闸门，admit_arm=""，落
    #   Decision.excluded）。
    #
    # 性能护栏（F3 修复期间实测）：Decision.excluded 条目的 total/hits 是**未计算的占位值**
    # （total=0.0、hits=[]），不代表真实分数/命中词——admitted=True 的条目 total/hits 才是
    # 真实计算值。原因：excluded 在**真实 Vault** 上是数量主体，若对每篇都额外调
    # score()/_hit_keywords()（等价于把 O(N) 打分主循环的单趟开销翻倍），
    # tests/integration/test_perf.py::test_prompt_submit_under_300ms 的 300ms 预算会被顶穿。
    #
    # 举证语料必须用真实 Vault，不能用 500 篇合成 fixture——两者 excluded 占比差一个量级：
    #   真实 Vault（2026-08-04，1064 cache 条目 / 728 active，test_perf 同款 prompt）：
    #       admitted=150、excluded=578 → excluded 占 79.4%；
    #       补算 excluded 的 total/hits 实测 +150~200ms（3 轮 median，决策层 ~200~240ms → ~1.8×）
    #   500 篇合成 fixture（同 prompt、同轮次）：
    #       admitted=426、excluded=74 → excluded 仅占 14.8%；补算仅 +6~8ms
    # 合成 fixture 的 frontmatter 密度远低于真实笔记，绝大多数篇反而**过得了**闸门，
    # 故"excluded 是主体"这一护栏前提只在真实 Vault 成立——按 fixture 数据会误判护栏无必要。
    #
    # topical 字段本身已在原循环免费算出，故 excluded 条目的 topical 是真实值，可直接用于
    # 回放归因判断"差多少没过闸"；若未来需要 excluded 条目的精确 total/hits，需按需再算，
    # 不建议默认全量计算。


@dataclass
class Decision:
    admitted: list[EntryDecision]      # 已按 (-total, -mtime) 排序
    excluded: list[EntryDecision]      # 未进 admitted 的条目（去重命中 / 精度闸门拒绝）；
                                        # 保持 active_entries 迭代序，供 Task 10 回放归因，
                                        # 不参与渲染（渲染层只消费 admitted/fulltext_path）
    fulltext_path: str | None
    fulltext_arm: str                  # 如 "topical>=6+strong_evidence"；无全文则 ""
    any_relevant: bool
    relaxed: bool
    gate_reason: str                   # "" | "too_few_keywords"（min_keyword_count 未过且非纯CJK放宽）


def gate_keywords(prompt_keywords, config: dict) -> tuple[str, bool]:
    """触发点1单一真源：关键词数量闸门 + 纯 CJK 放宽判定。

    main()（保持旧早退时机——采集完 prompt_keywords 后立即调用）与 decide_injection
    （决策纯函数内部）共用本函数，避免闸门逻辑出现第二处副本（漂移风险）。

    返回 (gate_reason, relaxed)：
    - gate_reason == "too_few_keywords"（此时 relaxed 恒 False）→ 调用方应静默早退；
    - gate_reason == ""（relaxed 可能 True/False）→ 放行，continue 打分。
    """
    ups_cfg = config["user_prompt_submit"]
    rel_cfg = config["relevance"]
    if len(prompt_keywords) < ups_cfg["min_keyword_count"]:
        if (rel_cfg.get("relax_pure_cjk_single", True)
                and is_pure_cjk_keywords(prompt_keywords)):
            return "", True
        return "too_few_keywords", False
    return "", False


def _hit_keywords(entry: Entry, prompt_keywords) -> list[str]:
    """命中该 entry 的 tag/summary/keywords 的 prompt 关键词，保序去重。
    与精度闸门 topical 口径一致（不含 path）——path 命中不计入话题相关性，
    避免向主模型展示仅靠文件名命中的词、高估相关性。"""
    return [kw for kw in sorted(prompt_keywords)
            if _keyword_hits_tags(kw, entry)
            or _keyword_hits_summary(kw, entry)
            or _keyword_hits_keywords(kw, entry)]


def select_fulltext(candidates, ft_topical: float):
    """全文主候选选择——**唯一实现**（H-A）。

    决策层 `decide_injection` 与渲染层 `build_injection_text_ups` 的回退路径共用本函数；
    此前两处各有一套独立实现（渲染层还多跑一趟 `_hit_keywords`），漂移无人发现且四类
    排序变异全部测不出。任何一方再分叉出第二套实现，tests/test_decision.py 的
    parity 用例即被打红。

    candidates: 可迭代的 `(topical, total, hits, payload)` 四元组——两侧各自把自己的
        条目形态（EntryDecision / (total, topical, entry) 三元组）投影成该形态。
    返回：胜出者的 payload；无合格候选返回 None。

    资格与排序语义（与文档口径同源，勿分头改）：
    - 资格：topical 达全文阈值「且」≥2 个不同关键词佐证（强证据档）；
    - 胜出：取 topical 最强者，tie-break 取 total 高者——**不取 total 排序首位**
      （context 底噪可能把弱话题条目顶到首位、埋掉强话题命中）。
    """
    ok = [c for c in candidates if c[0] >= ft_topical and has_strong_evidence(c[2])]
    if not ok:
        return None
    return max(ok, key=lambda c: (c[0], c[1]))[3]


def decide_injection(active_entries: dict, signals: Signals, weights: dict,
                     config: dict, state: StateView) -> Decision:
    """UPS 候选决策：闸门 + 打分 + 去重 + 全文主候选选择，纯计算、无 IO。

    active_entries 须已完成第1层排除（archived 等，由调用方过滤）。
    config 为 load_config 返回的完整 merge 后 dict（内部取 relevance/user_prompt_submit 段）。
    """
    rel_cfg = config["relevance"]
    prompt_keywords = signals.prompt_keywords

    # 触发点1：单一真源见 gate_keywords（main() 也调用同一函数，在采集完
    # prompt_keywords 后立即早退，不等 cache/state IO 完成——本函数内部复用同一
    # 判定结果，不重复实现闸门逻辑）。
    gate_reason, relaxed = gate_keywords(prompt_keywords, config)
    if gate_reason:
        return Decision(admitted=[], excluded=[], fulltext_path=None, fulltext_arm="",
                        any_relevant=False, relaxed=False, gate_reason=gate_reason)

    use_kw = rel_cfg.get("use_keywords", True)
    min_topical = rel_cfg["min_topical_score"]
    ft_topical = rel_cfg["fulltext_topical_threshold"]
    use_tag_idf = rel_cfg.get("use_tag_idf", True)
    tag_df = build_tag_df(active_entries) if use_tag_idf else None
    n_docs = len(active_entries)
    floor = rel_cfg.get("tag_idf_floor", 0.5)

    admitted: list[EntryDecision] = []
    excluded: list[EntryDecision] = []
    any_relevant = False   # 有任一篇 topical 达标（含被去重的）→ 区分"全失配"vs"已注入过"

    def _total() -> float:
        return score(entry, signals, weights, use_kw,
                    tag_df=tag_df, n_docs=n_docs, tag_idf_floor=floor)

    for path, entry in active_entries.items():
        t = topical_score(entry, signals, weights, use_kw,
                          tag_df=tag_df, n_docs=n_docs, tag_idf_floor=floor)
        if path in state.fulltext_injected:
            if t >= min_topical:
                any_relevant = True   # 仍相关但已拿全文 → 不重注、抑制兜底
            excluded.append(EntryDecision(
                path=path, topical=t, total=0.0, hits=[],
                admitted=False, admit_arm="", dedup="fulltext_injected",
            ))
            continue
        if path in state.candidate_injected:
            # 曾弱候选注入：仅升到全文阈值才作升级候选再注入（治 reverse High#1：
            # 升级候选不在渲染层排除，进候选参与主候选；非主候选时仍可见于清单、保留升级机会）
            if t >= ft_topical:
                admitted.append(EntryDecision(
                    path=path, topical=t, total=_total(),
                    hits=_hit_keywords(entry, prompt_keywords),
                    admitted=True, admit_arm="upgrade_candidate",
                    dedup="candidate_injected",
                ))
            else:
                if t >= min_topical:
                    any_relevant = True   # 仍相关但已展示过弱候选 → 不重复展示、抑制兜底
                excluded.append(EntryDecision(
                    path=path, topical=t, total=0.0, hits=[],
                    admitted=False, admit_arm="", dedup="candidate_injected",
                ))
            continue
        # 新篇：精度闸门——topical 达标即进候选。keyword-only 命中：默认权重
        # (prompt_keyword_hit=5 ≥ min_topical=4) 下已由 t >= min_topical 直接放行、且**高于**
        # 被 IDF 降权的泛 tag（非低排名）；下面的 has_keyword_hit 旁路仅当用户把
        # prompt_keyword_hit 调到 <min_topical 时才复活（默认下是死分支，保留以守护自定义低权重 config）。
        # 与打分共用 has_keyword_hit 单点，口径一致、防漂移。
        # （BUG-1 前该函数还会做「命中 tag 的词不计 keyword」的去重，已删除——
        #  双命中是最强相关性信号，去重反而让它得分最低。）
        if t < min_topical and not has_keyword_hit(entry, prompt_keywords, use_kw):
            # 性能护栏：不为该 excluded 分支调 score()/_hit_keywords()——这是大语料下
            # 数量占绝对主体的分支，逐篇多算一遍会把 O(N) 打分主循环单趟开销翻倍，顶穿
            # tests/integration/test_perf.py 的 300ms 预算（见 EntryDecision.dedup 上方
            # 性能护栏注释）。total/hits 用占位值，topical(t) 仍是真实值。
            excluded.append(EntryDecision(
                path=path, topical=t, total=0.0, hits=[],
                admitted=False, admit_arm="", dedup="",
            ))
            continue
        admit_arm = "topical" if t >= min_topical else "keyword_bypass"
        admitted.append(EntryDecision(
            path=path, topical=t, total=_total(),
            hits=_hit_keywords(entry, prompt_keywords),
            admitted=True, admit_arm=admit_arm, dedup="",
        ))

    admitted.sort(key=lambda ed: (-ed.total, -active_entries[ed.path].mtime))

    # 全文主候选选择：资格与排序语义见 select_fulltext（决策层与渲染层共用单点）。
    fulltext_path: str | None = None
    fulltext_arm = ""
    winner = select_fulltext(
        ((ed.topical, ed.total, ed.hits, ed) for ed in admitted), ft_topical)
    if winner is not None:
        fulltext_path = winner.path
        fulltext_arm = f"topical>={ft_topical}+strong_evidence"

    return Decision(
        admitted=admitted, excluded=excluded, fulltext_path=fulltext_path,
        fulltext_arm=fulltext_arm, any_relevant=any_relevant, relaxed=relaxed,
        gate_reason="",
    )
