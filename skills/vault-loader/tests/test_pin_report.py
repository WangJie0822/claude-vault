# -*- coding: utf-8 -*-
"""报表对账栏、渲染篇数口径、list 字段坏值逐元素清洗、标注池含 pinned。"""
from __future__ import annotations

import pytest

from scripts import _metrics
from scripts import analyze_metrics as am


def _rec(**kw):
    base = {"_schema": 1, "ts": 1.7e9, "session": "s", "prompt_id": "p", "gate": "", "src": "",
            "admitted": [], "n_admitted": 0, "n_excluded": 0, "arm_counts": {}, "admitted_k": 20,
            "near_miss": [], "near_miss_k": 10, "ft": {"path": "", "arm": ""}, "max_notes": 3,
            "cwd_h": "x", "kw_h": [], "relaxed": False}
    base.update(kw)
    return base


def test_summary_counts_pinned_and_ranks():
    recs = [
        _rec(pinned_injected=1, llm_pinned=["a.md"], llm_pinned_rank=[2]),
        _rec(pinned_injected=1, llm_pinned=["b.md"], llm_pinned_rank=[7]),
        _rec(pinned_injected=0, llm_pinned=["c.md"], llm_pinned_rank=[None]),
        _rec(),
    ]
    s = am.summarize(recs)
    assert s["pin_rounds"] == 2 and s["pin_injected_sum"] == 2
    assert s["pin_n"] == 3 and s["pin_rank_top3"] == 1 and s["pin_rank_top10"] == 2 and s["pin_rank_missing"] == 1
    text = am.render_report(s)
    assert "pinned：" in text
    assert "不在本轮 admitted 1" in text


def test_acc_pin_ignores_junk_rank_elements_without_crashing():
    """`_acc_pin` 的 `rk` 类型防御（镜像 `_acc_topic`）：`summarize` 是公开函数，
    手搭 dict 绕过 `load_records` 清洗时，非 int/None 的 `llm_pinned_rank` 元素
    必须被跳过、不计入 `pin_n`，不得抛异常。

    修复前实测：`am._acc_pin({"pinned_injected": 1, "llm_pinned_rank": ["x"]}, am._Acc())`
    → `TypeError`（`'x' <= 3` 不可比较）。
    """
    recs = [
        _rec(pinned_injected=1, llm_pinned=["a.md"], llm_pinned_rank=[2]),
        _rec(pinned_injected=1, llm_pinned=["d.md"], llm_pinned_rank=["x", True]),
    ]
    s = am.summarize(recs)   # 不得抛异常
    assert s["pin_n"] == 1, "第二条记录的两个 junk 元素（str / bool）都不计入 pin_n"
    assert s["pin_rank_top3"] == 1 and s["pin_rank_top10"] == 1 and s["pin_rank_missing"] == 0


def test_bad_list_elements_dropped_not_whole_record():
    r = _rec(llm_pinned=["a.md", 42], llm_pinned_rank=[1, "x", None, True], topic_extracted_h=["h1", 3],
             llm_pinned_dedup=["candidate_injected", 3, None])
    n = am._drop_bad_list_elements(r)
    # 三个对账列表按位置联动（A-L6）：位置 1（42 / "x" / 3）、位置 2（llm_pinned 无此位）、位置 3 全丢；
    # topic_extracted_h 独立逐元素。
    assert r["llm_pinned"] == ["a.md"] and r["llm_pinned_rank"] == [1] and r["llm_pinned_dedup"] == ["candidate_injected"]
    assert r["topic_extracted_h"] == ["h1"]
    assert n == 1 + 3 + 2 + 1


def test_span_text_mentions_pinned_when_present():
    s = am.summarize([_rec(pinned_injected=1, n_admitted=3, admitted=[{"path": "a.md", "topical": 5, "total": 5, "arm": "topical", "dedup": "", "hits": []}])])
    assert "另有 1 轮追加 pinned（共 1 篇，独占名额）" in am._render_span(s)


