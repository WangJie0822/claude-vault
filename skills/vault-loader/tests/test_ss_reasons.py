# -*- coding: utf-8 -*-
"""会话开始注入的入选原因：摘要清单与 mod 快照共用 note_reasons（spec 2026-10-12 §3.4）。"""
from __future__ import annotations

from scripts._frontmatter_reader import Entry
from scripts.session_start_load import build_summary_ss, note_reasons, ss_snapshot_item

RLO = chr(0x202E)                     # bidi 覆盖字符：运行时拼接，不写字面


def test_note_reasons():
    e = Entry(path="p/a.md", tags=("设计",))
    assert note_reasons(e, {"p/a.md"}, {"设计"}) == ["project_dir", "tag"]
    assert note_reasons(e, set(), {"设计"}) == ["tag"]
    assert note_reasons(e, {"p/a.md"}, set()) == ["project_dir"]
    assert note_reasons(e, set(), set()) == []


def test_summary_label_uses_first_reason():
    a = Entry(path="p/a.md", tags=("设计",))
    b = Entry(path="q/b.md", tags=("设计",))
    out = build_summary_ss([a, b], [], [], {"p/a.md"}, {"设计"}, "x", {"verbosity": "full"})
    assert "a  [项目目录]" in out and "b  [标签]" in out


def test_snapshot_item_sanitizes_and_caps_tags():
    """快照里的命中标签逐个净化、截断、最多 5 个（full-review T-M5：去掉净化此前不红）。"""
    tags = tuple(f"t{i}" for i in range(7)) + ("设" + RLO + "计",)
    e = Entry(path="q/b.md", tags=tags)
    item = ss_snapshot_item(e, set(), set(tags), include_tag=True)
    assert item["why"] == ["tag"]
    assert len(item["tags"]) == 5 and all(RLO not in t for t in item["tags"])
    item = ss_snapshot_item(Entry(path="q/b.md", tags=("设" + RLO + "计",)), set(),
                            {"设" + RLO + "计"}, include_tag=True)
    assert item["tags"] == ["设计"]


def test_snapshot_item_drops_tag_reason_when_tag_matching_is_off():
    """include_tag_matched_notes 关时，入选原因里不出现「标签」（full-review T-L7 / PM5）。"""
    e = Entry(path="p/a.md", tags=("设计",))
    assert ss_snapshot_item(e, {"p/a.md"}, {"设计"}, include_tag=False)["why"] == ["project_dir"]
    assert ss_snapshot_item(e, {"p/a.md"}, {"设计"}, include_tag=True)["why"] == ["project_dir", "tag"]
