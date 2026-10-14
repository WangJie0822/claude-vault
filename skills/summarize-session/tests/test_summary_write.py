"""单字段精确写入、路径白名单、cache 同步（spec §3.3–§3.4）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import _frontmatter_field as F
import _summary as S
import _summary_store as ST
import rebuild_index as R

V = '摘要内容足够二十个字并且说清楚了解决的问题与结论'


def test_set_scalar_field_preserves_crlf_and_other_bytes():
    text = "---\r\ncategory: 技术笔记\r\nsummary: 旧\r\ntags: [x]\r\n---\r\n# 正文\r\n"
    out = F.set_scalar_field(text, "summary", V)
    assert out == f'---\r\ncategory: 技术笔记\r\ntags: [x]\r\nsummary: "{V}"\r\n---\r\n# 正文\r\n'


def test_set_scalar_field_lf_and_insert_when_absent():
    text = "---\ncategory: a\n---\nbody\n"
    assert F.set_scalar_field(text, "summary", V) == f'---\ncategory: a\nsummary: "{V}"\n---\nbody\n'


def test_set_scalar_field_removes_block_scalar_continuation():
    text = "---\nsummary: >-\n  第一行\n  第二行\ncategory: 项目笔记\n---\nbody\n"
    out = F.set_scalar_field(text, "summary", V)
    assert out == f'---\ncategory: 项目笔记\nsummary: "{V}"\n---\nbody\n'


def test_set_scalar_field_none_deletes_key():
    text = '---\nsummary: "x"\ncategory: a\n---\nbody'
    assert F.set_scalar_field(text, "summary", None) == "---\ncategory: a\n---\nbody"


@pytest.mark.parametrize("text", ["\ufeff---\na: b\n---\n", "no frontmatter\n", "---\na: b\nnever closed\n"])
def test_set_scalar_field_skips_bom_missing_unclosed(text):
    assert F.set_scalar_field(text, "summary", V) is None


@pytest.mark.parametrize("bad", ['含"引号', "含\\反斜杠", "含\n换行"])
def test_set_scalar_field_rejects_forbidden_chars(bad):
    with pytest.raises(ValueError):
        F.set_scalar_field("---\na: b\n---\n", "summary", bad)


def test_get_field_lines_returns_key_and_continuation():
    text = "---\nsummary: >-\n  一\n  二\ntags: [x]\n---\n"
    assert F.get_field_lines(text, "summary") == ["summary: >-", "  一", "  二"]
    assert F.get_field_lines(text, "missing") == []
    assert F.get_field_lines("plain", "summary") is None


def _after_frontmatter(t: str) -> str:
    lines = t.split("\n")
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return "\n".join(lines[i + 1:])
    raise AssertionError("no closing line")


@pytest.mark.parametrize("text", [
    "---\ncategory: a\n--- \n正文第一段\n\n---\n\n正文第二段\n",               # 闭合行带尾随空白 + 正文水平线
    "---\r\ncategory: a\r\n---  \r\n正文\r\n---\r\n尾\r\n",                   # CRLF + 尾随空白
    '---\nnotes: |\n  ---\n  缩进的分隔线属于块标量\nsummary: "旧"\n---\n正文\n',  # 块标量里缩进的 ---
    "---\nsummary: >-\n  第一段\n\n  第二段\ncategory: x\n---\nbody\n",          # 块标量内有空行
])
def test_write_then_read_roundtrip_agrees_on_boundaries(text):
    """写端与读端（rebuild_index.parse_frontmatter）对 frontmatter 边界的判据必须一致。"""
    out = F.set_scalar_field(text, "summary", V)
    assert out is not None
    assert R.parse_frontmatter(out).get("summary") == V
    assert _after_frontmatter(out) == _after_frontmatter(text)      # 正文逐字不变
    assert "第二段" not in out.split("\n---")[0]                      # 块标量续行不残留在 frontmatter


def test_reader_without_pyyaml_parses_block_scalars(monkeypatch):
    """PyYAML 是可选依赖：没有它时块标量摘要不得被读成 '>-' 而判为过短、再被补全覆盖。"""
    monkeypatch.setattr(R, "_HAS_YAML", False)
    fm = R.parse_frontmatter("---\nsummary: >-\n  第一行内容足够长的块标量摘要\n  说明了问题与结论\ncategory: x\n---\n")
    assert fm["summary"] == "第一行内容足够长的块标量摘要 说明了问题与结论"
    assert fm["category"] == "x"
    assert R.parse_frontmatter("---\nsummary: |-\n  一\n  二\n---\n")["summary"] == "一\n二"
    assert S.classify("技术笔记/a.md", {"summary": fm["summary"], "summary_src": "fm"}) == "ok"


def test_current_summary_without_pyyaml_reads_block_scalar(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_HAS_YAML", False)
    p = tmp_path / "a.md"
    p.write_text("---\nsummary: >-\n  一段足够长的块标量摘要内容\n  说明了问题与结论\n---\n正文\n", encoding="utf-8")
    assert ST.current_summary(p) == "一段足够长的块标量摘要内容 说明了问题与结论"


def _vault(tmp_path):
    v = tmp_path / "V"
    (v / "技术笔记").mkdir(parents=True)
    (v / ".meta").mkdir()
    return v


def test_safe_note_path_whitelist(tmp_path):
    v = _vault(tmp_path)
    (v / "技术笔记" / "a.md").write_text("---\na: b\n---\n", encoding="utf-8")
    (v / "VERSION").write_text("x", encoding="utf-8")
    entries = {"技术笔记/a.md": {"category": "技术笔记"}, "VERSION": {}, ".meta/x.md": {},
               "工作日志/w.md": {}, "技术笔记/c:d.md": {}}
    assert ST.safe_note_path(v, "技术笔记/a.md", entries) == (v / "技术笔记" / "a.md").resolve()
    assert ST.safe_note_path(v, "技术笔记/not-in-cache.md", entries) is None
    assert ST.safe_note_path(v, "VERSION", entries) is None
    assert ST.safe_note_path(v, ".meta/x.md", entries) is None
    assert ST.safe_note_path(v, "工作日志/w.md", entries) is None
    assert ST.safe_note_path(v, "技术笔记/c:d.md", entries) is None
    assert ST.safe_note_path(v, "../escape.md", {"../escape.md": {}}) is None


def _dir_link(link: Path, target: Path) -> None:
    """目录链接：Windows 用 junction（不需管理员；Path.is_symlink() 对它返回 False），POSIX 用 symlink。"""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_safe_note_path_rejects_target_outside_vault_via_dir_link(tmp_path):
    """末段不是链接、中间目录链到 vault 外：is_symlink 管不到，只能靠「resolve 后在 vault 内」拦（T-M8）。"""
    v = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.md").write_text("---\na: b\n---\n", encoding="utf-8")
    _dir_link(v / "链接目录", outside)
    rel = "链接目录/a.md"
    assert ST.safe_note_path(v, rel, {rel: {"category": "x"}}) is None


def test_write_with_backup_syncs_cache_and_records_backup(tmp_path):
    """备份 → 写入 → 同步 cache 的唯一实现：三个写入点都经它，cache 同步只需在这里钉住（T-M5 / A15）。"""
    v = _vault(tmp_path)
    rel = "技术笔记/a.md"
    p = v / rel
    p.write_text('---\ncategory: 技术笔记\nsummary: "旧"\n---\n正文\n', encoding="utf-8")
    cp = v / ".meta" / "frontmatter-cache.json"
    cp.write_text(json.dumps({"_version": 1, "entries": {rel: {"summary": "旧", "summary_src": "fm"}}},
                             ensure_ascii=False), encoding="utf-8")
    bf = ST.new_backup_file(v)
    assert ST.write_with_backup(v, rel, p, "旧", V, bf) is True
    assert json.loads(cp.read_text(encoding="utf-8"))["entries"][rel]["summary"] == V
    rec = json.loads(bf.read_text(encoding="utf-8").splitlines()[-1])
    assert (rec["old"], rec["old_lines"], rec["new"]) == ("旧", ['summary: "旧"'], V)


def test_write_summary_exact_bytes_and_current_summary(tmp_path):
    v = _vault(tmp_path)
    p = v / "技术笔记" / "a.md"
    p.write_bytes("---\r\ncategory: 技术笔记\r\n---\r\n正文\r\n".encode("utf-8"))
    assert ST.write_summary(p, V) is True
    assert p.read_bytes() == f'---\r\ncategory: 技术笔记\r\nsummary: "{V}"\r\n---\r\n正文\r\n'.encode("utf-8")
    assert ST.current_summary(p) == V


def test_write_summary_on_block_scalar_note(tmp_path):
    v = _vault(tmp_path)
    p = v / "技术笔记" / "b.md"
    p.write_text("---\nsummary: >-\n  旧的多行\n  摘要\ncategory: x\n---\nbody\n", encoding="utf-8")
    assert isinstance(ST.current_summary(p), str)
    assert ST.write_summary(p, V) is True
    assert ST.current_summary(p) == V
    assert "旧的多行" not in p.read_text(encoding="utf-8")


def test_write_summary_permission_error_leaves_file_unchanged(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    p = v / "技术笔记" / "a.md"
    p.write_text("---\na: b\n---\n", encoding="utf-8")
    before = p.read_bytes()

    def boom(*a, **k):
        raise PermissionError(32, "being used by another process")
    monkeypatch.setattr(ST, "_replace_with_retry", boom)
    assert ST.write_summary(p, V) is False
    assert p.read_bytes() == before
    assert not list((v / "技术笔记").glob(".atomic_*"))


def test_write_summary_returns_false_for_no_frontmatter(tmp_path):
    v = _vault(tmp_path)
    p = v / "技术笔记" / "n.md"
    p.write_text("# 只有标题\n", encoding="utf-8")
    assert ST.write_summary(p, V) is False


def test_sync_cache_summary_updates_and_deletes(tmp_path):
    v = _vault(tmp_path)
    (v / "技术笔记" / "a.md").write_text("---\na: b\n---\n", encoding="utf-8")
    cp = v / ".meta" / "frontmatter-cache.json"
    cp.write_text(json.dumps({"_version": 1, "entries": {"技术笔记/a.md": {"summary": "旧", "summary_src": "h1"}}},
                             ensure_ascii=False), encoding="utf-8")
    assert ST.sync_cache_summary(v, "技术笔记/a.md", V) is True
    e = json.loads(cp.read_text(encoding="utf-8"))["entries"]["技术笔记/a.md"]
    assert e["summary"] == V and e["summary_src"] == "fm" and isinstance(e["mtime"], int)
    assert ST.sync_cache_summary(v, "技术笔记/a.md", None) is True
    e = json.loads(cp.read_text(encoding="utf-8"))["entries"]["技术笔记/a.md"]
    assert "summary" not in e and "summary_src" not in e
    assert not os.path.exists(str(cp) + ".lock")
    assert ST.sync_cache_summary(v, "技术笔记/none.md", V) is False
