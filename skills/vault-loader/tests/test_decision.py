"""decide_injection 纯函数 parity 测试（Task 2：主循环抽取，行为零变更）。"""
from __future__ import annotations

from pathlib import Path

from scripts._decision import decide_injection, StateView
from scripts._scorer import Signals
from scripts._frontmatter_reader import Entry
from scripts._config_loader import DEFAULT_CONFIG
from scripts.prompt_submit_load import build_injection_text_ups


def _entry(path, tags=(), summary="", keywords=(), mtime=1000.0):
    return Entry(path=path, tags=tuple(tags), summary=summary,
                 keywords=tuple(keywords), mtime=mtime)


def test_single_generic_bigram_admits_via_keyword_field():
    # 锁定现状病理行为（R2 旁路生态）：孤立泛词经 keywords 子串 +5 过闸
    e = _entry("a.md", keywords=("病历字段",), summary="s")
    d = decide_injection({"a.md": e}, Signals(prompt_keywords={"字段"}),
                         DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG, StateView())
    assert d.admitted and d.admitted[0].path == "a.md"
    assert d.admitted[0].admit_arm == "topical"   # keyword=5 >= min_topical=4


def test_fulltext_requires_strong_evidence():
    e = _entry("a.md", tags=("t1",), keywords=("连续一致尾段",), summary="一致性")
    d = decide_injection({"a.md": e}, Signals(prompt_keywords={"一致"}),
                         DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG, StateView())
    assert d.fulltext_path is None    # 单 bigram 链数<2，无强证据


def test_dedup_fulltext_sets_any_relevant():
    e = _entry("a.md", keywords=("病历字段",))
    d = decide_injection({"a.md": e}, Signals(prompt_keywords={"字段"}),
                         DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG,
                         StateView(fulltext_injected={"a.md"}))
    assert not d.admitted and d.any_relevant


def test_too_few_keywords_gates_unless_pure_cjk():
    d = decide_injection({}, Signals(prompt_keywords={"gradle"}),
                         DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG, StateView())
    assert d.gate_reason == "too_few_keywords" and not d.relaxed
    d2 = decide_injection({}, Signals(prompt_keywords={"崩溃"}),
                          DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG, StateView())
    assert d2.relaxed and d2.gate_reason == ""


# ---------------------------------------------------------------------------
# F2（fix round 1）：decide_injection.fulltext_path 与渲染层 build_injection_text_ups
# 的 fulltext_title 是两处独立实现（漂移无人发现的风险点），加 parity 断言锁死一致。
# ---------------------------------------------------------------------------

def _fulltext_title_via_render(decision, active_entries, prompt_keywords, config):
    """复现渲染层的**回退**调用路径（不传 fulltext_path/hits_by_path，如既有测试与
    未接决策层的调用点）：EntryDecision + active_entries 还原 scored 三元组，
    调用 build_injection_text_ups，取其 fulltext_title 返回值。

    该路径与决策层共用 scripts._decision.select_fulltext 单点，故 parity 断言不是
    "两套实现碰巧一致"，而是"任一处再分叉出第二套实现即被捕获"。"""
    scored = [(ed.total, ed.topical, active_entries[ed.path]) for ed in decision.admitted]
    keywords_str = ", ".join(sorted(prompt_keywords))
    _text, _paths, ft_title = build_injection_text_ups(
        scored, keywords_str, prompt_keywords,
        config["user_prompt_submit"], config["relevance"],
        vault_path=Path("/nonexistent"))
    return ft_title


def _fulltext_title_via_production_render(decision, active_entries, prompt_keywords, config):
    """复现 main() 的**生产**调用路径：传 hits_by_path 缓存 + decision.fulltext_path，
    渲染层不再自行重算主候选。守卫"生产路径确实消费了决策层结论"。"""
    scored = [(ed.total, ed.topical, active_entries[ed.path]) for ed in decision.admitted]
    hits_by_path = {ed.path: ed.hits for ed in decision.admitted}
    keywords_str = ", ".join(sorted(prompt_keywords))
    _text, _paths, ft_title = build_injection_text_ups(
        scored, keywords_str, prompt_keywords,
        config["user_prompt_submit"], config["relevance"],
        vault_path=Path("/nonexistent"),
        hits_by_path=hits_by_path, fulltext_path=decision.fulltext_path)
    return ft_title


