"""手动钉住（spec §6.2）：合计上限单点、独立小节、PIN_HEADER 不变、新字段登记。"""
from __future__ import annotations

from scripts import _metrics, analyze_metrics as am
from scripts._config_loader import DEFAULT_CONFIG
from scripts._frontmatter_reader import Entry
from scripts._output import INJECTION_NOTICE
from scripts._topic import MAX_PIN_HARD_CAP, split_pin_budget
from scripts.prompt_submit_load import (PIN_HEADER, USER_PIN_HEADER,
                                        render_user_pins_section)


def _cfg(enabled=True, pin_max=1):
    import copy
    c = copy.deepcopy(DEFAULT_CONFIG)
    c["relevance"]["session_pin_enabled"] = enabled
    c["relevance"]["session_pin_max"] = pin_max
    return c


def test_budget_defaults_leave_llm_unchanged_when_no_user_pins():
    assert split_pin_budget(0, _cfg(pin_max=1)) == (0, 1)
    assert split_pin_budget(0, _cfg(pin_max=8)) == (0, 8)


def test_budget_user_first_and_total_capped():
    assert split_pin_budget(3, _cfg(pin_max=8)) == (3, 5)
    assert split_pin_budget(8, _cfg(pin_max=8)) == (8, 0)
    assert split_pin_budget(20, _cfg(pin_max=1)) == (MAX_PIN_HARD_CAP, 0)


def test_budget_user_pins_survive_llm_switch_off():
    assert split_pin_budget(2, _cfg(enabled=False)) == (2, 0)


def _e(path, summary="这是一段足够长的摘要文本用于当标题"):
    return Entry(path=path, tags=(), summary=summary, mtime=1.0)


def test_user_section_has_own_header_and_skips_already():
    entries = {"a.md": _e("a.md"), "b.md": _e("b.md")}
    text, rendered = render_user_pins_section(["a.md", "b.md", "gone.md"], entries, {"b.md"}, 10)
    assert USER_PIN_HEADER in text and PIN_HEADER not in text
    assert INJECTION_NOTICE.rstrip("\n") in text
    assert rendered == ["a.md"]


def test_user_section_without_notice():
    text, _ = render_user_pins_section(["a.md"], {"a.md": _e("a.md")}, set(), 10, with_notice=False)
    assert INJECTION_NOTICE.rstrip("\n") not in text


def test_user_section_empty():
    assert render_user_pins_section([], {}, set(), 10) == ("", [])


def test_pin_header_text_is_unchanged():
    assert PIN_HEADER == ("📌 本会话固定上下文（LLM 首轮点名；"
                          "`relevance.session_pin_enabled: false` 可关）：")


def test_new_metrics_fields_are_registered():
    assert {"user_pinned", "user_pinned_injected"} <= _metrics._ANNOTATE_ALLOWED
    assert "user_pinned_injected" in am._NUMERIC_TOP_FIELDS
    assert am._LIST_TOP_FIELDS["user_pinned"] == (str,)
