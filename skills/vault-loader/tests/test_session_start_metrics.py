"""SessionStart 通道落盘（F4）。

此前 `session_start_load.py` 对 `_metrics` 的引用数是 **0**——这条通道的注入开销
在「值不值」的账上完全缺席，报表只有一句免责「不含 SessionStart 通道，它不落
metrics」，而免责不是数据。

实测这条通道有多大：从 transcript 侧直接量，557 个会话检出 SessionStart 注入，
additionalContext 合计 **642,439 字符**（中位 724、p90 2091）。对照 UPS 侧 metrics
记录的 86 万字符，量级相当可观。

**隔离是硬要求**：SS 记录一旦被现有 `_acc_*` 当成 UPS 记录处理，`gate` 为空会让它
计入 `n_ok`，「走到打分」「全文注入率」「候选池均值」全部被稀释——修一个缺口却
弄坏三个已有指标。判据用 `channel` 字段，旧记录没有该键、一律按 UPS 处理，
向后兼容。
"""

from scripts import _metrics
from scripts.analyze_metrics import render_report, summarize


def _ups(**kw):
    base = {"_schema": 1, "ts": 1_800_000_000.0, "session": "s", "prompt_id": "p",
            "gate": "", "n_admitted": 1, "arm_counts": {"topical": 1},
            "near_miss": [], "admitted": [], "n_excluded": 0}
    base.update(kw)
    return base


# ── 写端 ────────────────────────────────────────────────────────────────

def test_session_start_record_shape():
    """极简记录：只记规模与开销，**不记笔记路径**（隐私增量为 0）。

    变异验证：让它落 path，本用例转红。
    """
    r = _metrics.build_session_start_record(
        session_id="sess-1", inj_chars=1234,
        n_notes=4, n_worklogs=2, n_commits=5)
    assert r["channel"] == "session_start"
    assert r["session"] == "sess-1"
    assert r["inj_chars"] == 1234
    assert (r["n_notes"], r["n_worklogs"], r["n_commits"]) == (4, 2, 5)
    joined = " ".join(str(v) for v in r.values())
    assert ".md" not in joined, "SessionStart 记录不该带笔记路径"


def test_session_start_record_has_no_prompt_fields():
    """它不是一次提问，不得混入 prompt 相关字段。"""
    r = _metrics.build_session_start_record(
        session_id="s", inj_chars=1, n_notes=0, n_worklogs=0, n_commits=0)
    for k in ("kw_h", "prompt_h", "n_kw", "admitted", "near_miss"):
        assert k not in r, f"SessionStart 记录混入了 UPS 字段 {k}"


# ── 读端隔离：这是本次改动最容易弄坏别的东西的地方 ──────────────────────

def test_session_start_records_do_not_pollute_ups_stats():
    """SS 记录不得计入 UPS 的任何指标。

    变异验证：去掉 summarize 里的 channel 分流，本用例转红——SS 记录的 gate
    为空，会被当成「走到打分」计入 n_ok。
    """
    ss = _metrics.build_session_start_record(
        session_id="s", inj_chars=700, n_notes=3, n_worklogs=1, n_commits=2)
    s = summarize([_ups(), ss])
    assert s["n_ok"] == 1, f"SS 记录被计入了「走到打分」，实际 n_ok={s['n_ok']}"
    assert s["n_events"] == 1, f"SS 记录被计入了 UPS 事件数，实际 {s['n_events']}"


def test_session_start_stats_are_collected_separately():
    recs = [_metrics.build_session_start_record(
        session_id=f"s{i}", inj_chars=100 * (i + 1),
        n_notes=i, n_worklogs=0, n_commits=1) for i in range(3)]
    s = summarize(recs)
    assert s["ss_n"] == 3
    assert s["ss_inj_chars"] == 100 + 200 + 300


def test_legacy_records_without_channel_still_count_as_ups():
    """旧记录没有 channel 键，必须仍按 UPS 处理（向后兼容）。"""
    s = summarize([_ups(), _ups()])
    assert s["n_events"] == 2 and s["n_ok"] == 2
    assert s.get("ss_n", 0) == 0


# ── 报表 ────────────────────────────────────────────────────────────────

def test_report_shows_session_start_cost():
    """报表必须把这条通道的开销显示出来，并且不再声称「它不落 metrics」。

    变异验证：删掉报表里的 SessionStart 行，本用例转红。
    """
    # UPS 记录必须带 inj_chars：那句旧免责声明挂在 `if inj_n:` 分支里，
    # 不带它的话该分支根本不执行，「免责句已撤」这条断言就恒真、零判别力
    # （变异实测发现）。
    recs = [_ups(inj_chars=500)] + [_metrics.build_session_start_record(
        session_id="s", inj_chars=800, n_notes=3, n_worklogs=1, n_commits=2)]
    out = render_report(summarize(recs))
    assert "SessionStart" in out
    assert "800" in out, "没有显示 SessionStart 的注入字符数"
    assert "它不落 metrics" not in out, "旧的免责声明还在，与新数据自相矛盾"


def test_report_omits_session_start_line_when_absent():
    """没有 SS 记录时不显示该行 —— 不给读者制造「有这条通道但是 0」的错觉。"""
    # 用带 inj_chars 的记录：否则报表根本不进那个分支，断言无判别力
    out = render_report(summarize([_ups(inj_chars=100)]))
    assert "SessionStart 注入" not in out