def test_fulltext_path_parity_with_render_layer_when_triggered():
    # ≥2 个不同关键词命中（hook 命中 tag+summary、skill 命中 tag）→ 触发全文
    # （镜像 tests/integration/test_prompt_submit.py::test_b_two_distinct_keywords_trigger_fulltext）
    e = _entry("技术笔记/hook.md", tags=("hook", "skill"),
               summary="hook 的设计实现说明详述文档", mtime=1900000000)
    active_entries = {"技术笔记/hook.md": e}
    prompt_keywords = {"hook", "skill"}
    d = decide_injection(active_entries, Signals(prompt_keywords=prompt_keywords),
                         DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG, StateView())
    assert d.fulltext_path == "技术笔记/hook.md"   # 场景①：本轮确实触发了全文

    ft_title = _fulltext_title_via_render(d, active_entries, prompt_keywords, DEFAULT_CONFIG)
    assert d.fulltext_path == ft_title
    assert d.fulltext_path == _fulltext_title_via_production_render(
        d, active_entries, prompt_keywords, DEFAULT_CONFIG)


def test_fulltext_path_parity_with_render_layer_when_not_triggered():
    # 单关键词刷满 topical=6（同时命中 tag+summary）不构成强证据 → 不触发全文
    # （镜像 tests/integration/test_prompt_submit.py::test_b_single_keyword_no_fulltext）
    e = _entry("技术笔记/single.md", tags=("hook",),
               summary="hook 的设计实现说明详述文档资料", mtime=1900000000)
    active_entries = {"技术笔记/single.md": e}
    prompt_keywords = {"hook"}
    d = decide_injection(active_entries, Signals(prompt_keywords=prompt_keywords),
                         DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG, StateView())
    assert d.fulltext_path is None   # 场景②：本轮未触发全文

    ft_title = _fulltext_title_via_render(d, active_entries, prompt_keywords, DEFAULT_CONFIG)
    assert d.fulltext_path == ft_title
    assert ft_title is None
    assert _fulltext_title_via_production_render(
        d, active_entries, prompt_keywords, DEFAULT_CONFIG) is None


# ---------------------------------------------------------------------------
# H-A（full-review 修复批 A）：全文主候选选择的**多候选**守卫。
#
# 既有 parity 用例语料只有单条 ft 合格条目——单元素下 max/min 同解、winner key 任意排列
# 同解，四类变异（max→min / key 顺序 / fulltext_arm 篡改 / 去 -mtime tie-break）全部
# 存活。以下用例刻意构造 ≥2 条 ft 合格条目且**有意造 tie / 有意让 topical 序与 total 序
# 相反**，使排序语义成为可观测行为。
# ---------------------------------------------------------------------------

def _two_candidate_corpus():
    """构造 topical 序与 total 序**相反**的两条 ft 合格条目（实测值见断言注释）。

    A.md：tag(alpha, df=1→factor 1.0)=4 + keywords(gamma 未命中 tag)=5 → topical 9；
          mtime 2001 年（>90d 无衰减加成）、不命中 target_tags → total 9。
    B.md：tag(beta)=4 + summary(alpha/beta)=2 → topical 6；ctx ∈ target_tags(+3)、
          mtime 在未来（≤30d 档 +1）→ total 10。
    故 topical 最强者是 A、total 最强者是 B——全文主候选必须取 A。
    """
    a = _entry("A.md", tags=("alpha",), summary="无关摘要占位说明文字内容",
               keywords=("gamma",), mtime=1000000000)
    b = _entry("B.md", tags=("beta", "ctx"), summary="beta alpha 说明文档内容",
               mtime=1900000000)
    active_entries = {"A.md": a, "B.md": b}
    prompt_keywords = {"alpha", "beta", "gamma"}
    signals = Signals(target_tags={"ctx"}, prompt_keywords=prompt_keywords)
    return active_entries, prompt_keywords, signals


