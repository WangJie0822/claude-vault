# -*- coding: utf-8 -*-
"""报表的 LLM 精排段（spec §12）与 `sample_admitted` 按 `shown` 抽样（F31）。

单元用例手搭记录喂 `summarize`；末尾几条从真实 UPS 入口（`prompt_submit_load.main()`）产出的记录经
`analyze_metrics.main()` 的报表出口喂进来（`.claude/rules/testing.md`：手工构造被测对象的用例都要补一条
从真实产出喂入的端到端用例）。
"""
from __future__ import annotations

import io
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import analyze_metrics as am


def _fine(session="s1", **kw):
    r = {"_schema": 2, "ts": 1.0, "session": session, "prompt_id": "p", "fine_on": True}
    r.update(kw)
    return r


def test_fine_section_counts_the_delivery_funnel():
    recs = [
        _fine("s1", n_fine_done=1, llm_none=0, n_supp_gen=1, n_supp_drop_stale=0,
              n_supp_drop_recheck=1, n_supp_delivered=2, n_supp_late_delivered=1,
              n_catalog=300, n_catalog_dropped=0, n_summary_layer=40, n_echo_mismatch=1,
              bytes_sent=30000, n_vault_reads=2, n_vault_reads_injected=1,
              degraded=["transcript_none"]),
        _fine("s1", n_fine_done=1, llm_none=1, n_supp_gen=1, n_supp_drop_stale=2,
              n_catalog=310, n_catalog_dropped=5, bytes_sent=61000, degraded=["lock"]),
        _fine("s2", n_fine_done=0),
        {"_schema": 2, "ts": 1.0, "session": "s3", "fine_on": False},
    ]
    s = am.summarize(recs)
    assert (s["fine_field_n"], s["fine_off_n"]) == (3, 1)
    assert (s["fine_done_sum"], s["llm_none_sum"]) == (2, 1)
    assert (s["supp_gen_sum"], s["supp_drop_stale_sum"], s["supp_drop_recheck_sum"],
            s["supp_delivered_sum"], s["supp_late_sum"]) == (2, 2, 1, 2, 1)
    assert (s["catalog_n"], s["catalog_sum"]) == (2, 610)
    assert (s["catalog_dropped_sum"], s["catalog_dropped_rounds"]) == (5, 1)
    assert s["summary_layer_sum"] == 40 and s["echo_mismatch_sum"] == 1
    assert (s["vault_reads_sum"], s["vault_reads_injected_sum"]) == (2, 1)
    assert s["fine_calls_dist"] == {"2": 1, "0": 1}
    assert s["bytes_session_median"] == 61000
    assert s["degraded_dist"] == {"transcript_none": 1, "lock": 1}
    text = am.render_report(s)
    assert "已并账调用 2 次（其中失败 0 次）" in text
    assert "NONE 1 次（占成功 2 次的 50.0%）" in text
    assert "时效丢弃 2 篇、复核丢弃 1 篇、投递 2 篇（其中放宽档 1 篇）" in text
    assert "读的是本会话已注入篇 1 次（50.0%）" in text
    assert "超预算丢弃 5 篇（1 轮）" in text
    assert "降级码分布" in text and "lock" in text


def test_fine_render_points_out_never_delivered():
    s = am.summarize([_fine(n_fine_done=1, n_supp_gen=3, n_supp_drop_stale=2,
                            n_supp_drop_recheck=1, n_supp_delivered=0)])
    assert "**一次都没投递**" in am.render_report(s)


def test_fine_render_names_old_records_and_all_off():
    old = am.render_report(am.summarize([{"_schema": 2, "ts": 1.0, "session": "s"}]))
    assert "LLM 精排：无记录携带该字段" in old
    off = am.render_report(am.summarize([{"_schema": 2, "ts": 1.0, "session": "s", "fine_on": False}]))
    assert "1 轮处于关闭状态" in off


def test_fine_acc_ignores_bad_types():
    s = am.summarize([_fine(n_fine_done="x", llm_none=True, n_supp_delivered=float("inf"),
                            degraded="lock"),
                      _fine(degraded=[1, "", "lock"])])
    assert s["fine_done_sum"] == 0 and s["llm_none_sum"] == 0 and s["supp_delivered_sum"] == 0
    assert s["degraded_dist"] == {"lock": 1}


