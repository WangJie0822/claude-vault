# -*- coding: utf-8 -*-
"""build_record 的四个对账字段：None 不落键，落了就是指定形态。"""
from __future__ import annotations

from pathlib import Path

from scripts._decision import Decision, EntryDecision
from scripts._metrics import build_record


def _dec(paths):
    adm = [EntryDecision(path=p, topical=5.0, total=5.0, hits=[], admitted=True, admit_arm="topical", dedup="")
           for p in paths]
    return Decision(admitted=adm, excluded=[], fulltext_path=None, fulltext_arm="", any_relevant=True,
                    relaxed=False, gate_reason="")


def test_fields_absent_when_none():
    r = build_record(_dec(["a.md"]), ["x"], Path("."), session_id="s", prompt_id="p", salt=b"0" * 32, src="")
    for k in ("llm_pinned", "llm_pinned_rank", "topic_extracted_h", "n_topic_extracted"):
        assert k not in r


def test_fields_present_when_given():
    r = build_record(_dec(["a.md"]), ["x"], Path("."), session_id="s", prompt_id="p", salt=b"0" * 32, src="",
                     llm_pinned=["a.md"], llm_pinned_rank=[1], topic_extracted_h=["ab" * 8], n_topic_extracted=5)
    assert r["llm_pinned"] == ["a.md"] and r["llm_pinned_rank"] == [1]
    assert r["topic_extracted_h"] == ["ab" * 8] and r["n_topic_extracted"] == 5


def test_pin_on_always_present_and_new_fields_absent_when_none():
    """`pin_on` 与 `topic_on` 同族：无条件落盘（1A-M1 / 1B-M-2）；候选池构成与去重伴随字段 None 不落键。"""
    r = build_record(_dec(["a.md"]), ["x"], Path("."), session_id="s", prompt_id="p", salt=b"0" * 32, src="")
    assert r["pin_on"] is True
    for k in ("n_pool", "n_pool_proj", "llm_pinned_dedup"):
        assert k not in r


def test_pool_and_dedup_fields_present_when_given():
    r = build_record(_dec(["a.md"]), ["x"], Path("."), session_id="s", prompt_id="p", salt=b"0" * 32, src="",
                     pin_on=False, n_pool=30, n_pool_proj=7,
                     llm_pinned=["a.md", "b.md"], llm_pinned_rank=[1, None],
                     llm_pinned_dedup=[None, "candidate_injected"])
    assert r["pin_on"] is False and r["n_pool"] == 30 and r["n_pool_proj"] == 7
    assert r["llm_pinned_dedup"] == [None, "candidate_injected"]


def test_dedup_field_coerces_non_str_to_none():
    r = build_record(_dec(["a.md"]), ["x"], Path("."), session_id="s", prompt_id="p", salt=b"0" * 32, src="",
                     llm_pinned_dedup=[3, "fulltext_injected", True, None])
    assert r["llm_pinned_dedup"] == [None, "fulltext_injected", None, None]
