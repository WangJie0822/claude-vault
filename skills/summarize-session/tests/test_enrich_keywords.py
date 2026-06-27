from pathlib import Path
import sys
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import enrich_keywords as E


def test_sanitize_rejects_yaml_metachars_and_newline():
    raw = ["good", "ba:d", "x\ny", "ok词", "a]b"]
    assert E.sanitize_keywords(raw) == ["good", "ok词"]


def test_sanitize_length_limits():
    # CJK 单字拒、ASCII <3 拒、合规留
    assert E.sanitize_keywords(["回", "ab", "召回", "abcd"]) == ["召回", "abcd"]


def test_sanitize_caps_at_8():
    raw = [f"词条{i}" for i in range(20)]
    assert len(E.sanitize_keywords(raw)) == 8


def test_enrich_note_writes_keywords_yaml(tmp_path):
    note = tmp_path / "a.md"
    note.write_text("---\ntags: [t1]\nsummary: s\n---\n# 标题\n正文\n", encoding="utf-8")
    ok = E.enrich_note(note, '{"keywords": ["召回", "recall"]}')
    assert ok
    txt = note.read_text(encoding="utf-8")
    assert "keywords:" in txt and "召回" in txt and "recall" in txt
    assert "正文" in txt


def test_enrich_note_bad_json_leaves_file_untouched(tmp_path):
    note = tmp_path / "b.md"
    orig = "---\ntags: [t1]\n---\n# 标题\n"
    note.write_text(orig, encoding="utf-8")
    assert E.enrich_note(note, "not json") is False
    assert note.read_text(encoding="utf-8") == orig


def test_main_dry_run_does_not_write(tmp_path, monkeypatch):
    vault = tmp_path / "v"
    vault.mkdir()
    note = vault / "n.md"
    note.write_text("---\ntags: [t]\n---\n# x\n", encoding="utf-8")
    monkeypatch.setattr(E, "_call_claude", lambda content: '{"keywords": ["召回"]}')
    rc = E.main(["--vault", str(vault), "--dry-run"])
    assert rc == 0
    assert "keywords" not in note.read_text(encoding="utf-8")
