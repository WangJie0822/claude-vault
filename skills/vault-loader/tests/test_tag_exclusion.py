# -*- coding: utf-8 -*-
"""tag 归一、排除判定与精排外发的 fail-closed（2.4.0 full-review S-H1）。

端到端（真实写端 → cache → 读端 → 外发 prompt）在仓库根 `tests/test_private_tag_outbound_e2e.py`；这里是
各环节的单元守卫，读端用例都经真实的 `load_cache_status` 从 cache 文件读入，不手工构造 Entry。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts._fine_input import build_fine_input, exclusion_set, tag_blocked
from scripts._frontmatter_reader import (MAX_TAG_CHARS, MAX_TAGS_PER_ENTRY, Entry,
                                         load_cache_status, normalize_tags)
from scripts._scorer import is_archived, tag_key


def _load(tmp_path: Path, tags_by_name: dict) -> dict:
    (tmp_path / ".meta").mkdir(exist_ok=True)
    entries = {f"笔记/{n}.md": {"tags": t, "summary": f"召回 {n}", "mtime": 1}
               for n, t in tags_by_name.items()}
    (tmp_path / ".meta" / "frontmatter-cache.json").write_text(
        json.dumps({"_version": 1, "entries": entries}, ensure_ascii=False), encoding="utf-8")
    got, _status = load_cache_status(tmp_path)
    assert len(got) == len(entries)
    return got


# ---------------- normalize_tags ----------------

@pytest.mark.parametrize("raw, tags, unknown", [
    pytest.param(None, (), False, id="none"),
    pytest.param("private", ("private",), False, id="str"),
    pytest.param("diary, private", ("diary", "private"), False, id="comma"),
    pytest.param("diary private\tx", ("diary", "private", "x"), False, id="whitespace"),
    pytest.param("#private, ##x ,, #", ("private", "x"), False, id="str-hash-and-empty"),
    pytest.param(["#private", " a ", "", "#"], ("private", "a"), False, id="list-hash-strip"),
    pytest.param(["my tag"], ("my tag",), False, id="list-item-not-split"),
    pytest.param(["a", 3], ("a",), True, id="list-non-str-item"),
    pytest.param({"private": True}, (), True, id="dict"),
    pytest.param(7, (), True, id="int"),
    pytest.param(True, (), True, id="bool"),
    pytest.param([], (), False, id="empty-list"),
])
def test_normalize_tags(raw, tags, unknown):
    assert normalize_tags(raw) == (tags, unknown)


def test_normalize_tags_caps_mark_unknown():
    """被长度或条数上限丢掉过 tag ⇒ 判不全；没丢就不标。"""
    long_tag = "private/" + "x" * MAX_TAG_CHARS
    tags, unknown = normalize_tags(["a", long_tag])
    assert tags == ("a",) and unknown is True
    many = [f"t{i}" for i in range(MAX_TAGS_PER_ENTRY)] + ["private"]
    tags, unknown = normalize_tags(many)
    assert len(tags) == MAX_TAGS_PER_ENTRY == 32 and "private" not in tags and unknown is True
    tags, unknown = normalize_tags(many[1:])                    # 恰好 32 条：不丢、不标
    assert "private" in tags and unknown is False


def test_normalize_before_cap():
    """归一在截断之前：先截断会让 32 个空项 / 纯 `#` 项占满窗口，把其后的 private 挤掉。"""
    tags, unknown = normalize_tags(["#", " ", ""] * 11 + ["private"])
    assert tags == ("private",) and unknown is False


def test_length_cap_judged_on_raw_item():
    """长度按原始项判：超长但大半是 `#` 的项照旧丢弃（并标判不全），不挤进条数窗口把第 32 个 tag 推出去。"""
    raw = ["#" * (MAX_TAG_CHARS + 1) + "x"] + [f"t{i}" for i in range(MAX_TAGS_PER_ENTRY - 1)] + ["archived"]
    tags, unknown = normalize_tags(raw)
    assert "x" not in tags and tags[-1] == "archived" and len(tags) == MAX_TAGS_PER_ENTRY
    assert unknown is True


def test_reader_normalizes_tags_from_cache(tmp_path):
    """经真实读端：`#` 前缀与标量写法在 Entry.tags 里已归一（打分侧看到的也是归一后的 tag）。"""
    got = _load(tmp_path, {"hash": ["#private", "#Work"], "scalar": "private", "comma": "a, #b",
                           "dict": {"private": 1}})
    assert got["笔记/hash.md"].tags == ("private", "Work")
    assert got["笔记/scalar.md"].tags == ("private",)
    assert got["笔记/comma.md"].tags == ("a", "b")
    assert got["笔记/dict.md"].tags == () and got["笔记/dict.md"].tags_unknown is True
    assert not any(got[f"笔记/{n}.md"].tags_unknown for n in ("hash", "scalar", "comma"))


def test_reader_marks_truncated_frontmatter_unknown(tmp_path):
    """写端读满上限仍没见到 frontmatter 闭合行时记 `fm_truncated`（2.4.0 full-review D-M1）：读端按 tag 判不全，
    精排两条外发通路都不送；键缺失或为 false 时与此前相同。tags 本身照常归一。"""
    (tmp_path / ".meta").mkdir()
    entries = {"笔记/cut.md": {"mtime": 1, "fm_truncated": True},
               "笔记/cut_tags.md": {"mtime": 1, "tags": ["work"], "fm_truncated": True},
               "笔记/false.md": {"mtime": 1, "tags": ["work"], "fm_truncated": False},
               "笔记/plain.md": {"mtime": 1, "tags": ["work"]}}
    (tmp_path / ".meta" / "frontmatter-cache.json").write_text(
        json.dumps({"_version": 1, "entries": entries}, ensure_ascii=False), encoding="utf-8")
    got, _status = load_cache_status(tmp_path)
    assert {p: e.tags_unknown for p, e in got.items()} == {
        "笔记/cut.md": True, "笔记/cut_tags.md": True, "笔记/false.md": False, "笔记/plain.md": False}
    assert got["笔记/cut_tags.md"].tags == ("work",)
    ex = exclusion_set(got, exclude_note_tags=["archived"], fine_exclude_tags=["private"], injected=[], excluded=[])
    assert ex == {"笔记/cut.md", "笔记/cut_tags.md"}


# ---------------- is_archived ----------------

def _e(*tags, unknown=False):
    return Entry(path="笔记/x.md", tags=tuple(tags), tags_unknown=unknown)


@pytest.mark.parametrize("tags, hit", [
    pytest.param(("private",), True, id="exact"),
    pytest.param(("Private",), True, id="case"),
    pytest.param(("#private",), True, id="hash-on-tag"),
    pytest.param(("private/journal",), True, id="nested"),
    pytest.param(("Private/Journal/2026",), True, id="nested-deep-case"),
    pytest.param(("#private/journal",), True, id="nested-hash"),
    pytest.param(("privatestuff",), False, id="prefix-without-slash"),
    pytest.param(("work/private",), False, id="private-as-child"),
    pytest.param(("work",), False, id="other"),
    pytest.param((), False, id="no-tags"),
])
def test_is_archived_case_hash_nested(tags, hit):
    assert is_archived(_e(*tags), {"private"}) is hit


def test_is_archived_exclude_side_normalized():
    """排除集合一侧同样按 `tag_key`：配成 `#Private` 也认；只含空串 / `#` 的集合等于空集合。"""
    assert is_archived(_e("private"), {"#Private"}) is True
    assert is_archived(_e("private/a"), ["#Private"]) is True
    assert is_archived(_e("private"), {"", "#"}) is False
    assert is_archived(_e("private"), set()) is False
    assert tag_key("  ##Private ") == "private"


def test_is_archived_ignores_tags_unknown():
    """粗排不读 `tags_unknown`：判不全只影响精排外发（`tag_blocked`），`is_archived` 行为不变。"""
    assert is_archived(_e(unknown=True), {"archived"}) is False


# ---------------- 精排外发的 fail-closed ----------------

def test_tag_blocked_fail_closed_only_when_exclusion_configured():
    assert tag_blocked(_e("work", unknown=True), {"private"}) is True
    assert tag_blocked(_e("work"), {"private"}) is False
    assert tag_blocked(_e("private"), {"private"}) is True
    assert tag_blocked(_e("work", unknown=True), set()) is False          # 没有排除 tag：无需判定


def _cfg(**over):
    c = {"top_n": 30, "project_top_k": 0, "pool_max": 50, "catalog": True, "catalog_max_chars": 80000,
         "fine_exclude_tags": ["private"], "exclude_note_tags": ["archived"]}
    c.update(over)
    return c


def test_unknown_tags_excluded_from_both_outbound_paths(tmp_path):
    """经真实读端：tag 判不全的笔记（dict tags、第 33 个之后才出现 private、超长嵌套 tag、list 里混非 str）
    不进候选，也不进「已在上下文」；对照组照常外发。"""
    filler = [f"t{i}" for i in range(MAX_TAGS_PER_ENTRY)]
    got = _load(tmp_path, {
        "dict_tags": {"private": True},
        "late_private": filler + ["private"],
        "long_nested": ["private/" + "x" * MAX_TAG_CHARS],
        "mixed_list": ["work", {"private": True}],
        "public": ["work"],
    })
    ex = exclusion_set(got, exclude_note_tags=["archived"], fine_exclude_tags=["private"], injected=[], excluded=[])
    assert ex == {"笔记/dict_tags.md", "笔记/late_private.md", "笔记/long_nested.md", "笔记/mixed_list.md"}
    fin = build_fine_input(got, words=["召回"], exclude=ex, project_rels=[], cfg=_cfg(), injected=[],
                           work_titles=[], k_texts=[], degraded=[])
    assert [c.name for c in fin.candidates] == ["public"]
    inj = sorted(got)
    fin2 = build_fine_input(got, words=["召回"], exclude=set(inj), project_rels=[], cfg=_cfg(), injected=inj,
                            work_titles=[], k_texts=[], degraded=[])
    assert fin2.in_context == ["public"]
