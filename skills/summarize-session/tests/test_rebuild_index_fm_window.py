"""frontmatter 读取窗口：读到闭合行为止，读满上限仍未闭合时记 `fm_truncated`（2.4.0 full-review D-M1）。

此前 `_read_entry` 只读文件前 2000 字符：frontmatter 的闭合行落在窗口外时整段判为未闭合，entry 没有 tags，
带 `private` 的笔记照样进精排外发（评审实测：`tags: [private]` 写在最前或最后都一样读不到）。现在先读 2000 字符，
frontmatter 已开而未闭合时继续读，直到闭合、文件结束或 `FM_SCAN_CHARS`；读满仍未闭合时 entry 记 `fm_truncated`，
读端（vault-loader `_frontmatter_reader`）据此把 `tags_unknown` 置真，精排外发 fail-closed。

上限在用例里写成字面量 65536，不从 `rebuild_index.FM_SCAN_CHARS` 反算（反算时改常量用例恒绿）。
端到端（真实写端 → 读端 → 外发 prompt）在仓库根 `tests/test_private_tag_outbound_e2e.py`。
"""
from __future__ import annotations

import pytest

import rebuild_index as R

CAP = 65536
LONG = "x" * 2100          # 一个字段就让 frontmatter 超过旧的 2000 字符窗口


def _write(tmp_path, text: str) -> str:
    (tmp_path / "笔记").mkdir(exist_ok=True)
    (tmp_path / "笔记" / "a.md").write_text(text, encoding="utf-8")
    return "笔记/a.md"


def test_cap_literal():
    assert R.FM_SCAN_CHARS == CAP


@pytest.mark.parametrize("front", [
    pytest.param(f"note: {LONG}\ntags: [private]\nsummary: 一段摘要\n", id="tags-last"),
    pytest.param(f"tags: [private]\nnote: {LONG}\nsummary: 一段摘要\n", id="tags-first"),
])
def test_long_frontmatter_tags_reach_cache(tmp_path, monkeypatch, front):
    """frontmatter 长于 2000 字符：tags 不论在前在后都读得到，不记截断。窗口与解析器无关，走回落解析器。"""
    monkeypatch.setattr(R, "_HAS_YAML", False)
    rel = _write(tmp_path, f"---\n{front}---\n# A\n正文\n")
    entry = R._read_entry(tmp_path, rel, 1)
    assert entry.get("tags") == ["private"]
    assert entry.get("summary") == "一段摘要"
    assert "fm_truncated" not in entry


def test_closing_line_just_inside_cap_is_read(tmp_path, monkeypatch):
    """闭合行恰在上限之内：读到它，不记截断。"""
    monkeypatch.setattr(R, "_HAS_YAML", False)
    head = "---\ntags: [private]\nnote: "
    filler = "y" * (CAP - len(head) - len("\n---") - 1)
    text = head + filler + "\n---" + "\n# A\n" + "正文" * 100
    assert text.index("\n---", 4) + len("\n---") <= CAP
    rel = _write(tmp_path, text)
    entry = R._read_entry(tmp_path, rel, 1)
    assert entry.get("tags") == ["private"] and "fm_truncated" not in entry


def test_unclosed_past_cap_is_marked(tmp_path, monkeypatch):
    """frontmatter 已开、读满上限仍没见到闭合行：记 `fm_truncated: True`，不产出 tags（窗口外的看不到）。"""
    monkeypatch.setattr(R, "_HAS_YAML", False)
    text = "---\ntags: [work]\n" + "".join(f"k{i}: {'v' * 60}\n" for i in range(2000)) + "---\n# A\n"
    assert len(text) > CAP
    rel = _write(tmp_path, text)
    entry = R._read_entry(tmp_path, rel, 1)
    assert entry.get("fm_truncated") is True
    assert "tags" not in entry and entry["_has_frontmatter"] is False


@pytest.mark.parametrize("text", [
    pytest.param("---\ntags: [private]\n" + "z" * 3000 + "\n", id="unclosed-to-eof"),
    pytest.param("# 标题\n" + "w" * (CAP + 10) + "\n", id="no-frontmatter-long"),
    pytest.param("﻿---\ntags: [x]\n" + "u" * (CAP + 10) + "\n", id="bom"),
])
def test_not_marked_when_whole_file_seen_or_no_frontmatter(tmp_path, monkeypatch, text):
    """读到文件结束仍没有闭合行（看到了全文，只是没有 frontmatter）、首行不是 `---`、以 BOM 开头：
    都不记截断，行为与此前相同。"""
    monkeypatch.setattr(R, "_HAS_YAML", False)
    rel = _write(tmp_path, text)
    entry = R._read_entry(tmp_path, rel, 1)
    assert "fm_truncated" not in entry


def test_rebuild_writes_marker_into_cache(tmp_path, monkeypatch):
    """经真实 `rebuild_index.main()`：标记随 entry 写进 cache 文件，读端拿得到。"""
    import json
    import sys
    monkeypatch.setattr(R, "_HAS_YAML", False)
    (tmp_path / ".meta").mkdir()
    rel = _write(tmp_path, "---\ntags: [work]\n" + "".join(f"k{i}: {'v' * 60}\n" for i in range(2000)))
    monkeypatch.setattr(sys, "argv", ["rebuild_index.py", "--vault", str(tmp_path), "--emit", "claude_md"])
    try:
        R.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    cache = json.loads((tmp_path / ".meta" / "frontmatter-cache.json").read_text(encoding="utf-8"))
    assert cache["entries"][rel].get("fm_truncated") is True
