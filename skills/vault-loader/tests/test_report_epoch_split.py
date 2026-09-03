"""报表口径修复：全量成因分布、阈值分期、埋点时代切分。

三条都是「报表在说假话」而不是「报表不够详细」——数字本身是错的，读者据此
会得出与事实相反的结论。每条的实测依据见 `_metrics._dedup_counts` 的 docstring
与本仓库 2026-09-01 的两路评审。
"""

import json

from scripts.analyze_metrics import render_report, summarize


def _rec(**kw):
    """一条走到打分的最小记录。"""
    base = {"_schema": 1, "ts": 1_800_000_000.0, "session": "s", "prompt_id": "p",
            "gate": "", "n_admitted": 1, "arm_counts": {"topical": 1},
            "near_miss": [], "admitted": [], "n_excluded": 0}
    base.update(kw)
    return base


# ── H-2：成因分布必须走全量计数，不能用被截断的 near_miss 窗口 ──────────

def test_dedup_distribution_uses_full_counts_not_window():
    """有 dedup_counts 的记录，成因分布必须用它。

    构造复刻真实偏斜：窗口里全是 dedup 条目（因为它们 topical 高），
    而全量里 dedup 只占 10/100。

    变异验证：让报表改回累加 near_miss 的 dedup，本用例转红。
    """
    r = _rec(
        n_excluded=100,
        dedup_counts={"fulltext_injected": 10, "": 90},
        near_miss=[{"path": f"hi/{i}.md", "topical": 11.0,
                    "dedup": "fulltext_injected"} for i in range(10)],
    )
    s = summarize([r])
    assert s["dedup_full"] == {"fulltext_injected": 10, "": 90}, \
        f"成因分布没走全量计数，实际 {s['dedup_full']}"
    assert s["n_dedup_full"] == 1


def test_dedup_full_and_legacy_window_are_not_mixed():
    """新旧记录不得混算 —— 混了就既不是全量也不是窗口口径。

    变异验证：把旧记录的窗口计数并进 dedup_full，本用例转红。
    """
    new = _rec(n_excluded=100, dedup_counts={"": 100}, near_miss=[])
    old = _rec(n_excluded=50,
               near_miss=[{"path": "a.md", "topical": 9.0,
                           "dedup": "fulltext_injected"}])
    s = summarize([new, old])
    assert s["dedup_full"] == {"": 100}, "旧记录的窗口计数混进了全量口径"
    assert s["n_dedup_full"] == 1
    assert s["n_dedup_legacy"] == 1


def test_report_states_full_coverage_when_legacy_present():
    """混合样本时报表必须说明全量口径只覆盖了多少记录。"""
    new = _rec(n_excluded=10, dedup_counts={"": 10})
    old = _rec(n_excluded=10,
               near_miss=[{"path": "a.md", "topical": 9.0, "dedup": ""}])
    out = render_report(summarize([new, old]))
    assert "1/2" in out or "50.0%" in out or "50%" in out, \
        "报表没有给出全量口径的覆盖率分母，读者无从判断这个分布代表多少数据"


# ── M-3：全文注入率必须按阈值制度分期 ──────────────────────────────────

def test_fulltext_rate_split_by_threshold():
    """阈值变过时，报表不得只给一个跨制度的合并值。

    真实数据里 ft 阈值由 6 改成 10，两期实测 65.4% vs 40.1%，而合并值 45.9%
    不描述任何一个时期。

    变异验证：让报表只输出合并值，本用例转红。
    """
    old_era = [_rec(ft_topical=6.0, ft={"path": "a.md", "arm": "topical>=6"})
               for _ in range(3)]
    old_era.append(_rec(ft_topical=6.0, ft={"path": "", "arm": ""}))   # 4 轮 3 全文
    new_era = [_rec(ft_topical=10.0, ft={"path": "", "arm": ""})
               for _ in range(3)]
    new_era.append(_rec(ft_topical=10.0, ft={"path": "b.md", "arm": "topical>=10"}))
    s = summarize(old_era + new_era)
    assert s["ft_by_threshold"] == {"6.0": [4, 3], "10.0": [4, 1]}, \
        f"ft 率没有按阈值分期，实际 {s['ft_by_threshold']}"
    out = render_report(s)
    assert "75.0%" in out and "25.0%" in out, \
        "报表没有分期呈现两个制度下的全文注入率"