def test_stale_drop_line_says_it_excludes_gate_rounds():
    """索引为空的闸门轮仍会读到并消耗到期补充，而闸门记录恰好五键、记不下来（指代型与关键词不足的闸门轮
    不认领补充，留给下一个过闸轮计）：凡写到时效丢弃的文案都要点明，两个分支（投递过 / 一次都没投递）
    各自断言，否则任一分支漏写读者都会把它当全量。"""
    delivered = am.render_report(am.summarize([_fine(n_fine_done=1, n_supp_gen=2, n_supp_drop_stale=1,
                                                     n_supp_delivered=1)]))
    never = am.render_report(am.summarize([_fine(n_fine_done=1, n_supp_gen=2, n_supp_drop_stale=1)]))
    assert "**一次都没投递**" in never
    assert "不含索引为空的闸门轮" in delivered and "不含索引为空的闸门轮" in never


def test_catalog_mean_counts_only_rounds_that_carry_a_catalog():
    """真实记录在每个完整轮上都带 `n_catalog`（没有统计出队的轮次是 0）：只数正值，否则均值被无调用轮稀释。"""
    s = am.summarize([_fine(n_catalog=0), _fine(n_catalog=0), _fine(n_catalog=300), _fine(n_catalog=200)])
    assert (s["catalog_n"], s["catalog_sum"]) == (2, 500)
    assert "目录层均 250 篇" in am.render_report(s)
    none = am.summarize([_fine(n_catalog=0)])
    assert none["catalog_n"] == 0 and "目录层均" not in am.render_report(none)


def test_fine_fail_not_counted_as_completed_and_none_rate_uses_successes():
    """`n_fine_done` 是并账的调用数，含失败的（子进程失败同样追加统计）：文案不得把它写成「完成」，
    NONE 率的分母是成功调用数。"""
    s = am.summarize([_fine(n_fine_done=4, n_fine_fail=2, llm_none=1)])
    assert (s["fine_done_sum"], s["fine_fail_sum"]) == (4, 2)
    text = am.render_report(s)
    assert "已并账调用 4 次（其中失败 2 次）" in text
    assert "NONE 1 次（占成功 2 次的 50.0%）" in text
    assert "完成 4 次" not in text
    assert "llm.fail" in text, "有失败时给一行提示"
    clean = am.render_report(am.summarize([_fine(n_fine_done=2, n_fine_fail=0, llm_none=0)]))
    assert "llm.fail" not in clean


def test_fine_fail_every_call_failed_shows_no_none_rate():
    text = am.render_report(am.summarize([_fine(n_fine_done=2, n_fine_fail=2)]))
    assert "已并账调用 2 次（其中失败 2 次）" in text
    assert "无成功调用，不计 NONE 率" in text and "%" not in text.split("已并账调用")[1].split("\n")[0]
    # 失败数大于并账数（坏数据）：成功数夹到 0，不出现负分母 / 负百分比
    bad = am.render_report(am.summarize([_fine(n_fine_done=1, n_fine_fail=5)]))
    assert "无成功调用，不计 NONE 率" in bad


def test_zero_calls_line_names_only_causes_that_leave_zero_calls():
    """N=0 的提示只留确实会让并账调用数为 0 的原因：失败的调用会计进 N，「连续失败停用」不在其列。"""
    text = am.render_report(am.summarize([_fine(n_fine_done=0, n_fine_fail=0)]))
    line = [x for x in text.splitlines() if x.startswith("LLM 精排调用")][0]
    assert "没有并账到任何调用" in line and "找不到 claude 命令" in line
    assert "连续失败停用" not in line and "调用或外发上限" not in line


def test_catalog_budget_drop_and_summary_layer_survive_zero_catalog():
    """预算把目录层吃光（`n_catalog=0` 且 `n_catalog_dropped>0`）时，丢弃数与摘要层不能被均值那一半挡掉。"""
    s = am.summarize([_fine(n_catalog=0, n_catalog_dropped=40, n_summary_layer=12)])
    text = am.render_report(s)
    assert "超预算丢弃 40 篇（1 轮）" in text and "摘要层累计 12 篇" in text
    assert "目录层均" not in text


def test_catalog_off_is_stated():
    off = am.render_report(am.summarize([_fine(catalog_on=False, n_catalog=0, n_summary_layer=7)]))
    assert "目录层已关闭" in off and "摘要层累计 7 篇" in off
    mixed = am.render_report(am.summarize([_fine(catalog_on=True, n_catalog=300),
                                           _fine(catalog_on=False, n_catalog=0)]))
    assert "目录层均 300 篇" in mixed and "另有 1 轮目录层已关闭" in mixed
    on = am.render_report(am.summarize([_fine(catalog_on=True, n_catalog=300)]))
    assert "目录层已关闭" not in on


