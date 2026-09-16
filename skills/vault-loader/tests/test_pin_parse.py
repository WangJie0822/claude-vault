# -*- coding: utf-8 -*-
"""双段输出解析：PINNED 段与 WORDS 段各自独立，任一段坏只让该段为空。"""
from __future__ import annotations

import pytest

from scripts._topic import (_clean_words, build_topic_prompt,
                            parse_pin_and_words)


def test_normal_two_sections():
    idx, words = parse_pin_and_words("PINNED: 3,17\nWORDS: 精排,排序效果,主题词", 30, 3)
    assert idx == [2, 16]
    assert words == ["精排", "排序效果", "主题词"]


def test_pin_numbers_never_leak_into_words():
    """security H2：整份喂 parse_topic_words 会把 'PINNED:'/'3'/'17' 当词，且 '3' 走词边界匹配命中 9% 笔记。"""
    _, words = parse_pin_and_words("PINNED: 3,17\nWORDS: 精排", 30, 3)
    assert "PINNED:" not in words and "PINNED" not in words
    assert "3" not in words and "17" not in words
    assert "WORDS:" not in words


def test_only_words_section():
    idx, words = parse_pin_and_words("WORDS: 精排,排序效果", 30, 3)
    assert idx == [] and words == ["精排", "排序效果"]


def test_only_pinned_section():
    idx, words = parse_pin_and_words("PINNED: 5", 30, 3)
    assert idx == [4] and words == []


def test_none_keyword_means_empty_pin():
    idx, _ = parse_pin_and_words("PINNED: NONE\nWORDS: 精排", 30, 3)
    assert idx == []


@pytest.mark.parametrize("seg,expected", [
    ("0", []),            # 0 越下界；朴素 pool[n-1] 会回绕到池尾（security M4）
    ("-1", []),           # 负数：实现的正则带负向断言 (?<![-\d]) 拒掉带负号的数
    ("31", []),           # 越上界（pool_size=30）
    ("3,3,3", [2]),       # 重复只留一个
    ("1,2,3,4,5", [0]),   # 超过 pin_max=1 截断
    ("abc", []),          # 非数字
    ("9" * 40, []),       # 超长数字串
])
def test_pin_index_boundaries(seg, expected):
    idx, _ = parse_pin_and_words(f"PINNED: {seg}\nWORDS: x", 30, 1)
    assert idx == expected


def test_garbage_input_is_safe():
    assert parse_pin_and_words(None, 30, 1) == ([], [])
    assert parse_pin_and_words("", 30, 1) == ([], [])
    assert parse_pin_and_words("完全不按格式\n乱七八糟", 30, 1)[0] == []


def test_clean_words_drops_pure_digits_and_single_ascii():
    assert _clean_words(["3", "17", "a", "精排", "ok"]) == ["精排", "ok"]


def test_prompt_has_numbered_candidates_and_pin_max():
    text = build_topic_prompt("问题", [("项目笔记/a.md", "摘要A"), ("项目笔记/b.md", "摘要B")], pin_max=2)
    assert "1 - 项目笔记/a.md：«摘要A»" in text
    assert "2 - 项目笔记/b.md：«摘要B»" in text
    assert "最多 2 个" in text
    assert "PINNED:" in text and "WORDS:" in text
    assert "仅供理解话题范围" not in text     # v1 那句关掉了 LLM 的排序能力


@pytest.mark.parametrize("raw, idx, words", [
    ("**PINNED:** 3\n**WORDS:** 精排,主题词", [2], ["精排", "主题词"]),        # markdown 加粗标签
    ("**PINNED**: 3\n**WORDS**: 精排", [2], ["精排"]),
    ("- PINNED: 3\n- WORDS: 精排", [2], ["精排"]),                             # 列表项装饰
    ("# PINNED: 3\n## WORDS: 精排", [2], ["精排"]),
    ("PINNED: 3 WORDS: 精排,主题词", [2], ["精排", "主题词"]),                  # 同行
    ("PINNED: 3, 5 WORDS: 精排", [2, 4], ["精排"]),
    ("PINNED: 3 (none of the rest)\nWORDS: 精排", [2], ["精排"]),             # none 作附注不误杀
    ("PINNED: NONE\nWORDS: 精排", [], ["精排"]),
    ("- **PINNED:** none.\n- **WORDS:** 精排", [], ["精排"]),
    ("PINNED: (none)\nWORDS: 精排", [], ["精排"]),
    ("1. PINNED: 3\n2. WORDS: 精排", [2], ["精排"]),                       # 有序列表前缀（A-M2）
    ("1) **PINNED:** 3\n2) **WORDS:** 精排", [2], ["精排"]),
])
def test_markdown_decorations_and_same_line_shapes(raw, idx, words):
    """终审 M-6 三种退化：① 加粗/列表/标题装饰 ② 同行 ③ 行内 none 附注——修复前 ①② pinned 空、
    标签串还占词槽（`'**PINNED:**'`/`'**WORDS:**'` 进 words），③ 整段被判 NONE。"""
    got_idx, got_words = parse_pin_and_words(raw, 30, 3)
    assert got_idx == idx
    assert got_words == words
    assert not any("PINNED" in w.upper() or "WORDS" in w.upper() for w in got_words)


def test_decorated_label_never_matches_mid_line_prose():
    """放宽的只是标签外壳：行首装饰字符集不含字母，正文里出现 PINNED 一词不当标签。"""
    idx, words = parse_pin_and_words("我觉得 PINNED: 3 不对\nWORDS: 精排", 30, 3)
    assert idx == []
    assert "精排" in words


def test_label_tokens_never_become_topic_words():
    """第二道防线（A-M2）：解析器没剥干净的标签串在 `_clean_words` 处被丢，不依赖正则枚举到每种装饰。"""
    assert _clean_words(["PINNED:", "**WORDS**", "words：", "精排", "pinned"]) == ["精排"]


def test_words_first_same_line_is_a_documented_limitation():
    """同一行 WORDS 在前、PINNED 在后：不识别 PINNED（docstring 已写明），但标签串不得漏进词表，
    残留的编号 `3` 由写端 `_clean_words` 丢弃（A-M2 的第二道防线）。"""
    idx, words = parse_pin_and_words("WORDS: 精排 PINNED: 3", 30, 3)
    assert idx == []
    assert not any("PINNED" in w.upper() or "WORDS" in w.upper() for w in words)
    assert _clean_words(words) == ["精排"]
