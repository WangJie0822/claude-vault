"""本会话排除进决策层（spec §6.1）。"""
from __future__ import annotations

from scripts._config_loader import DEFAULT_CONFIG
from scripts._decision import USER_EXCLUDED, StateView, decide_injection
from scripts._frontmatter_reader import Entry
from scripts._scorer import Signals


def _e(path, tags=(), keywords=(), summary="s"):
    return Entry(path=path, tags=tuple(tags), summary=summary, keywords=tuple(keywords),
                 mtime=1000.0)


def _decide(entries, state):
    return decide_injection(entries, Signals(prompt_keywords={"字段"}),
                            DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG, state)


def test_excluded_note_is_not_admitted():
    entries = {"a.md": _e("a.md", keywords=("病历字段",)), "b.md": _e("b.md", keywords=("字段说明",))}
    d = _decide(entries, StateView(user_excluded={"a.md"}))
    assert [ed.path for ed in d.admitted] == ["b.md"]
    ex = {ed.path: ed.dedup for ed in d.excluded}
    assert ex["a.md"] == USER_EXCLUDED == "user_excluded"


def test_exclusion_precedes_dedup_branches():
    entries = {"a.md": _e("a.md", keywords=("病历字段",))}
    st = StateView(fulltext_injected={"a.md"}, candidate_injected={"a.md"}, user_excluded={"a.md"})
    d = _decide(entries, st)
    assert [ed.dedup for ed in d.excluded] == [USER_EXCLUDED]


def test_excluded_upgrade_candidate_is_not_readmitted():
    e = _e("a.md", tags=("字段",), keywords=("病历字段", "字段说明"), summary="字段 字段")
    d = _decide({"a.md": e}, StateView(candidate_injected={"a.md"}, user_excluded={"a.md"}))
    assert not d.admitted and d.fulltext_path is None


def test_excluding_the_only_relevant_note_suppresses_fallback_hint():
    d = _decide({"a.md": _e("a.md", keywords=("病历字段",))}, StateView(user_excluded={"a.md"}))
    assert not d.admitted and d.any_relevant


def test_exclusion_does_not_change_other_scores():
    entries = {"a.md": _e("a.md", tags=("t",), keywords=("病历字段",)),
               "b.md": _e("b.md", tags=("t",), keywords=("字段说明",))}
    base = {ed.path: ed.total for ed in _decide(entries, StateView()).admitted}
    with_ex = {ed.path: ed.total for ed in _decide(entries, StateView(user_excluded={"a.md"})).admitted}
    assert with_ex["b.md"] == base["b.md"]


def test_default_state_view_has_empty_exclusion():
    assert StateView().user_excluded == set()
