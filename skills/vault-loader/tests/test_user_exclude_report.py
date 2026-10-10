"""user_excluded 不得被报表当成「曾注入过」（spec §6.1、impact M1）。"""
from __future__ import annotations

from scripts import analyze_metrics as am
from scripts._decision import USER_EXCLUDED, Decision, EntryDecision
from scripts import _metrics


def _rec(**kw):
    base = {"_schema": 1, "ts": 1.0, "session": "s", "src": "", "gate": "",
            "near_miss_scorelow": [], "near_miss": []}
    base.update(kw)
    return base


def test_suppressed_board_skips_user_excluded():
    r = _rec(near_miss=[{"path": "a.md", "topical": 9.0, "dedup": USER_EXCLUDED},
                        {"path": "b.md", "topical": 8.0, "dedup": "fulltext_injected"}])
    s = am.summarize([r])
    assert [p for p, _ in s["near_miss_suppressed_top"]] == ["b.md"]


def test_acc_pin_counts_user_excluded_separately():
    r = _rec(pinned_injected=0, llm_pinned=["a.md", "b.md"], llm_pinned_rank=[None, None],
             llm_pinned_dedup=[USER_EXCLUDED, "candidate_injected"])
    s = am.summarize([r])
    assert s["pin_user_excluded_n"] == 1
    assert s["pin_dedup_n"] == 1


def test_render_pin_line_names_user_exclusion():
    r = _rec(pinned_injected=1, llm_pinned=["a.md"], llm_pinned_rank=[None],
             llm_pinned_dedup=[USER_EXCLUDED])
    text = am._render_pin(am.summarize([r]))
    assert "用户排除 1" in text


def test_dedup_distribution_labels_user_excluded():
    r = _rec(dedup_counts={USER_EXCLUDED: 2, "": 3})
    out = am.render_report(am.summarize([r]), show_paths=False)
    assert "用户本会话排除" in out


def test_build_record_keeps_user_excluded_out_of_scorelow():
    ed = EntryDecision(path="a.md", topical=5.0, total=0.0, hits=[], admitted=False,
                       admit_arm="", dedup=USER_EXCLUDED)
    d = Decision(admitted=[], excluded=[ed], fulltext_path=None, fulltext_arm="",
                 any_relevant=True, relaxed=False, gate_reason="")
    rec = _metrics.build_record(d, {"x"}, "/c", session_id="s", prompt_id="p", src="",
                                prompt="x", salt=b"k")
    assert rec["near_miss_scorelow"] == []
    assert rec["dedup_counts"] == {USER_EXCLUDED: 1}
