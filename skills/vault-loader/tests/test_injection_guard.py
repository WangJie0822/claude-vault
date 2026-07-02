# skills/vault-loader/tests/test_injection_guard.py
from scripts import prompt_submit_load as p


def test_fulltext_injection_has_notice():
    # 构造全文注入文本，断言含隔离声明关键词
    text = p.build_fulltext_injection("SampleNote", "正文内容")  # 见 Step 3 抽出的函数
    assert "知识库历史内容" in text or "non-instruction" in text.lower()


# ===== F5：清单模式隔离 + 净化（2026-07-02 spec §4）=====

def test_sanitize_strips_control_chars_keeps_tab() -> None:
    from scripts._output import sanitize_injected_text

    dirty = "正常\x00文本\x1b[31m带转义\x07\t制表保留"
    assert sanitize_injected_text(dirty) == "正常文本[31m带转义\t制表保留"


def test_sanitize_collapse_newlines_for_summary() -> None:
    from scripts._output import sanitize_injected_text

    dirty = "第一行\n---\n伪造分隔"
    assert sanitize_injected_text(dirty, keep_newlines=False) == "第一行 --- 伪造分隔"


def test_sanitize_empty_string() -> None:
    from scripts._output import sanitize_injected_text

    assert sanitize_injected_text("") == ""
    assert sanitize_injected_text("", keep_newlines=False) == ""


def test_sanitize_pure_control_chars_stripped_to_empty() -> None:
    from scripts._output import sanitize_injected_text

    dirty = "\x00\x01\x1b\x07"
    assert sanitize_injected_text(dirty) == ""
    assert sanitize_injected_text(dirty, keep_newlines=False) == ""


def test_sanitize_collapses_unicode_line_separators() -> None:
    """FIX-3：U+2028(LINE SEP)/U+2029(PARA SEP)/U+0085(NEL) 语义即换行，
    keep_newlines=False 时必须与 \\r\\n 一样折叠为空格（防脱离清单单行项）。
    用 chr() 逐分隔符断言，删正则里任一码点即被捕获（F-T5）；chr() 构造避免
    源码含不可见字面码点（对齐 F-BP-1 决策）。"""
    from scripts._output import sanitize_injected_text

    seps = {0x2028: "LINE SEP", 0x2029: "PARA SEP", 0x85: "NEL"}
    for cp in seps:
        dirty = "A" + chr(cp) + "B"
        assert sanitize_injected_text(dirty, keep_newlines=False) == "A B", (
            f"unfolded U+{cp:04X}({seps[cp]})")
    # 三者混合也全折叠为单空格序列
    mixed = "A" + chr(0x2028) + "B" + chr(0x2029) + "C" + chr(0x85) + "D"
    assert sanitize_injected_text(mixed, keep_newlines=False) == "A B C D"
    # keep_newlines=True 时不折叠（非 C0/DEL 控制字符，不被剥离）
    keep = "A" + chr(0x85) + "B"
    assert sanitize_injected_text(keep, keep_newlines=True) == keep