def test_supp_off_and_not_generated_are_told_apart():
    off = am.render_report(am.summarize([_fine(n_fine_done=1, supp_on=False)]))
    assert "逐轮补充：已关闭" in off and "未产生" not in off
    quiet = am.render_report(am.summarize([_fine(n_fine_done=1, supp_on=True)]))
    assert "逐轮补充：未产生" in quiet and "已关闭" not in quiet.split("逐轮补充")[1].split("\n")[0]
    both = am.render_report(am.summarize([_fine(n_fine_done=1, supp_on=True), _fine(supp_on=False)]))
    assert "逐轮补充：未产生" in both and "另有 1 轮补充已关闭" in both


def test_off_round_still_adds_in_flight_call_stats():
    """中途关闭那一轮仍会并账此前在途调用的统计：`fine_on` 只决定轮数分母，求和不因它跳过。"""
    s = am.summarize([_fine("s1", fine_on=False, n_fine_done=1, n_fine_fail=1, bytes_sent=500,
                            n_catalog=9, degraded=["lock"])])
    assert (s["fine_field_n"], s["fine_off_n"]) == (0, 1)
    assert (s["fine_done_sum"], s["fine_fail_sum"], s["catalog_sum"]) == (1, 1, 9)
    assert s["bytes_session_median"] == 500 and s["degraded_dist"] == {"lock": 1}
    text = am.render_report(s)
    assert "1 轮处于关闭状态" in text and "已并账调用 1 次（其中失败 1 次）" in text
    quiet = am.render_report(am.summarize([{"_schema": 2, "ts": 1.0, "session": "s", "fine_on": False}]))
    assert "已并账调用" not in quiet and "逐轮补充" not in quiet


def test_off_message_names_both_switches_and_not_dry_run():
    text = am.render_report(am.summarize([{"_schema": 2, "ts": 1.0, "session": "s", "fine_on": False}]))
    assert "relevance.fine_rank=false" in text and "relevance.session_topic=false" in text
    assert "dry_run" not in text.split("LLM 精排：")[1].split("\n")[0]


def test_fine_rounds_without_counts_are_named_not_diagnosed():
    """`fine_on=true` 却没有 `n_fine_done` 的轮次（dry_run 轮、精排状态读取失败的轮不记精排计数）不能据此
    判断精排在不在工作；它们不该被读成「调用发不出去」。"""
    s = am.summarize([_fine(), _fine()])
    assert (s["fine_on_n"], s["fine_noacct_n"]) == (2, 2)
    line = [x for x in am.render_report(s).splitlines() if x.startswith("LLM 精排调用")][0]
    assert "2 轮没有精排计数" in line and "dry_run" in line
    assert "找不到 claude 命令" not in line
    mixed = am.summarize([_fine(), _fine(n_fine_done=0)])
    mline = [x for x in am.render_report(mixed).splitlines() if x.startswith("LLM 精排调用")][0]
    assert "其中 1 轮没有精排计数" in mline and "找不到 claude 命令" in mline


def test_only_gate_records_are_not_labeled_old_records():
    gate = {"_schema": 2, "ts": 1.0, "session": "s", "prompt_id": "p", "gate": "too_few_keywords"}
    text = am.render_report(am.summarize([gate]))
    assert "2.4.0 之前的旧记录" not in text and "只有闸门轮" in text
    old = am.render_report(am.summarize([gate, {"_schema": 2, "ts": 1.0, "session": "s"}]))
    assert "2.4.0 之前的旧记录" in old


def test_only_supp_only_records_say_they_carry_no_call_stats():
    """旧 supp_only 记录（冻结夹具）不带调用统计：窗口里只有它们时不诊断成「调用发不出去」。"""
    s = am.summarize([_legacy_supp_only_record()])
    assert (s["fine_on_n"], s["fine_supp_only_n"]) == (0, 1)
    line = [x for x in am.render_report(s).splitlines() if x.startswith("LLM 精排调用")][0]
    assert "旧 supp_only 记录" in line and "找不到 claude 命令" not in line


