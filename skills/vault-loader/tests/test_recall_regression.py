# -*- coding: utf-8 -*-
"""召回回归 gold 集（2026-07-02 spec §9）：脱敏合成条目 + 代表性查询。
数字锚点来自真实 862 篇 PoC（docs/superpowers/poc/，作者本地），此处以
行为不变量形式固化：正样本可召回 / 负样本零注入 / 弱泛词不升全文。

_recall() 改走真实决策面 decide_injection（Task 5 漂移修复）：此前直接内联
`topical_score(e, sig, WEIGHTS) >= MIN_TOPICAL or has_keyword_hit(e, kws)` 复刻
精度闸门逻辑，WEIGHTS/MIN_TOPICAL 是本文件早期硬编码的权重快照
（prompt_keyword_hit=3），与生产 DEFAULT_CONFIG（Task 8 已改 5，见
scripts/_config_loader.py 注释）永久失步——Task 8 调整生产权重后，本文件的回归
断言仍验证旧值，真实改动被静默架空、测不出来。现直接引用 DEFAULT_CONFIG（而非
拷贝快照）并调用与生产 prompt_submit_load.py 同一决策函数 decide_injection，
消除第二处闸门逻辑副本，回归覆盖的是真实生产链路（含精度闸门 + 去重 + tag-IDF），
而非本文件自制的简化版判定。"""
from scripts._config_loader import DEFAULT_CONFIG
from scripts._decision import decide_injection, StateView
from scripts._frontmatter_reader import Entry
from scripts._scorer import Signals, has_strong_evidence, is_archived
from scripts._signal_collect import collect_signal_j_prompt_keywords

FIXTURE = [
    Entry(path="n/budget.md", tags=("记账", "预算管理"), summary="预算管理功能实施与月周期配置"),
    Entry(path="n/crash.md", tags=("崩溃定位",), summary="空指针崩溃排查与堆栈分析"),
    Entry(path="n/build.md", tags=("gradle", "构建"), summary="gradle 构建内存与代理配置"),
    Entry(path="n/log.md", tags=("日志",), summary="日志目录结构与轮转策略"),
    Entry(path="n/arch.md", tags=("spec", "archived", "预算管理"), summary="已归档的预算管理设计文档"),
]


def _recall(prompt: str, exclude_archived: bool = True, entries=None):
    """entries 默认 FIXTURE；drift 守卫用它注入独立的最小语料，不污染共享 FIXTURE
    也不改变既有 4 条测试的默认行为（它们都不传 entries，走原 FIXTURE 分支）。"""
    entries = FIXTURE if entries is None else entries
    kws = collect_signal_j_prompt_keywords(prompt, max_keywords=30)
    sig = Signals(prompt_keywords=kws)
    active = {e.path: e for e in entries
              if not (exclude_archived and is_archived(e, {"archived"}))}
    decision = decide_injection(active, sig, DEFAULT_CONFIG["scoring"],
                                DEFAULT_CONFIG, StateView())
    out = [(ed.topical, active[ed.path]) for ed in decision.admitted]
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


def test_weight_drift_guard_reads_live_default_config(monkeypatch) -> None:
    """漂移守卫：_recall() 必须读取 DEFAULT_CONFIG["scoring"] 的当前权重，而非本文件
    早期硬编码的 WEIGHTS 快照（prompt_keyword_hit=3）——否则生产权重改动（如 Task 8
    的 3→5）不会被本文件的回归断言覆盖，是本次修复要堵住的漂移本身。

    FIXTURE 5 篇均无 keywords 字段（历史遗留），prompt_keyword_hit 权重变化在其上
    不产生任何可观测差异（has_keyword_hit 恒 False，_recall 的现有 4 条测试也验不出
    这个漂移）——故本守卫经 `entries=` 注入一条独立的、仅靠 keywords 字段命中精度
    闸门的最小语料（不污染共享 FIXTURE），**经由 _recall() 本身**（而非绕过它直接调
    decide_injection）验证"临时把 DEFAULT_CONFIG['scoring']['prompt_keyword_hit']
    改成 3"会让 _recall 返回的 topical 分数随之改变（5→3）——直接调 decide_injection
    只能证明 DEFAULT_CONFIG 本身可被 monkeypatch，测不出 _recall() 有没有真的读它
    （已用"硬编码快照"变异实测验证：见 fix report，若 _recall 改回传入本文件内固定
    权重字面量而非 DEFAULT_CONFIG["scoring"]，本测试经由 _recall 调用会失败于
    `t_at_3 != t_at_5` 断言，红→绿闭环成立）。"""
    entry = Entry(path="n/kwonly.md", tags=("其他",), summary="无关摘要文本",
                 keywords=("专属关键词",))
    # 交给 collect_signal_j_prompt_keywords 走真实 CJK bigram 切分（而非手填整词），
    # 与 _recall 的真实调用路径完全一致；bigram（如"专属"）仍是 entry.keywords 整词
    # 的子串，_kw_in_text 子串匹配可命中，且 bigram 切分天然产出 ≥2 个 token，不会
    # 触发 min_keyword_count 闸门（无需再纠结"纯 CJK 单 token 放宽"这条边界）。
    prompt = "专属关键词"

    _, cands_at_5 = _recall(prompt, entries=[entry])
    t_at_5 = next(t for t, e in cands_at_5 if e.path == entry.path)
    assert t_at_5 == 5, f"生产默认 prompt_keyword_hit 应为 5，topical 却是 {t_at_5}"

    monkeypatch.setitem(DEFAULT_CONFIG["scoring"], "prompt_keyword_hit", 3)
    _, cands_at_3 = _recall(prompt, entries=[entry])
    t_at_3 = next(t for t, e in cands_at_3 if e.path == entry.path)

    assert t_at_3 != t_at_5, (
        "把 DEFAULT_CONFIG['scoring']['prompt_keyword_hit'] 从 5 改到 3 后 _recall() "
        f"返回的 topical 分数未变化（仍是 {t_at_3}）——说明 _recall 未读取 "
        "DEFAULT_CONFIG 的当前权重（可能又回退成硬编码快照），本集空网。")
    assert t_at_3 == 3
