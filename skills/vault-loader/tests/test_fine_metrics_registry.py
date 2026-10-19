# -*- coding: utf-8 -*-
"""2.4.0 精排的 metrics 登记（spec 2026-10-13 §12）：自描述字段、annotate 白名单、gate 记录五键、
llm_supplement 标注类别与配额、坏值清洗单点、--purge 覆盖精排状态。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import _annotations as ann
from scripts import _metrics
from scripts import _session_ops as ops
from scripts import analyze_metrics as am
from scripts._decision import Decision

# 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id（test_no_secrets_in_tree）
SID = "-".join(("11111111", "2222", "3333", "4444", "555555555555"))
PH = "0123456789abcdef"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

FINE_ANNOTATE = {"supp_shown", "n_supp_gen", "n_supp_drop_stale", "n_supp_drop_recheck",
                 "n_supp_delivered", "n_supp_late_delivered", "bytes_sent", "n_fine_done", "n_fine_fail",
                 "llm_none", "n_echo_mismatch", "n_catalog", "n_catalog_dropped", "n_summary_layer",
                 "vault_reads", "n_vault_reads", "n_vault_reads_injected", "degraded", "fmt"}
FINE_NUMERIC = {"n_supp_gen", "n_supp_drop_stale", "n_supp_drop_recheck", "n_supp_delivered",
                "n_supp_late_delivered", "bytes_sent", "n_fine_done", "n_fine_fail", "llm_none",
                "n_echo_mismatch",
                "n_catalog", "n_catalog_dropped", "n_summary_layer", "n_vault_reads",
                "n_vault_reads_injected", "fmt", "pin_max", "supp_max"}
FINE_FIELDS = ("coarse_on", "fine_on", "supp_on", "catalog_on", "ft_on", "fine_model",
               "pin_max", "supp_max", "fmt")


def _build(tmp_path, **kw):
    d = Decision(admitted=[], excluded=[], fulltext_path=None, fulltext_arm="",
                 any_relevant=False, relaxed=False, gate_reason="")
    return _metrics.build_record(d, set(), tmp_path, session_id="s", prompt_id="p",
                                 salt=b"x" * 16, src="", **kw)


def test_build_record_writes_fine_self_describing_fields(tmp_path):
    r = _build(tmp_path, coarse_on=False, fine_on=True, supp_on=True, catalog_on=False, ft_on=False,
               fine_model="sonnet", pin_max=1, supp_max=3, fmt=2)
    assert (r["coarse_on"], r["fine_on"], r["supp_on"], r["catalog_on"], r["ft_on"]) == \
        (False, True, True, False, False)
    assert r["fine_model"] == "sonnet" and r["pin_max"] == 1 and r["supp_max"] == 3 and r["fmt"] == 2


def test_build_record_omits_fine_fields_when_not_passed(tmp_path):
    r = _build(tmp_path)
    for k in FINE_FIELDS:
        assert k not in r, k
    assert "topic_on" in r          # 旧字段照旧无条件落


def test_build_record_drops_bad_typed_fine_values(tmp_path):
    r = _build(tmp_path, fine_model="", pin_max=True, supp_max="3", fmt=2.0)
    for k in ("fine_model", "pin_max", "supp_max", "fmt"):
        assert k not in r, k


def test_annotate_accepts_fine_fields():
    assert FINE_ANNOTATE <= _metrics._ANNOTATE_ALLOWED
    _metrics.stage({"_schema": 1})
    try:
        _metrics.annotate(supp_shown=["a.md"], n_supp_delivered=1, degraded=["lock"], fmt=2)
        assert _metrics._PENDING["supp_shown"] == ["a.md"]
        assert _metrics._PENDING["degraded"] == ["lock"] and _metrics._PENDING["fmt"] == 2
    finally:
        _metrics.reset()


def test_gate_record_keys_are_the_five():
    assert _metrics.GATE_RECORD_KEYS == {"_schema", "ts", "session", "prompt_id", "gate"}


@pytest.mark.parametrize("field", sorted(FINE_NUMERIC))
def test_fine_numeric_fields_are_cleaned_in_one_place(field):
    r = {"_schema": 1, field: "abc"}
    assert am._drop_bad_numeric_fields(r) == 1 and field not in r


def test_fine_list_fields_drop_bad_elements():
    r = {"_schema": 1, "supp_shown": ["a.md", 3], "vault_reads": ["b.md", None], "degraded": ["lock", {}]}
    am._drop_bad_list_elements(r)
    assert r["supp_shown"] == ["a.md"] and r["vault_reads"] == ["b.md"] and r["degraded"] == ["lock"]


def test_llm_supplement_kind_registered_everywhere():
    assert "llm_supplement" in ann.KINDS and am.KINDS is ann.KINDS
    assert "llm_supplement" in ops.ANN_KINDS
    assert am._KIND_BUCKET["llm_supplement"] == "supp"
    r = {"supp_shown": ["s.md"]}
    assert am._kind_hits_path(r, "s.md", "llm_supplement") is True
    assert am._kind_hits_path(r, "x.md", "llm_supplement") is False
    assert am._kind_hits_path({"llm_pinned": ["s.md"]}, "s.md", "llm_supplement") is False


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _metrics.configure_context("legacy")
    return tmp_path


def _a(**kw):
    base = {"id": "ann-00000009", "path": "s.md", "kind": "llm_supplement", "verdict": "relevant",
            "session": SID, "prompt_h": PH, "ts": 100}
    base.update(kw)
    return base


def test_apply_llm_supplement_checks_supp_shown_in_session(home):
    """补充只在投递那一轮出现；2.4.0 开发期的旧 supp_only 记录没有打分轮的上下文，按会话内任一记录核对。"""
    _metrics.write_record(home, SID, {"_schema": 1, "session": SID, "prompt_id": "p1",
                                      "gate": "deictic_only", "supp_shown": ["s.md"]})
    _metrics.write_record(home, SID, {"_schema": 1, "session": SID, "prompt_id": "p2", "prompt_h": PH,
                                      "shown": ["x.md"], "gate": "", "llm_pinned": ["p.md"]})
    acked, rejected = ann.apply_annotations(home, [_a()])
    assert acked == ["ann-00000009"] and rejected == []
    acked, rejected = ann.apply_annotations(home, [_a(id="ann-00000010", path="p.md")])
    assert acked == [] and rejected == [{"what": "annotation", "key": "ann-00000010",
                                         "reason": "no_record"}]
    assert ann.load_annotations(home)[("llm_supplement", "s.md")] == "relevant"


def _item(kind, path, count=1):
    return {"kind": kind, "path": path, "count": count, "topical_max": 0.0}


def test_quota_gives_supp_bucket_its_own_share():
    ranked = [_item("admitted_list", f"l{i}.md", 50 - i) for i in range(20)] + [_item("llm_supplement", "s.md")]
    out = am._apply_kind_quota(ranked, 8)
    assert [x["path"] for x in out if x["kind"] == "llm_supplement"] == ["s.md"]
    assert len(out) == 8


def test_quota_without_supp_items_is_unchanged():
    """没有补充条目时配额与 2.3.0 逐条相同：n_pin=2、全文 4、清单 4。"""
    ranked = ([_item("admitted_fulltext", f"f{i}.md", 30 - i) for i in range(6)]
              + [_item("llm_pinned", f"p{i}.md") for i in range(3)]
              + [_item("admitted_list", f"l{i}.md", 40 - i) for i in range(10)])
    out = am._apply_kind_quota(ranked, 10)
    assert [x["path"] for x in out] == ["f0.md", "f1.md", "f2.md", "f3.md", "p0.md", "p1.md",
                                        "l0.md", "l1.md", "l2.md", "l3.md"]


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_quota_small_k_never_exceeds_k(k):
    ranked = [_item("llm_pinned", "p.md"), _item("llm_supplement", "s.md"), _item("admitted_list", "l.md", 9)]
    assert len(am._apply_kind_quota(ranked, k)) <= k


def test_sample_admitted_pools_supp_shown():
    recs = [{"_schema": 1, "session": "s", "gate": "deictic_only", "supp_shown": ["s.md"]},
            {"_schema": 1, "session": "s", "gate": "", "admitted": [], "ft": {"path": ""},
             "max_notes": 3, "supp_shown": ["s.md", "t.md"]}]
    got = {(x["kind"], x["path"], x["count"]) for x in am.sample_admitted(recs, k=8)}
    assert ("llm_supplement", "s.md", 2) in got and ("llm_supplement", "t.md", 1) in got


def test_sample_admitted_supp_shown_must_be_list():
    """坏记录里 supp_shown 是字符串时不得逐字符取样（合稿补充）。"""
    recs = [{"_schema": 1, "session": "s", "gate": "deictic_only", "supp_shown": "s.md"}]
    assert [x for x in am.sample_admitted(recs, k=8) if x["kind"] == "llm_supplement"] == []


def test_purge_also_clears_fine_state(tmp_home):
    d = tmp_home / ".context-vault" / "state" / "claude" / "fine"
    (d / "slots").mkdir(parents=True)
    (d / f"{'a' * 24}.json").write_text("{}", encoding="utf-8")
    (d / "slots" / "slot-0.json").write_text("{}", encoding="utf-8")
    env = os.environ.copy()
    env.update({"HOME": str(tmp_home), "USERPROFILE": str(tmp_home), "PYTHONUTF8": "1"})
    r = subprocess.run([sys.executable, "-B", str(SCRIPTS / "analyze_metrics.py"), "--purge"],
                       env=env, cwd=str(SCRIPTS.parent), capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    assert not (d / f"{'a' * 24}.json").exists()
    assert "精排状态 2 个" in r.stdout