def test_bytes_takes_each_sessions_largest_cumulative_value():
    """`bytes_sent` 是会话累计值：同一会话取最大的一轮，不取最后读到的一条（读入顺序不保证按时间）。"""
    s = am.summarize([_fine("s1", bytes_sent=61000), _fine("s1", bytes_sent=30000),
                      _fine("s2", bytes_sent=40000), _fine("s3", bytes_sent=50000)])
    # 每会话最大：s1=61000、s2=40000、s3=50000，中位 50000；取最后一条则 s1=30000，中位 40000
    assert s["bytes_session_median"] == 50000
    assert am.summarize([_fine(n_fine_done=1)])["bytes_session_median"] is None


def test_call_distribution_buckets_cover_every_range():
    counts = (0, 1, 2, 3, 5, 6, 10, 11, 20, 21)
    s = am.summarize([_fine(f"s{n}", n_fine_done=n) for n in counts])
    assert s["fine_calls_dist"] == {"0": 1, "1": 1, "2": 1, "3-5": 2, "6-10": 2, "11-20": 2, ">20": 1}
    text = am.render_report(s)
    order = ["0 次的 1 个", "1 次的 1 个", "2 次的 1 个", "3-5 次的 2 个", "6-10 次的 2 个",
             "11-20 次的 2 个", ">20 次的 1 个"]
    assert [text.index(x) for x in order] == sorted(text.index(x) for x in order)


def test_supp_only_record_counts_both_ends_of_the_funnel():
    """旧 supp_only 记录（闸门轮投递了补充）没有 `fine_on`，也要把「产生」和「投递」两端一并计入；
    用冻结的真实产出，不手写键。"""
    rec = _legacy_supp_only_record()
    assert "fine_on" not in rec
    s = am.summarize([rec])
    assert (s["supp_gen_sum"], s["supp_delivered_sum"], s["fine_field_n"], s["fine_off_n"]) == (1, 1, 1, 0)


def test_supp_annotation_rate(tmp_path, monkeypatch):
    from scripts import _metrics
    from scripts._annotations import save_annotation
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _metrics.configure_context("legacy")
    for i, v in enumerate(["relevant", "relevant", "relevant", "irrelevant", "unsure"]):
        save_annotation(tmp_path, f"笔记/s{i}.md", v, kind="llm_supplement")
    save_annotation(tmp_path, "笔记/x.md", "irrelevant", kind="admitted_list")
    ann = am.supp_annotation_counts(tmp_path, ("legacy",))
    assert ann == {"relevant": 3, "irrelevant": 1, "unsure": 1}
    s = am.summarize([_fine(n_fine_done=1)])
    s["supp_ann"] = ann
    assert "补充标注：相关 3 / 不相关 1（无关率 25.0%" in am.render_report(s)


def _adm(*paths):
    return [{"path": p, "topical": 5.0} for p in paths]


def test_sample_admitted_uses_shown_when_present():
    rec = {"max_notes": 3, "admitted": _adm("a.md", "b.md", "c.md"), "shown": ["b.md"]}
    got = {(x["kind"], x["path"]) for x in am.sample_admitted([rec], k=20)}
    assert got == {("admitted_list", "b.md")}


def test_sample_admitted_empty_shown_samples_no_list():
    rec = {"max_notes": 3, "admitted": _adm("a.md", "b.md"), "shown": []}
    assert am.sample_admitted([rec], k=20) == []


def test_sample_admitted_legacy_record_without_shown_unchanged():
    rec = {"max_notes": 3, "admitted": _adm("a.md", "b.md", "c.md", "d.md")}
    got = {x["path"] for x in am.sample_admitted([rec], k=20)}
    assert got == {"a.md", "b.md", "c.md"}


def test_sample_admitted_shown_skips_the_fulltext_path_and_non_strings():
    """`shown` 含全文那篇（渲染层的 `injected_paths` 以它打头）：它只进 admitted_fulltext 桶，不重复进清单；
    非字符串元素不抛、不入池。"""
    rec = {"max_notes": 3, "admitted": _adm("ft.md", "b.md", "x.md"), "ft": {"path": "ft.md"},
           "shown": ["ft.md", "b.md", 7, None, "", ["x"], {"a": 1}]}   # 列表 / 字典元素不可哈希，裸查 by_path 会抛
    got = {(x["kind"], x["path"]) for x in am.sample_admitted([rec], k=20)}
    assert got == {("admitted_fulltext", "ft.md"), ("admitted_list", "b.md")}