def test_sample_admitted_includes_llm_pinned_kind():
    """有竞争场景（I-1）：40 个不同 admitted_list 路径各 count=3，与 1 条 llm_pinned
    （每会话仅记一次、topical 恒 0，count 几乎恒为 1）同池竞争。修复前该用例在
    没有竞争条目时通过、对该缺陷零判别力——真实数据（3315 条记录、1 条含
    llm_pinned）实测 `sample_admitted` 一条都不会含 llm_pinned，见 final-review.md I-1。"""
    admitted_recs = [
        _rec(admitted=[{"path": f"list{i}.md", "topical": 5, "total": 5,
                        "arm": "topical", "dedup": "", "hits": []}])
        for i in range(40) for _ in range(3)
    ]
    pinned_rec = _rec(llm_pinned=["pin.md"], llm_pinned_rank=[None], session="s1")

    items = am.sample_admitted(admitted_recs + [pinned_rec], k=10)
    assert len(items) == 10
    pinned_items = [it for it in items if it["kind"] == "llm_pinned"]
    assert len(pinned_items) == 1

    # 无竞争 pinned 记录时，两侧互相回填的既有行为不变：仍能凑满 10 条 admitted_list。
    items_no_pin = am.sample_admitted(admitted_recs, k=10)
    assert len(items_no_pin) == 10
    assert all(it["kind"] == "admitted_list" for it in items_no_pin)


def test_list_field_bad_elements_survive_load_records(tmp_home, capsys):
    """S2 同模式：坏元素经 load_records 被逐元素剔除，记录本身存活；
    变异：去掉 load_records 里的 `_drop_bad_list_elements(r)` 调用，本用例转红（见 fix report）。"""
    _metrics.write_record(tmp_home, "s-e2e", _rec(
        session="s-e2e",
        llm_pinned=["a.md", 42],
        llm_pinned_rank=[1, "x", None, True],
        topic_extracted_h=["h1", 3],
    ))
    recs = list(am.load_records(tmp_home))
    assert len(recs) == 1
    assert recs[0]["llm_pinned"] == ["a.md"]
    assert recs[0]["llm_pinned_rank"] == [1]              # 与 llm_pinned 按位置联动：位置 2/3 无对应篇，一起丢
    assert recs[0]["topic_extracted_h"] == ["h1"]
    err = capsys.readouterr().err
    assert "丢弃 5 个非法值" in err


def test_render_pin_three_states_are_distinguishable():
    """1A-M1 实跑形态：修复前 live-never-pins / disabled / old 三态输出**逐字相同**且含「功能未启用」。
    形态 A（开着、每轮 pinned_injected=0、无点名）是这条功能静默死掉的样子，报表必须点破。"""
    live = am.render_report(am.summarize([_rec(pin_on=True, pinned_injected=0) for _ in range(5)]))
    off = am.render_report(am.summarize([_rec(pin_on=False) for _ in range(5)]))
    old = am.render_report(am.summarize([_rec() for _ in range(5)]))
    assert "一次都没点名" in live and "0/5 轮注入" in live
    assert "未启用" not in live and "关闭状态" not in live
    assert "5 轮该功能处于关闭状态" in off and "一次都没点名" not in off
    assert "旧记录" in old and "一次都没点名" not in old and "关闭状态" not in old
    assert len({live, off, old}) == 3


def test_render_pin_splits_dedup_from_missing():
    """终审 I-3：rank=None 按逐位 `llm_pinned_dedup` 拆成「被跨轮去重压掉」与「不在本轮 admitted」。"""
    s = am.summarize([_rec(pin_on=True, pinned_injected=1, llm_pinned=["a.md", "b.md", "c.md"],
                           llm_pinned_rank=[None, None, 2],
                           llm_pinned_dedup=["candidate_injected", None, None])])
    assert s["pin_n"] == 3 and s["pin_dedup_n"] == 1 and s["pin_rank_missing"] == 1 and s["pin_rank_top3"] == 1
    text = am._render_pin(s)
    assert "被跨轮去重压掉 1" in text and "不在本轮 admitted 1" in text and "1/1 轮注入" in text
    # 旧记录没有 dedup 字段 ⇒ None 一律记「不在本轮 admitted」（向后兼容）
    s_old = am.summarize([_rec(pinned_injected=1, llm_pinned=["a.md"], llm_pinned_rank=[None])])
    assert s_old["pin_dedup_n"] == 0 and s_old["pin_rank_missing"] == 1


def test_pool_line_flags_zero_project_share():
    """1B-H-1：项目笔记片恒 0 要点破——组件 A 退化成纯 admitted 时功能照常工作，这一行是唯一信号。"""
    s0 = am.summarize([_rec(n_pool=30, n_pool_proj=0), _rec(n_pool=28, n_pool_proj=0), _rec()])
    assert s0["pool_n"] == 2 and s0["pool_sum"] == 58 and s0["pool_proj_sum"] == 0
    t0 = am._render_pool(s0)
    assert "恒为 0" in t0 and "2 轮提炼" in t0 and "均 29 篇" in t0
    s1 = am.summarize([_rec(n_pool=50, n_pool_proj=20), _rec(n_pool=30, n_pool_proj=0)])
    t1 = am._render_pool(s1)
    assert "恒为 0" not in t1 and "均 10.0 篇" in t1 and "1/2 轮含项目笔记" in t1
    assert "候选池：" in am.render_report(s1)
    assert "无记录携带" in am._render_pool(am.summarize([_rec()]))