def test_fulltext_winner_takes_topical_max_not_total_max():
    """winner key = (topical, total) 的**顺序**与 max 语义守卫。

    杀 `max→min`（会选 B）与 `key 换成 (total, topical)`（会选 B）两类变异。"""
    active_entries, prompt_keywords, signals = _two_candidate_corpus()
    d = decide_injection(active_entries, signals, DEFAULT_CONFIG["scoring"],
                         DEFAULT_CONFIG, StateView())

    # 承重断言：没有它，语料退化成单候选时上面两类变异会静默存活
    assert len(d.admitted) >= 2, f"多候选语料退化为 {len(d.admitted)} 条，守卫失效"
    by_path = {ed.path: ed for ed in d.admitted}
    assert (by_path["A.md"].topical, by_path["A.md"].total) == (9.0, 9.0)
    assert (by_path["B.md"].topical, by_path["B.md"].total) == (6.0, 10.0)

    assert d.fulltext_path == "A.md"          # topical 最强，而非 total 最强的 B
    assert d.fulltext_arm == "topical>=6+strong_evidence"   # 杀 fulltext_arm 篡改

    # 决策层结论 == 渲染层（回退路径 & 生产路径）实际选中的全文主候选
    assert _fulltext_title_via_render(
        d, active_entries, prompt_keywords, DEFAULT_CONFIG) == "A.md"
    assert _fulltext_title_via_production_render(
        d, active_entries, prompt_keywords, DEFAULT_CONFIG) == "A.md"


def test_fulltext_winner_tie_on_topical_breaks_by_total():
    """topical 相同 → tie-break 取 total 高者（杀 max→min）。

    C/D 两条 topical 均为 6（tag df=1 → factor 1.0 满权 4 + summary 2），
    C 因 ctx ∈ target_tags 多 3 分 → total 10 vs 7。"""
    c = _entry("C.md", tags=("beta", "ctx"), summary="beta alpha 说明文档内容",
               mtime=1900000000)
    dd = _entry("D.md", tags=("delta",), summary="delta alpha 说明文档内容",
                mtime=1900000000)
    active_entries = {"C.md": c, "D.md": dd}
    prompt_keywords = {"alpha", "beta", "delta"}
    d = decide_injection(active_entries,
                         Signals(target_tags={"ctx"}, prompt_keywords=prompt_keywords),
                         DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG, StateView())

    assert len(d.admitted) >= 2, f"多候选语料退化为 {len(d.admitted)} 条，守卫失效"
    by_path = {ed.path: ed for ed in d.admitted}
    assert by_path["C.md"].topical == by_path["D.md"].topical == 6.0   # 有意造 tie
    assert by_path["C.md"].total > by_path["D.md"].total
    assert d.fulltext_path == "C.md"
    assert _fulltext_title_via_render(
        d, active_entries, prompt_keywords, DEFAULT_CONFIG) == "C.md"


def test_admitted_sort_tie_breaks_by_mtime_desc():
    """admitted.sort 的 `-mtime` tie-break 守卫：total 完全相同、仅 mtime 不同时，
    新的排前。杀 `key=lambda ed: -ed.total`（去掉 tie-break——stable sort 会保留
    active_entries 迭代序，即先插入的 old.md 排前）。"""
    old = _entry("old.md", tags=("alpha",), summary="alpha beta 说明文档内容",
                 mtime=1900000000)
    new = _entry("new.md", tags=("alpha",), summary="alpha beta 说明文档内容",
                 mtime=1900000001)
    active_entries = {"old.md": old, "new.md": new}   # 迭代序：old 在前
    d = decide_injection(active_entries, Signals(prompt_keywords={"alpha", "beta"}),
                         DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG, StateView())

    assert len(d.admitted) == 2
    assert d.admitted[0].total == d.admitted[1].total   # 有意造 total 全等
    assert d.admitted[0].path == "new.md"               # mtime 新者排前


# ---------------------------------------------------------------------------
# F3（fix round 1）：Decision.excluded——未进 admitted 的条目记录进它，供 Task 10 回放归因。
# ---------------------------------------------------------------------------

def test_fulltext_dedup_entry_lands_in_excluded():
    e = _entry("a.md", keywords=("病历字段",))
    d = decide_injection({"a.md": e}, Signals(prompt_keywords={"字段"}),
                         DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG,
                         StateView(fulltext_injected={"a.md"}))
    assert not d.admitted
    assert len(d.excluded) == 1
    ed = d.excluded[0]
    assert ed.path == "a.md" and ed.admitted is False
    assert ed.dedup == "fulltext_injected" and ed.admit_arm == ""


def test_gate_rejected_entry_lands_in_excluded():
    # topical 全失配（无 tag/summary/keyword 命中）→ 精度闸门拒绝，落 excluded
    e = _entry("noise.md", tags=("xyz",), summary="毫不相关的内容")
    d = decide_injection({"noise.md": e}, Signals(prompt_keywords={"hook", "skill"}),
                         DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG, StateView())
    assert not d.admitted
    assert len(d.excluded) == 1
    ed = d.excluded[0]
    assert ed.path == "noise.md" and ed.admitted is False
    assert ed.dedup == "" and ed.admit_arm == ""