def test_sample_admitted_non_list_shown_falls_back_to_admitted():
    rec = {"max_notes": 3, "admitted": _adm("a.md", "b.md"), "shown": "a.md"}
    assert {x["path"] for x in am.sample_admitted([rec], k=20)} == {"a.md", "b.md"}


def test_sample_admitted_max_notes_zero_samples_no_list():
    """没有 `shown` 的记录（旧形态）里 `max_notes=0` 也是「清单不展示」，不能被当成非法值回落到 3；
    非法值（bool / 字符串 / 负数 / None）仍回落到 3。"""
    assert am.sample_admitted([{"max_notes": 0, "admitted": _adm("a.md", "b.md")}], k=20) == []
    for bad in (True, False, "x", -1, None, 2.0):
        got = {x["path"] for x in am.sample_admitted([{"max_notes": bad, "admitted": _adm("a.md", "b.md")}],
                                                     k=20)}
        assert got == {"a.md", "b.md"}, bad


def test_sample_admitted_takes_supplements():
    rec = {"max_notes": 3, "admitted": [], "shown": [], "supp_shown": ["s.md", 7]}
    got = [(x["kind"], x["path"]) for x in am.sample_admitted([rec], k=20)]
    assert got == [("llm_supplement", "s.md")]


def test_sample_admitted_ignores_string_supp_shown():
    rec = {"max_notes": 3, "admitted": [], "shown": [], "supp_shown": "abc"}
    assert am.sample_admitted([rec], k=20) == []


# ---- 端到端：真实 UPS 入口产出的记录，经 analyze_metrics.main() 的报表出口 ----

# 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id（test_no_secrets_in_tree）
SID = "-".join(("66666666", "7777", "8888", "9999", "aaaaaaaaaaaa"))
SUPP_NOTE = "笔记/注入设计复盘.md"
ASK_SUPP = "注入设计 复盘 怎么做"
NOTES = {
    "笔记/alpha.md": {"tags": ["hook"], "keywords": ["注入设计"],
                      "summary": "alpha 讲 hook 注入设计的取舍与实现细节，摘要足够长"},
    "笔记/beta.md": {"tags": ["hook"], "keywords": ["注入设计"],
                     "summary": "beta 讲 hook 注入设计的另一种做法，摘要也足够长"},
    SUPP_NOTE: {"tags": [], "keywords": [],
                "summary": "一次只在文件名里写着主题的复盘，摘要足够长但不含别的提问词"},
}
# 一条已写回的子进程统计（键取 `_fine_state.STATS_KEYS`）：数值互不相同，记错字段能看出来
STAT = {"gen_id": "s1", "mode": "supp", "ok": True, "fail": None, "none": False, "n_sel": 1,
        "n_echo_mismatch": 2, "n_catalog": 5, "n_catalog_dropped": 1, "n_summary_layer": 3,
        "bytes": 1234, "degraded": ["transcript_none"]}


def _pid(n: int) -> str:
    return "-".join(("aaaaaaaa", "bbbb", "cccc", "dddd", f"{n:012d}"))


def _legacy_supp_only_record() -> dict:
    """冻结夹具：2.4.0 开发期闸门轮投递补充时落盘的 supp_only 记录。生产者（`_metrics.supp_only_record` 与
    UPS 的 `_supp_only_exit`）已在批次 3c 删除，原实现用 `git log -S"def supp_only_record"` 取回。
    逐字取自删除前真实 UPS 入口的一条产出（指代轮「ok」投递了一篇补充，经 `load_records` 读回）；
    UUID 形态的会话 id、prompt_id、event_id 运行时拼接（发布闸门拦截 8-4-4-4-12 hex 字面量），其余键值原样。"""
    return {"_schema": 2, "event_id": _pid(5), "fmt": 2, "gate": "deictic_only", "inj_chars": 255,
            "n_supp_delivered": 1, "n_supp_drop_stale": 0, "n_supp_gen": 1, "n_supp_late_delivered": 0,
            "prompt_h": "fdae5ef6431006f3", "prompt_id": _pid(5), "runtime": "claude", "session": SID,
            "supp_shown": ["笔记/注入设计复盘.md"], "ts": 1792401470.977}


