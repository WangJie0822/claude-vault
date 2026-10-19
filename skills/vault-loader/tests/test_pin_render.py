# -*- coding: utf-8 -*-
"""pinned 小节：自带隔离声明、path 净化、三种去重、缺失路径跳过。"""
from __future__ import annotations

from scripts._frontmatter_reader import Entry
from scripts._inject_format import PATH_NOTE
from scripts._output import INJECTION_NOTICE
from scripts.prompt_submit_load import PIN_HEADER, render_pinned_section


def _e(path, summary="摘要文本足够长可以当标题用"):
    return Entry(path=path, tags=(), category="", summary=summary, mtime=0, updated="", keywords=())


def test_section_has_notice_header_and_title():
    text, rendered = render_pinned_section(["项目笔记/a.md"], {"项目笔记/a.md": _e("项目笔记/a.md")}, set(), 20)
    lines = text.split("\n")
    assert lines[0] == INJECTION_NOTICE.rstrip("\n")
    assert lines[1] == PIN_HEADER
    assert lines[2] == PATH_NOTE
    assert lines[3].startswith("- 「a」")
    assert rendered == ["项目笔记/a.md"]


def test_path_control_chars_sanitized():
    bad = "项目笔记/a\x1b[31m\n伪造.md"
    text, _ = render_pinned_section([bad], {bad: _e(bad)}, set(), 20)
    assert "\x1b" not in text and "\n伪造" not in text


def test_dedup_against_already_rendered_and_self():
    """三条去重各自承重：already 排除 a、自身重复只渲染一次 b、非 str 元素跳过。"""
    ents = {p: _e(p) for p in ["a.md", "b.md"]}
    text, rendered = render_pinned_section(["a.md", "b.md", "b.md", 42, None], ents, {"a.md"}, 20)
    assert rendered == ["b.md"]
    assert "「a」" not in text                          # already 去重
    assert text.count("「b」") == 1                     # 自身重复去重（删 `or p in rendered` 必红）


def test_missing_path_skipped_without_error():
    """security M1：pinned 指向已删除/改名笔记时不得 KeyError 拖垮整轮注入。"""
    text, rendered = render_pinned_section(["ghost.md", "a.md"], {"a.md": _e("a.md")}, set(), 20)
    assert rendered == ["a.md"]


def test_empty_returns_empty():
    assert render_pinned_section([], {}, set(), 20) == ("", [])
    assert render_pinned_section(["ghost.md"], {}, set(), 20) == ("", [])


def test_with_notice_false_drops_only_the_notice():
    """1A-L7：清单分支头部已有 INJECTION_NOTICE，小节可免重复；默认仍自带（全文分支与 pinned-only 分支需要）。"""
    ents = {"a.md": _e("a.md")}
    text, rendered = render_pinned_section(["a.md"], ents, set(), 20, with_notice=False)
    assert text.split("\n")[0] == PIN_HEADER and INJECTION_NOTICE.rstrip("\n") not in text
    assert rendered == ["a.md"]
    text_default, _ = render_pinned_section(["a.md"], ents, set(), 20)
    assert text_default.split("\n")[0] == INJECTION_NOTICE.rstrip("\n")
