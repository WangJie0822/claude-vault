"""_frontmatter_reader 单测。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts._frontmatter_reader import Entry, load_cache


def test_load_normal(tmp_vault: Path, write_frontmatter_cache) -> None:
    write_frontmatter_cache({
        "技术笔记/foo.md": {
            "tags": ["android", "测试"],
            "category": "技术笔记",
            "summary": "示例",
            "mtime": 1700000000,
            "updated": "2026-04-01",
        }
    })

    entries = load_cache(tmp_vault)

    assert "技术笔记/foo.md" in entries
    e = entries["技术笔记/foo.md"]
    assert isinstance(e, Entry)
    assert e.tags == ("android", "测试")
    assert e.category == "技术笔记"
    assert e.summary == "示例"
    assert e.mtime == 1700000000
    assert e.path == "技术笔记/foo.md"


def test_missing_cache_returns_empty(tmp_vault: Path) -> None:
    entries = load_cache(tmp_vault)
    assert entries == {}


def test_corrupted_json_returns_empty(tmp_vault: Path) -> None:
    (tmp_vault / ".meta" / "frontmatter-cache.json").write_text("{ broken")
    entries = load_cache(tmp_vault)
    assert entries == {}


def test_missing_optional_fields_use_defaults(tmp_vault: Path, write_frontmatter_cache) -> None:
    write_frontmatter_cache({
        "foo.md": {}  # 全部字段缺失
    })

    entries = load_cache(tmp_vault)

    e = entries["foo.md"]
    assert e.tags == ()
    assert e.category == ""
    assert e.summary == ""
    assert e.mtime == 0


def test_huge_cache_rejected(tmp_vault: Path) -> None:
    """> 10 MB 应拒绝加载。"""
    cache_path = tmp_vault / ".meta" / "frontmatter-cache.json"
    cache_path.write_bytes(b"x" * (11 * 1024 * 1024))

    entries = load_cache(tmp_vault)

    assert entries == {}


def test_version_mismatch_returns_empty(tmp_path):
    from scripts._frontmatter_reader import load_cache
    meta = tmp_path / ".meta"
    meta.mkdir(parents=True)
    (meta / "frontmatter-cache.json").write_text(
        '{"_version": 999, "entries": {"a.md": {"tags": ["x"]}}}', encoding="utf-8")
    assert load_cache(tmp_path) == {}


def test_correct_version_loads(tmp_path):
    from scripts._frontmatter_reader import load_cache, CACHE_VERSION
    meta = tmp_path / ".meta"
    meta.mkdir(parents=True)
    (meta / "frontmatter-cache.json").write_text(
        '{"_version": %d, "entries": {"a.md": {"tags": ["x"]}}}' % CACHE_VERSION, encoding="utf-8")
    assert "a.md" in load_cache(tmp_path)


def test_load_cache_reads_keywords(write_frontmatter_cache, tmp_vault):
    from scripts._frontmatter_reader import load_cache
    write_frontmatter_cache({
        "a.md": {"tags": ["t1"], "summary": "s", "keywords": ["同义词", "alias"]},
    })
    entries = load_cache(tmp_vault)
    assert entries["a.md"].keywords == ("同义词", "alias")


def test_load_cache_keywords_absent_defaults_empty(write_frontmatter_cache, tmp_vault):
    from scripts._frontmatter_reader import load_cache
    write_frontmatter_cache({"a.md": {"tags": ["t1"], "summary": "s"}})
    assert load_cache(tmp_vault)["a.md"].keywords == ()


def test_load_cache_keywords_scalar_not_char_iterated(write_frontmatter_cache, tmp_vault):
    # keywords 写成标量字符串（非数组）不得被逐字符迭代成 ('f','o','o')
    from scripts._frontmatter_reader import load_cache
    write_frontmatter_cache({"a.md": {"keywords": "foo"}})
    assert load_cache(tmp_vault)["a.md"].keywords == ()


def test_load_cache_keywords_drops_non_str(write_frontmatter_cache, tmp_vault):
    from scripts._frontmatter_reader import load_cache
    write_frontmatter_cache({"a.md": {"keywords": ["ok", 123, None, "good"]}})
    assert load_cache(tmp_vault)["a.md"].keywords == ("ok", "good")


def test_load_cache_caps_keyword_count(write_frontmatter_cache, tmp_vault):
    # B2：读端每篇 keyword 条数上限（纵深防异常膨胀 cache）
    from scripts._frontmatter_reader import load_cache, MAX_KEYWORDS_PER_ENTRY
    write_frontmatter_cache({"a.md": {"keywords": [f"词条{i}" for i in range(50)]}})
    assert len(load_cache(tmp_vault)["a.md"].keywords) == MAX_KEYWORDS_PER_ENTRY


def test_tags_capped_at_max_count(tmp_path) -> None:
    """F6 对称护栏：tags 条数超过 32 条时按 MAX_TAGS_PER_ENTRY 截断（与 keywords 上限对称）。"""
    import json
    from scripts._frontmatter_reader import load_cache, MAX_TAGS_PER_ENTRY

    meta = tmp_path / ".meta"; meta.mkdir()
    entry = {"tags": [f"t{i}" for i in range(50)], "summary": "s", "mtime": 1}
    (meta / "frontmatter-cache.json").write_text(
        json.dumps({"_version": 1, "entries": {"a.md": entry}}), encoding="utf-8")
    entries = load_cache(tmp_path)
    tags = entries["a.md"].tags
    assert len(tags) == MAX_TAGS_PER_ENTRY == 32


def test_overlong_tag_dropped_within_cap_window(tmp_path) -> None:
    """F6 对称护栏：单条 tag 超过 128 字符按 MAX_TAG_CHARS 过滤丢弃。

    超长 tag 必须落在 [:32] 条数截断窗口**以内**，否则本测试对长度过滤零区分力——
    条数截断本身就会把窗口外的元素排除，无法证明长度过滤是否真的生效
    （历史教训：曾把超长 tag 放在第 51 位，条数截断先天排除它，删掉长度过滤条件测试仍 PASS）。
    """
    import json
    from scripts._frontmatter_reader import load_cache

    meta = tmp_path / ".meta"; meta.mkdir()
    overlong = "x" * 200
    tags_raw = [f"t{i}" for i in range(10)] + [overlong] + [f"t{i}" for i in range(10, 31)]
    assert len(tags_raw) == 32  # 超长 tag（索引 10）落在 32 条截断窗口以内
    entry = {"tags": tags_raw, "summary": "s", "mtime": 1}
    (meta / "frontmatter-cache.json").write_text(
        json.dumps({"_version": 1, "entries": {"a.md": entry}}), encoding="utf-8")
    entries = load_cache(tmp_path)
    tags = entries["a.md"].tags

    assert overlong not in tags
    assert all(len(t) <= 128 for t in tags)
    # 长度过滤生效后超长 tag 被剔除，剩余 31 条有效 tag 全部保留（未触发条数截断）
    assert len(tags) == 31
    assert tags == tuple(f"t{i}" for i in range(31))