@pytest.fixture
def e2e(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache, monkeypatch):
    """起一套真实入口的环境：交互式入口、metrics 开、绝不真起精排子进程。"""
    from scripts import prompt_submit_load as psl
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.delenv("VAULT_LOADER_DISABLE", raising=False)
    monkeypatch.setattr(psl, "spawn_fine", lambda cwd, sid, payload: "spawned")
    entries = {}
    for rel, meta in NOTES.items():
        p = tmp_vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {Path(rel).stem}\n\n全文：{Path(rel).stem}\n", encoding="utf-8")
        entries[rel] = {"category": "笔记", "mtime": 1900000000, **meta}
    write_frontmatter_cache(entries)

    def configure(top=None, **relevance):
        """`top` 是 config 顶层的覆盖（如 dry_run、user_prompt_submit）；其余关键字落在 `relevance` 下。"""
        cfg = {"vault_path": str(tmp_vault), "metrics": {"enabled": True}}
        cfg.update(top or {})
        if relevance:
            cfg["relevance"] = relevance
        d = tmp_home / ".context-vault"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    return configure


def _seed_fine(**fields) -> None:
    from scripts import _fine_state, _state
    _state.configure_context("claude", SID)
    try:
        assert _fine_state.update_fine(SID, lambda st: replace(st, **fields)) is not None
    finally:
        _state.configure_context("unknown")


def _real_round(monkeypatch, capsys, cwd: Path, prompt: str, n: int) -> str:
    from scripts import prompt_submit_load as psl
    from scripts._output import reset_emit_guard
    payload = {"cwd": str(cwd), "prompt": prompt, "session_id": SID, "prompt_id": _pid(n),
               "hook_event_name": "UserPromptSubmit", "transcript_path": ""}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload, ensure_ascii=False)))
    reset_emit_guard()
    assert psl.main() == 0
    out = capsys.readouterr().out.strip()
    return (json.loads(out).get("hookSpecificOutput") or {}).get("additionalContext", "") if out else ""


def _report_via_main(monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "argv", ["analyze_metrics.py", "--report"])
    assert am.main() == 0
    return capsys.readouterr().out


def _real_records(home: Path) -> list:
    from scripts import _metrics
    _metrics.configure_context("claude")
    return list(am.load_records(home))


def test_report_counts_a_real_delivery(e2e, tmp_home, tmp_git_repo, monkeypatch, capsys):
    """真实一轮：补充被投递、子进程统计被并账；再来一轮没有统计的完整轮。报表从 `main()` 出口读真实落盘记录。"""
    from scripts import _metrics
    from scripts._annotations import save_annotation
    e2e()
    _seed_fine(turn_seq=1, supp={"gen_id": "gen-e2e", "src_seq": 1, "paths": [SUPP_NOTE],
                                 "ts": time.time() - 1.0},
               stats_q=[dict(STAT)])
    ctx = _real_round(monkeypatch, capsys, tmp_git_repo, ASK_SUPP, 1)
    assert "「注入设计复盘」" in ctx, "补充没有被投递进注入正文，本用例的前提不成立"
    # 第二轮：本会话已注入过的篇被 per-cwd 去重压掉，正文可以为空；这里要的只是一条没有子进程统计的完整记录
    _real_round(monkeypatch, capsys, tmp_git_repo, "注入设计 怎么做", 2)

    recs = [r for r in _real_records(tmp_home) if "admitted" in r]
    assert len(recs) == 2, "两轮都应有完整决策记录"
    first, second = sorted(recs, key=lambda r: r["prompt_id"])
    # 落盘字段名以真实产出为准（本断言钉住报表读的是这些键）
    assert (first["n_supp_gen"], first["n_supp_delivered"], first["supp_shown"]) == (1, 1, [SUPP_NOTE])
    assert (first["n_fine_done"], first["bytes_sent"], first["n_catalog"], first["n_echo_mismatch"]) \
        == (1, 1234, 5, 2)
    assert first["n_fine_fail"] == 0 and second["n_fine_fail"] == 0
    assert second["n_catalog"] == 0 and second["n_fine_done"] == 0 and second["bytes_sent"] == 1234

    _metrics.configure_context("claude")
    save_annotation(tmp_home, SUPP_NOTE, "irrelevant", kind="llm_supplement")
    text = _report_via_main(monkeypatch, capsys)
    assert "已并账调用 1 次（其中失败 0 次）" in text and "NONE 0 次（占成功 1 次的 0.0%）" in text
    assert "每会话累计外发：中位 1,234 字节" in text
    assert "产生 1 份 → 时效丢弃 0 篇、复核丢弃 0 篇、投递 1 篇（其中放宽档 0 篇）" in text
    assert "回显不一致丢弃 2 篇" in text
    assert "目录层均 5 篇（只计带目录层的轮），超预算丢弃 1 篇（1 轮）；摘要层累计 3 篇" in text
    assert "降级码分布：{'transcript_none': 1}" in text
    assert "补充标注：相关 0 / 不相关 1（无关率 100.0%" in text

    items = {(x["kind"], x["path"]) for x in am.sample_admitted(recs, k=20)}
    assert ("llm_supplement", SUPP_NOTE) in items
    listed = {p for kind, p in items if kind == "admitted_list"}
    # `shown` 只在真走到渲染的轮次落盘（第二轮的候选全被去重压掉，没有这个键，admitted 也为空）
    expected = {p for r in recs for p in (r.get("shown") or []) if p != (r.get("ft") or {}).get("path")}
    assert first["shown"] and expected and listed == expected, "清单侧应按渲染层落盘的 shown 抽样"


