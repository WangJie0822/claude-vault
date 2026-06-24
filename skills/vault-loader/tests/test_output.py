"""_output 单测：emit JSON 结构 / 静默 / 降级 / 清洗 / 字数。"""
from __future__ import annotations

import io
import json

import pytest

from scripts._output import emit, sanitize_for_display, approx_size_str


def _capture(monkeypatch, additional, sysmsg, event="SessionStart"):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    emit(additional, sysmsg, event)
    return buf.getvalue()


def test_emit_both_fields(monkeypatch):
    out = _capture(monkeypatch, "CTX内容", "用户摘要")
    d = json.loads(out)
    assert d["systemMessage"] == "用户摘要"
    assert d["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert d["hookSpecificOutput"]["additionalContext"] == "CTX内容"


def test_emit_only_system_message(monkeypatch):
    d = json.loads(_capture(monkeypatch, None, "仅摘要"))
    assert d["systemMessage"] == "仅摘要"
    assert "hookSpecificOutput" not in d


def test_emit_only_additional_context(monkeypatch):
    d = json.loads(_capture(monkeypatch, "仅上下文", None))
    assert d["hookSpecificOutput"]["additionalContext"] == "仅上下文"
    assert "systemMessage" not in d


def test_emit_both_empty_is_silent(monkeypatch):
    assert _capture(monkeypatch, None, None) == ""
    assert _capture(monkeypatch, "", "") == ""


def test_emit_preserves_special_chars_verbatim(monkeypatch):
    raw = '正文含 ``` 代码 "引号" \\反斜杠 emoji😀 换行\n第二行'
    d = json.loads(_capture(monkeypatch, raw, None))
    assert d["hookSpecificOutput"]["additionalContext"] == raw


def test_sanitize_strips_terminal_escapes():
    cleaned = sanitize_for_display("标题\x1b]0;X\x07正常\x1b[31m红")
    assert "\x1b" not in cleaned and "\x07" not in cleaned
    assert "标题" in cleaned and "正常" in cleaned and "红" in cleaned


def test_sanitize_keeps_tab_newline():
    assert sanitize_for_display("a\tb\nc") == "a\tb\nc"


def test_approx_size():
    assert approx_size_str("x" * 420) == "~420 字"
    assert approx_size_str("x" * 3200) == "~3.2k 字"


def test_emit_sanitizes_system_message(monkeypatch):
    import io, json, sys
    buf = io.StringIO(); monkeypatch.setattr("sys.stdout", buf)
    emit("ctx 含\x1b[31m不清洗", "用户摘要\x1b]0;X\x07注入", "SessionStart")
    d = json.loads(buf.getvalue())
    assert "\x1b" not in d["systemMessage"] and "\x07" not in d["systemMessage"]  # 兜底清洗
    assert "\x1b" in d["hookSpecificOutput"]["additionalContext"]                  # additionalContext 逐字保留