def test_pool_fields_are_cleaned_as_numeric_top_fields():
    """新增顶层数值字段必须进 `_NUMERIC_TOP_FIELDS`（CLAUDE.md：坏值清洗只有一处）。"""
    r = _rec(n_pool="abc", n_pool_proj=True)
    assert am._drop_bad_numeric_fields(r) == 2
    assert "n_pool" not in r and "n_pool_proj" not in r


def test_kind_tables_cover_every_kind():
    """1A-M3 / 1B-M-5：kind → hit 判据与 kind → 配额桶都必须**全覆盖** KINDS，加第 5 个 kind 时这里先红。"""
    assert set(am.KINDS) == set(am._KIND_HIT) == set(am._KIND_BUCKET)
    assert set(am._KIND_BUCKET.values()) == {"fulltext", "pinned", "list"}


def test_kind_hits_path_dispatch_and_unknown_kind_never_hits():
    r = _rec(ft={"path": "f.md", "arm": "x"}, admitted=[{"path": "l.md"}], llm_pinned=["p.md"],
             near_miss_scorelow=[{"path": "n.md", "topical": 3.5}])
    for kind, yes, no in [("admitted_fulltext", "f.md", "l.md"), ("admitted_list", "l.md", "f.md"),
                          ("llm_pinned", "p.md", "n.md"), ("near_miss", "n.md", "p.md")]:
        assert am._kind_hits_path(r, yes, kind) is True, kind
        assert am._kind_hits_path(r, no, kind) is False, kind
    assert am._kind_hits_path(r, "n.md", "not_a_kind") is False     # 不再静默当 near_miss
    # 经公开入口走一遍 dispatch（post-review A-M3：此前这行是恒真的条件表达式）
    r2 = _rec(prompt_h="ph", llm_pinned=["p.md"])
    assert am.sample_admitted_events([r2], "p.md", "llm_pinned") == [("s", "ph")]
    assert am.sample_admitted_events([r2], "p.md", "admitted_list") == []


def test_apply_kind_quota_raises_on_unregistered_kind():
    with pytest.raises(KeyError):
        am._apply_kind_quota([{"kind": "mystery", "count": 1, "topical_max": 0.0}], 10)


def test_aligned_pin_lists_are_cleaned_jointly_so_dedup_pairs_stay_correct():
    """post-review A-L6 构造形态：rank 位置 1 坏、dedup 三个都合法——各自独立清洗会让 rank[1]=None 配到
    dedup[1]=None（记「真未召回」），而真值 dedup[2]="fulltext_injected" 是「被跨轮去重压掉」。"""
    r = _rec(pin_on=True, pinned_injected=1, llm_pinned=["a.md", "b.md", "c.md"],
             llm_pinned_rank=[1, "x", None], llm_pinned_dedup=["candidate_injected", None, "fulltext_injected"])
    am._drop_bad_list_elements(r)
    assert r["llm_pinned"] == ["a.md", "c.md"]
    assert r["llm_pinned_rank"] == [1, None] and r["llm_pinned_dedup"] == ["candidate_injected", "fulltext_injected"]
    s = am.summarize([r])
    assert s["pin_dedup_n"] == 1 and s["pin_rank_missing"] == 0


def test_pool_line_does_not_flag_pin_off_rounds_as_component_failure():
    """post-review A-M1 路径一：pin 关闭时池回落纯 admitted、不落 n_pool_proj，报表不得说「组件 A 未生效」。"""
    s = am.summarize([_rec(pin_on=False, n_pool=1), _rec(pin_on=False, n_pool=2)])
    t = am._render_pool(s)
    assert "恒为 0" not in t and "未生效" not in t and "纯 admitted" in t and "2 轮提炼" in t
    # 混合：两轮关、一轮开且项目片为 0 ⇒ 仍要点破，分母只算开着的那一轮
    s2 = am.summarize([_rec(pin_on=False, n_pool=1), _rec(pin_on=True, n_pool=30, n_pool_proj=0)])
    t2 = am._render_pool(s2)
    assert "恒为 0" in t2 and "1 轮算了项目笔记片" in t2