def test_report_counts_off_rounds_apart_from_the_denominator(e2e, tmp_home, tmp_git_repo,
                                                             monkeypatch, capsys):
    """真实入口关掉精排（relevance.fine_rank=false）：记录带 `fine_on=false`，报表单独计、不进分母。"""
    e2e(fine_rank=False)
    _real_round(monkeypatch, capsys, tmp_git_repo, "注入设计 怎么做", 3)
    text = _report_via_main(monkeypatch, capsys)
    assert "LLM 精排：1 轮处于关闭状态" in text


def test_real_coarse_off_round_samples_no_list_entries(e2e, tmp_home, tmp_git_repo,
                                                       monkeypatch, capsys):
    """粗排关：真实记录 `shown == []` 而 `admitted` 非空，清单侧一条都不抽（F31）；
    按旧口径从 admitted 截取会把从未渲染的条目混进不可再生的标注池。"""
    e2e(coarse_inject=False)
    _real_round(monkeypatch, capsys, tmp_git_repo, "注入设计 怎么做", 4)
    recs = [r for r in _real_records(tmp_home) if "admitted" in r]
    assert recs and recs[0]["shown"] == [] and recs[0]["n_admitted"] >= 2
    assert [x for x in am.sample_admitted(recs, k=20) if x["kind"] == "admitted_list"] == []


def test_real_gate_round_supp_is_counted_on_the_next_scored_round(e2e, tmp_home, tmp_git_repo,
                                                                  monkeypatch, capsys):
    """真实入口：指代轮（闸门轮）不投递、不认领补充，记录仍是 gate 五键；下一个过闸轮是第 src+2 轮、补充写回
    早于闸门轮开始，按时效丢弃。报表的漏斗两端都从过闸轮的完整记录来：「产生」计 1、时效丢弃计 1。"""
    e2e()
    _seed_fine(turn_seq=1, supp={"gen_id": "gen-so", "src_seq": 1, "paths": [SUPP_NOTE],
                                 "ts": time.time() - 1.0})
    assert "「注入设计复盘」" not in _real_round(monkeypatch, capsys, tmp_git_repo, "ok", 5)
    assert "「注入设计复盘」" not in _real_round(monkeypatch, capsys, tmp_git_repo, ASK_SUPP, 9)
    recs = _real_records(tmp_home)
    assert [r["gate"] for r in recs if "admitted" not in r] == ["deictic_only"]
    assert all("supp_shown" not in r for r in recs if "admitted" not in r)
    s = am.summarize(recs)
    assert (s["supp_gen_sum"], s["supp_drop_stale_sum"], s["supp_delivered_sum"]) == (1, 1, 0)
    assert (s["fine_on_n"], s["fine_supp_only_n"]) == (1, 0)
    text = _report_via_main(monkeypatch, capsys)
    assert "逐轮补充：产生 1 份、**一次都没投递**（时效丢弃 1 篇、复核丢弃 0 篇）" in text


