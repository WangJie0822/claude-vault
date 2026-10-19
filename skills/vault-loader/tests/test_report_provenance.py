# -*- coding: utf-8 -*-
"""UPS 快照的来源明细与 LLM 状态接线（spec 2026-10-12 §3.1、§3.3）。"""
from __future__ import annotations

import ast
import copy
from pathlib import Path

from scripts import _session_ops
from scripts import prompt_submit_load as psl
from scripts._config_loader import DEFAULT_CONFIG
from scripts._decision import Decision, EntryDecision
from scripts._frontmatter_reader import Entry

SRC = Path(psl.__file__).read_text(encoding="utf-8")
PATHS = ("笔记/a.md", "笔记/b.md", "笔记/c.md")
RLO = chr(0x202E)                     # bidi 覆盖字符：写成字面会被编辑器吞掉，运行时拼接


def _ed(path, topical=7.0, arm="topical", admitted=True, dedup=""):
    return EntryDecision(path=path, topical=topical, total=topical, hits=["hook"],
                         admitted=admitted, admit_arm=arm if admitted else "", dedup=dedup)


def _fill(monkeypatch, *, topic_words, weight=2, pinned_render=(), excluded=()):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["scoring"]["session_topic_hit"] = weight
    entries = {p: Entry(path=p, tags=("hook",), summary="设计说明", keywords=("注入设计",))
               for p in PATHS}
    decision = Decision(admitted=[_ed("笔记/a.md"), _ed("笔记/b.md", 5.0, "keyword_bypass")],
                        excluded=list(excluded), fulltext_path=None, fulltext_arm="",
                        any_relevant=True, relaxed=False, gate_reason="")
    rep = _session_ops.TurnReport(request_seen=True)
    psl._fill_report(psl._Turn(report=rep), config=cfg, prompt="hook 注入设计", home=Path("."),
                     active_entries=entries,
                     decision=decision, injected_paths=["笔记/a.md", "笔记/b.md"],
                     fulltext_title=None,
                     hits_by_path={"笔记/a.md": ["hook"], "笔记/b.md": ["hook"]},
                     pinned_render=list(pinned_render), pinned_rendered=[], user_pins=[],
                     user_rendered=[], excluded_paths=frozenset(), topic_words=topic_words)
    return rep, cfg


def test_report_llm_swallows_describe_errors(monkeypatch, capsys):
    """精排状态读取出错只让 llm 留空，不得逃出 _report_llm：main() 没有 except，逃出去本轮召回整体丢失
    （full-review T-M2：把 except 改成 raise 此前全量不红）。"""
    rep = _session_ops.TurnReport(request_seen=True)

    def boom(*a, **k):
        raise OverflowError("cannot convert float infinity to integer")

    monkeypatch.setattr(psl, "describe_session_topic", boom)
    psl._report_llm(psl._Turn(report=rep), copy.deepcopy(DEFAULT_CONFIG), Path("."), {"session_id": "s"})
    assert rep.llm is None
    assert "精排状态读取失败" in capsys.readouterr().err


def test_report_llm_sanitizes_words(monkeypatch):
    """LLM 主题词进快照前剥 bidi 与零宽：_topic._clean_words 只剥 C0/C1，这里是唯一一道（full-review T-M5）。"""
    rep = _session_ops.TurnReport(request_seen=True)
    base = {"state": "ok", "fail": None, "words": ["a" + RLO + "b", chr(0x200B) + "cd"],
            "in_scoring": None, "n_pinned": 0, "age_s": 1, "pin_on": True, "stale_pins": False}
    monkeypatch.setattr(psl, "describe_session_topic", lambda *a, **k: dict(base))
    psl._report_llm(psl._Turn(report=rep), copy.deepcopy(DEFAULT_CONFIG), Path("."), {"session_id": "s"})
    assert rep.llm["words"] == ["ab", "cd"]


def test_provenance_numbers_are_finite_or_none(monkeypatch):
    """配置里的非有限数值不进快照：Python 会写出 Infinity，mod 的 JSON.parse 整份拒收（full-review S-1）。"""
    rep = _session_ops.TurnReport(request_seen=True)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["relevance"]["fulltext_topical_threshold"] = float("inf")
    cfg["relevance"]["min_topical_score"] = float("nan")
    cfg["scoring"]["session_topic_hit"] = True               # bool 不是数值
    entries = {p: Entry(path=p, tags=("hook",), summary="设计说明") for p in PATHS}
    decision = Decision(admitted=[_ed("笔记/a.md", float("inf"))], excluded=[], fulltext_path=None,
                        fulltext_arm="", any_relevant=True, relaxed=False, gate_reason="")
    rep.injected = [{"path": "笔记/a.md"}]
    psl._fill_provenance(rep, cfg, entries, decision, [], frozenset())
    assert rep.thresholds == {"min_topical": None, "ft_topical": None, "topic_hit_weight": None}
    assert rep.injected[0]["topical"] is None


