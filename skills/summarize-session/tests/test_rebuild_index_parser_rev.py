"""写端解析器修订号：旧 cache 全量重解析一次，之后照常增量；其他写 cache 的出口不丢它（2.4.0 S-H1 升级路径）。

`rebuild_index` 按秒级 mtime 增量重读。2.4.0 之前的降级解析器（未装 PyYAML）不认块序列，那时建的 cache
里块序列写的 tags 是空的；文件不改，升级后的增量重建永远不会重读它，private 排除对这些旧笔记一直不生效。
所以 cache 顶层记一个写端解析器修订号 `_parser`（读端 vault-loader 不读它，`_version` 仍是 1）：缺失或
小于当前值时忽略 mtime、全量重解析一次，写回时带上当前值。

另外两个写 cache 的出口（`keywords_gap --set` 的 `_sync_cache_entry`、`_summary_store.sync_cache_summary`）
只改单条 entry、整份写回：它们必须原样保留 `_parser`——丢掉它会让下一次 rebuild 又全量重解析；也不得自己
补上它——那会把一份没重解析过的旧 cache 冒充成新的。

修订号在用例里写成字面量，不从 `rebuild_index.PARSER_REV` 反算（反算时改常量用例恒绿）。当前为 3
（D-M1：frontmatter 读到闭合行为止）；上一版 2 写的 cache 同样要全量重解析一次，此前被 2000 字符截断的
笔记才能读回 tags，`prev` 那一组钉住这一点。
"""
from __future__ import annotations

import json
import sys

import pytest

import _summary_store as ST
import keywords_gap as KG
import rebuild_index as R

REV = 3


def _vault(tmp_path):
    v = tmp_path / "V"
    (v / "笔记").mkdir(parents=True)
    (v / ".meta").mkdir()
    return v


def _note(v, rel, text):
    p = v / rel
    p.write_text(text, encoding="utf-8")
    return int(p.stat().st_mtime)


def _cache(v):
    return json.loads((v / ".meta" / "frontmatter-cache.json").read_text(encoding="utf-8"))