def test_fulltext_rate_no_split_when_single_threshold():
    """只有一个阈值时不必分期 —— 不给读者制造无意义的分栏。"""
    recs = [_rec(ft_topical=10.0, ft={"path": "a.md", "arm": "x"}) for _ in range(2)]
    s = summarize(recs)
    assert s["ft_by_threshold"] == {"10.0": [2, 2]}
    out = render_report(s)
    assert "按阈值分期" not in out


def test_legacy_records_without_threshold_are_labelled():
    """没落阈值的旧记录单独归组，不能塞进任何一个已知制度。"""
    s = summarize([_rec(ft_topical=10.0, ft={"path": "a.md", "arm": "x"}),
                   _rec(ft={"path": "b.md", "arm": "x"})])
    assert "(未记录)" in s["ft_by_threshold"], \
        f"旧记录没有单独归组，实际 {s['ft_by_threshold']}"
    # 只验「存在」不够：把所有 key 都写成 "(未记录)" 时上一条照样绿（变异实测），
    # 必须同时钉住「有阈值的记录不得被并进这一档」。
    assert s["ft_by_threshold"]["(未记录)"] == [1, 1], \
        f"未记录档混进了有阈值的记录，实际 {s['ft_by_threshold']}"
    assert "10.0" in s["ft_by_threshold"], \
        f"有阈值的记录被归进了未记录档，实际 {s['ft_by_threshold']}"


# ── H-1：埋点时代切分 ──────────────────────────────────────────────────

def test_pre_gate_epoch_records_are_counted():
    """gate 埋点之前的记录要单独计数 —— 那段时期被拦的轮次一条都没落盘，
    把它们计入分母会系统性抬高「走到打分」的占比（实测 +6.5 个百分点）。

    判据：有 `src` 键（走到打分的新记录）或 gate 非空（被拦的新记录）。

    变异验证：把判据改成恒真，本用例转红。
    """
    pre = _rec()                                   # 无 src、gate 为空 => 前埋点
    post_ok = _rec(src="")                         # 有 src 键 => 新时代
    post_gate = {"_schema": 1, "ts": 1.0, "session": "s", "prompt_id": "p",
                 "gate": "too_few_keywords"}       # 被拦的新记录
    s = summarize([pre, post_ok, post_gate])
    assert s["n_pre_epoch"] == 1, f"前埋点记录计数错，实际 {s['n_pre_epoch']}"
    out = render_report(s)
    assert "前埋点" in out or "埋点前" in out, "报表没有标注时代切分"


# ── H-3：两榜量纲不同，必须标明 ────────────────────────────────────────

def test_two_boards_state_incomparable_units():
    """对照榜的 N 次受窗口截断、是下界，与真·擦肩榜量纲不同。

    两栏并排渲染成同一种「N 次」，读者会直接比大小 —— 而真实语料里两个窗口的
    饱和度是 100% vs 11%。

    变异验证：删掉渲染层那句量纲说明，本用例转红。
    """
    r = _rec(n_excluded=5, near_miss_scorelow=[{"path": "low.md", "topical": 3.5}],
             near_miss=[{"path": "sup.md", "topical": 9.0,
                         "dedup": "fulltext_injected"}])
    out = render_report(summarize([r]))
    assert "对照 · 被去重抑制" in out, "前提不成立：对照榜没有渲染出来"
    assert "不可直接比大小" in out, "两榜并排却没有标明量纲不同，读者会直接比 N 次"
