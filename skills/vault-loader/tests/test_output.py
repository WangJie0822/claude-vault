"""_output 单测：emit JSON 结构 / 静默 / 降级 / 清洗 / 字数。"""
from __future__ import annotations

import io
import json

import pytest

import unicodedata

from scripts._output import _CF_RE, emit, sanitize_for_display, approx_size_str, sanitize_injected_text


def test_sanitize_injected_strips_cf():
    """S39：注入正文剥 Unicode Cf（零宽、bidi、tag 字符、软连字符等），保留 tab 与换行。"""
    cf = [0x00AD, 0x061C, 0x200B, 0x200D, 0x202E, 0x2066, 0xFEFF, 0xE0001, 0xE0041]
    text = "a" + "".join(chr(c) for c in cf) + "中文" + chr(9) + "b" + chr(10) + "c"
    assert sanitize_injected_text(text) == "a中文" + chr(9) + "b" + chr(10) + "c"
    assert sanitize_injected_text(text, keep_newlines=False) == "a中文" + chr(9) + "b c"


def test_cf_ranges_cover_running_unicode():
    """`_CF_RE` 是按码点区间硬编码的（不在导入时扫全码表，那要约 110 万次 category 调用、吃 hook 预算）。
    拿运行时 unicodedata 对账：新版本 Unicode 增补的 Cf 漏了即红；反向只允许 Cf 或（旧版 Python 里尚未分配的）Cn。"""
    missing = [hex(c) for c in range(0x110000)
               if unicodedata.category(chr(c)) == "Cf" and not _CF_RE.match(chr(c))]
    assert not missing, f"_CF_RANGES 漏了这些 Cf：{missing[:20]}"
    extra = [hex(c) for c in range(0x110000)
             if _CF_RE.match(chr(c)) and unicodedata.category(chr(c)) not in ("Cf", "Cn")]
    assert not extra, f"_CF_RANGES 多了非 Cf 字符：{extra[:20]}"


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


def test_sanitize_strips_full_c1_range():
    """显示侧必须剥**完整** C1（\\x80-\\x9f），不能只剥 \\x9b。

    C1 是否可被终端解释取决于输出编码：stdout 为 UTF-8 时它们编码成 2 字节（首字节 0xc2）
    不构成向量；但 8-bit locale 下编码成裸单字节，0x9b=CSI / 0x9d=OSC / 0x90=DCS /
    0x9e=PM / 0x9f=APC 全是有效转义引导符。守卫按完整范围断言，杜绝退回「只剥 CSI」。
    """
    for cp in range(0x80, 0xA0):
        ch = chr(cp)
        assert ch not in sanitize_for_display(f"前{ch}后"), f"U+{cp:04X} 未被剥离"
    # 正常可见字符不受影响（边界两侧各取一个）
    assert sanitize_for_display("\x7e中文\xa0") == "\x7e中文\xa0"


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


def test_emit_is_single_shot(monkeypatch):
    """emit 只允许写出一个 JSON 文档；第二次调用必须被拦下。

    hook stdout 的契约是「一次进程执行产出一个 JSON 文档」。emit 是裸
    sys.stdout.write(json.dumps(...))，调用两次就是两段拼接 JSON——Claude Code 侧
    会因 JSON.parse 失败而把**整个原始 stdout**当 plainText 推进模型上下文，
    使 systemMessage 里未经注入侧净化的 vault 派生文本绕过隔离声明。
    """
    import io
    from scripts import _output

    buf = io.StringIO()
    monkeypatch.setattr(_output.sys, "stdout", buf)

    _output.emit(None, "第一条", "UserPromptSubmit")
    _output.emit("正文", "第二条", "UserPromptSubmit")     # 必须被拦

    raw = buf.getvalue()
    parsed = json.loads(raw)                               # 拦不住则 Extra data
    assert parsed == {"systemMessage": "第一条"}, f"第二次 emit 未被拦下：{raw!r}"


def test_emit_guard_resets_between_tests():
    """守卫可被显式重置——否则同进程内的后续用例会拿到空 stdout 的假失败。"""
    import io
    from scripts import _output

    _output.reset_emit_guard()
    buf = io.StringIO()
    old = _output.sys.stdout
    try:
        _output.sys.stdout = buf
        _output.emit(None, "重置后仍可写", "SessionStart")
    finally:
        _output.sys.stdout = old
    assert json.loads(buf.getvalue()) == {"systemMessage": "重置后仍可写"}