def _write_cache(v, data):
    (v / ".meta" / "frontmatter-cache.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _rebuild(v, monkeypatch, seen=None):
    """跑真实的 rebuild_index.main()，强制走降级解析器；seen 不为 None 时记下每次 `_read_entry` 的 rel。"""
    monkeypatch.setattr(R, "_HAS_YAML", False)
    if seen is not None:
        real = R._read_entry

        def spy(vault, rel, mtime):
            seen.append(rel)
            return real(vault, rel, mtime)

        monkeypatch.setattr(R, "_read_entry", spy)
    monkeypatch.setattr(sys, "argv", ["rebuild_index.py", "--vault", str(v), "--emit", "claude_md"])
    try:
        R.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    if seen is not None:
        monkeypatch.setattr(R, "_read_entry", real)


def _old_entry(mtime, **kw):
    return dict({"mtime": mtime, "summary": "一段摘要", "summary_src": "fm", "_has_frontmatter": True}, **kw)


def test_parser_rev_literal():
    assert getattr(R, "PARSER_REV", None) == REV


@pytest.mark.parametrize("rev", [None, 1, 2, 0, "3", True], ids=["absent", "lower", "prev", "zero", "str", "bool"])
def test_old_cache_is_fully_reparsed_once(tmp_path, monkeypatch, rev):
    """旧形态 cache（无修订号或更小、或类型不对）：mtime 未变的笔记也重读，块序列 tags 进 cache，写回当前修订号。"""
    v = _vault(tmp_path)
    ma = _note(v, "笔记/a.md", "---\ntags:\n  - private\nsummary: 一段摘要\n---\n# A\n")
    mb = _note(v, "笔记/b.md", "---\ntags: [work]\nsummary: 一段摘要\n---\n# B\n")
    old = {"_version": 1, "entries": {"笔记/a.md": _old_entry(ma),                  # 旧解析器丢了块序列 tags
                                      "笔记/b.md": _old_entry(mb, tags=["work"])}}
    if rev is not None:
        old["_parser"] = rev
    _write_cache(v, old)
    seen = []
    _rebuild(v, monkeypatch, seen)
    c = _cache(v)
    assert c["entries"]["笔记/a.md"].get("tags") == ["private"]
    assert sorted(seen) == ["笔记/a.md", "笔记/b.md"]
    assert c.get("_parser") == REV and c.get("_version") == 1


def test_rev2_cache_rereads_notes_cut_by_old_window(tmp_path, monkeypatch):
    """修订号 2 写的 cache：frontmatter 长于 2000 字符的笔记当时读不到 tags（闭合行在旧窗口外）。文件不改、
    mtime 不变，升级后的 rebuild 也要重读它、读回 private（D-M1）。"""
    v = _vault(tmp_path)
    m = _note(v, "笔记/long.md", "---\nnote: " + "x" * 2100 + "\ntags: [private]\nsummary: 一段摘要\n---\n# L\n")
    _write_cache(v, {"_version": 1, "_parser": 2,
                     "entries": {"笔记/long.md": {"mtime": m, "summary": "L", "summary_src": "h1",
                                                  "_has_frontmatter": False}}})
    _rebuild(v, monkeypatch)
    c = _cache(v)
    assert c["entries"]["笔记/long.md"].get("tags") == ["private"]
    assert c.get("_parser") == REV


def test_current_cache_stays_incremental(tmp_path, monkeypatch):
    """修订号已是当前值：mtime 未变的笔记不重读（cache 里的哨兵摘要原样保留），修订号照写。"""
    v = _vault(tmp_path)
    m = _note(v, "笔记/a.md", "---\ntags:\n  - private\nsummary: 一段摘要\n---\n# A\n")
    _write_cache(v, {"_version": 1, "_parser": REV,
                     "entries": {"笔记/a.md": _old_entry(m, tags=["private"], summary="CACHED")}})
    seen = []
    _rebuild(v, monkeypatch, seen)
    c = _cache(v)
    assert seen == []
    assert c["entries"]["笔记/a.md"]["summary"] == "CACHED"
    assert c.get("_parser") == REV


def test_sync_exits_keep_parser_rev_so_next_rebuild_is_incremental(tmp_path, monkeypatch):
    """rebuild 写入修订号之后，`--set`（keywords）与摘要同步两个出口写回 cache 仍带着它，下一次 rebuild 只增量。"""
    v = _vault(tmp_path)
    _note(v, "笔记/a.md", "---\ntags: [work]\nsummary: 一段足够长的摘要内容用于测试同步\n---\n# A\n正文\n")
    _rebuild(v, monkeypatch)
    assert _cache(v).get("_parser") == REV
    r = KG.set_keywords(v, "笔记/a.md", ["召回打分", "精排补充"])
    assert r["status"] == "ok" and r["cache_synced"] is True
    assert _cache(v).get("_parser") == REV
    assert ST.sync_cache_summary(v, "笔记/a.md", "同步进来的新摘要内容") is True
    c = _cache(v)
    assert c.get("_parser") == REV and c["entries"]["笔记/a.md"]["summary"] == "同步进来的新摘要内容"
    seen = []
    _rebuild(v, monkeypatch, seen)
    assert seen == []
    assert _cache(v).get("_parser") == REV


def test_sync_exits_do_not_stamp_an_old_cache(tmp_path, monkeypatch):
    """同步出口只保留、不补写：一份没重解析过的旧 cache 经它们写回后仍没有修订号，下一次 rebuild 照样全量。"""
    v = _vault(tmp_path)
    m = _note(v, "笔记/a.md", "---\ntags: [work]\nsummary: 一段足够长的摘要内容用于测试同步\n---\n# A\n正文\n")
    _write_cache(v, {"_version": 1, "entries": {"笔记/a.md": _old_entry(m, tags=["work"])}})
    assert KG.set_keywords(v, "笔记/a.md", ["召回打分", "精排补充"])["cache_synced"] is True
    assert ST.sync_cache_summary(v, "笔记/a.md", "同步进来的新摘要内容") is True
    assert "_parser" not in _cache(v)