def test_real_none_supplement_is_not_reported_as_never_delivered(e2e, tmp_home, tmp_git_repo,
                                                                 monkeypatch, capsys):
    """批次 3c（评审 R6 / F3）真实链路：过闸轮发起补充调用 → 子进程（替身模型）判 NONE → 下一轮过闸。NONE 不写
    空补充，漏斗里没有「产生」；报表照常计 NONE，但不得说「产生 N 份、**一次都没投递**」。"""
    from scripts import _state, _topic
    from scripts import prompt_submit_load as psl
    e2e()
    _state.configure_context("claude", SID)
    try:
        _topic.save_session_topic(tmp_git_repo, SID, ["注入设计"], pinned=[], accounted=False, state="ok")
    finally:
        _state.configure_context("unknown")
    spawned: list = []
    monkeypatch.setattr(psl, "spawn_fine", lambda cwd, sid, payload: (
        spawned.append((cwd, sid, payload, _state.current_runtime())), "spawned")[1])
    _real_round(monkeypatch, capsys, tmp_git_repo, ASK_SUPP, 21)
    assert len(spawned) == 1 and spawned[0][2]["mode"] == "supp", "第一轮没有发起补充调用，本用例的前提不成立"
    cwd, sid, payload, runtime = spawned[0]
    monkeypatch.setattr(_topic, "_call_model", lambda _text: "SUPPLEMENT: NONE\nWORDS: 注入设计")
    assert _topic.run_extraction_child([str(cwd), sid, runtime],
                                       stdin_text=json.dumps(payload, ensure_ascii=False)) == 0
    _real_round(monkeypatch, capsys, tmp_git_repo, ASK_SUPP, 22)
    s = am.summarize([r for r in _real_records(tmp_home) if "admitted" in r])
    assert (s["fine_done_sum"], s["llm_none_sum"], s["supp_gen_sum"]) == (1, 1, 0)
    text = _report_via_main(monkeypatch, capsys)
    assert "NONE 1 次" in text and "逐轮补充：未产生" in text
    assert "**一次都没投递**" not in text


def test_real_failed_stats_are_reported_as_fail(e2e, tmp_home, tmp_git_repo, monkeypatch, capsys):
    """真实入口种入两条 ok=False 的子进程统计：记录里 `n_fine_done` 含失败，`n_fine_fail` 单独计；
    报表不把它们写成「完成」，NONE 率不在零成功的分母上算。"""
    e2e()
    fails = [dict(STAT, gen_id=f"f{i}", ok=False, fail="timeout", n_echo_mismatch=0, n_catalog=0,
                  n_catalog_dropped=0, n_summary_layer=0, bytes=100, degraded=[]) for i in (1, 2)]
    _seed_fine(stats_q=fails)
    _real_round(monkeypatch, capsys, tmp_git_repo, "注入设计 怎么做", 6)
    recs = [r for r in _real_records(tmp_home) if "admitted" in r]
    assert len(recs) == 1 and (recs[0]["n_fine_done"], recs[0]["n_fine_fail"]) == (2, 2)
    text = _report_via_main(monkeypatch, capsys)
    assert "已并账调用 2 次（其中失败 2 次）" in text
    assert "完成 2 次" not in text and "无成功调用，不计 NONE 率" in text


def test_real_dry_run_round_is_not_diagnosed_as_a_failed_call(e2e, tmp_home, tmp_git_repo,
                                                              monkeypatch, capsys):
    """dry_run 下记录里 `fine_on` 仍为 true、却没有精排计数：报表只说「没有精排计数」，
    不把它读成「调用发不出去」。"""
    e2e(top={"dry_run": True})
    _real_round(monkeypatch, capsys, tmp_git_repo, "注入设计 怎么做", 7)
    recs = _real_records(tmp_home)
    assert recs and recs[0]["fine_on"] is True and "n_fine_done" not in recs[0]
    text = _report_via_main(monkeypatch, capsys)
    line = [x for x in text.splitlines() if x.startswith("LLM 精排调用")][0]
    assert "没有精排计数" in line and "找不到 claude 命令" not in line


def test_real_max_notes_zero_round_records_empty_shown(e2e, tmp_home, tmp_git_repo, monkeypatch, capsys):
    """`max_notes=0` 时渲染层不展示粗排清单（`show_coarse` 为假），但 admitted 非空：记录里 `shown` 必须是 `[]`，
    清单侧一条都不抽——此前 `shown=[]` 只在粗排关时写，这里会漏。"""
    e2e(top={"user_prompt_submit": {"max_notes": 0}})
    _real_round(monkeypatch, capsys, tmp_git_repo, "注入设计 怎么做", 8)
    recs = [r for r in _real_records(tmp_home) if "admitted" in r]
    assert recs and recs[0]["n_admitted"] >= 2 and recs[0]["max_notes"] == 0
    assert recs[0].get("shown") == []
    assert [x for x in am.sample_admitted(recs, k=20) if x["kind"] == "admitted_list"] == []