def test_scoring_turn_topic_reads_are_bounded(tmp_home, tmp_vault, tmp_git_repo,
                                             write_frontmatter_cache, monkeypatch):
    """带 mod 请求的打分轮，topics 文件经 `_load_entry` 最多读 5 次（既有 3 次 + 快照两次 describe）。
    与机器负载无关的确定性守卫：挡住「读次数悄悄翻倍」这类回归（full-review T-H1 / P-L1）。"""
    import hashlib
    import io
    import json
    import sys
    from scripts import _state, _topic

    (tmp_home / ".context-vault").mkdir(parents=True, exist_ok=True)
    (tmp_home / ".context-vault" / "config.json").write_text(json.dumps(
        {"vault_path": str(tmp_vault), "relevance": {"session_topic": True}}), encoding="utf-8")
    (tmp_vault / "笔记").mkdir(parents=True, exist_ok=True)
    entries = {}
    for name in ("alpha", "beta", "gamma"):
        (tmp_vault / "笔记" / f"{name}.md").write_text(f"# {name}", encoding="utf-8")
        entries[f"笔记/{name}.md"] = {"tags": ["hook"], "category": "笔记",
                                       "keywords": ["注入设计", "hook 注入"],
                                       "summary": f"{name} 的 hook 注入设计说明，足够长",
                                       "mtime": 1900000000}
    write_frontmatter_cache(entries)
    sid = "-".join(("11111111", "2222", "3333", "4444", "555555555555"))
    pid = "-".join(("aaaaaaaa", "bbbb", "cccc", "dddd", "000000000001"))
    _state.configure_context("claude")
    _topic.save_session_topic(tmp_git_repo, sid, ["注入设计"], pinned=[], accounted=False, state="ok")
    _state.configure_context("unknown")
    live = tmp_home / ".context-vault" / "state" / "live"
    live.mkdir(parents=True, exist_ok=True)
    sid_h = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:24]
    (live / f"{sid_h}-{pid}.req.json").write_text('{"v": 1}', encoding="utf-8")
    empty = tmp_home / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))           # 预置失效时也不得真起付费的 claude 子进程

    calls = []
    real = _topic._load_entry
    monkeypatch.setattr(_topic, "_load_entry", lambda *a, **k: calls.append(a[1]) or real(*a, **k))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"cwd": str(tmp_git_repo), "prompt": "hook 注入设计 怎么做", "session_id": sid,
         "prompt_id": pid, "hook_event_name": "UserPromptSubmit"})))
    assert psl.main() == 0
    snap = json.loads((live / f"{sid_h}.json").read_text(encoding="utf-8"))
    assert snap["status"] == "scored" and snap["llm"]["state"] == "ok"   # 走到了带请求的打分轮
    assert 1 <= len(calls) <= 5, calls


def test_hits_written_even_with_metrics_off(monkeypatch):
    rep, cfg = _fill(monkeypatch, topic_words=frozenset())
    assert cfg["metrics"]["enabled"] is False
    assert [i["hits"] for i in rep.injected] == [["hook"], ["hook"]]


def test_provenance_fields(monkeypatch):
    excl = [_ed("笔记/c.md", 7.0, admitted=False, dedup="candidate_injected")]
    rep, cfg = _fill(monkeypatch, topic_words=frozenset({"注入设计"}),
                     pinned_render=["笔记/b.md", "笔记/c.md"], excluded=excl)
    a, b = rep.injected
    assert (a["topical"], a["admit_arm"], a["topic_hits"], a["llm_also_pinned"]) == \
        (7.0, "topical", ["注入设计"], False)
    assert (b["admit_arm"], b["llm_also_pinned"]) == ("keyword_bypass", True)
    pins = {p["path"]: p for p in rep.pins_llm}
    assert (pins["笔记/b.md"]["rank"], pins["笔记/b.md"]["dedup"]) == (2, None)
    assert (pins["笔记/c.md"]["rank"], pins["笔记/c.md"]["dedup"]) == (None, "candidate_injected")
    assert rep.thresholds == {"min_topical": cfg["relevance"]["min_topical_score"],
                              "ft_topical": cfg["relevance"]["fulltext_topical_threshold"],
                              "topic_hit_weight": 2}


def test_topic_hits_empty_when_weight_zero(monkeypatch):
    rep, _ = _fill(monkeypatch, topic_words=frozenset({"注入设计"}), weight=0)
    assert all(i["topic_hits"] == [] for i in rep.injected)


def test_topic_hits_are_sanitized(monkeypatch):
    monkeypatch.setattr(psl, "_hit_keywords", lambda entry, words: ["a" + RLO + "b"])
    rep, _ = _fill(monkeypatch, topic_words=frozenset({"x"}))
    assert rep.injected[0]["topic_hits"] == ["ab"]


def test_provenance_error_keeps_injected(monkeypatch):
    """来源明细出错只少几个键，不能把注入列表一起清空（节点 1 T13）。"""
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(psl, "_fill_provenance", boom)
    rep, _ = _fill(monkeypatch, topic_words=frozenset())
    assert [i["path"] for i in rep.injected] == ["笔记/a.md", "笔记/b.md"]


def _calls(name: str) -> list:
    nodes = [n for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == name]
    return sorted(nodes, key=lambda n: n.lineno)


def _kwargs(call) -> dict:
    return {k.arg: ast.unparse(k.value) for k in call.keywords}


def test_fill_report_gets_scoring_topic_words():
    """topic_hits 必须来自本轮实际参与打分的词，不重读状态（节点 1 T6）。"""
    calls = _calls("_fill_report")
    assert len(calls) == 2
    for c in calls:
        assert _kwargs(c).get("topic_words") == "signals.session_topic_words", _kwargs(c)


def test_llm_reported_before_gates_and_after_spawn():
    calls = _calls("_report_llm")
    assert len(calls) == 2
    first, second = (_kwargs(c) for c in calls)
    assert "in_scoring" not in first and "spawn_outcome" not in first
    assert second.get("in_scoring") == "len(signals.session_topic_words)"
    assert second.get("spawn_outcome") == "spawn_outcome"
    gates = [n.lineno for n in _calls("_stage_gate_record")]
    assert calls[0].lineno < min(gates)            # 闸门轮也要带 llm（spec §3.1）
