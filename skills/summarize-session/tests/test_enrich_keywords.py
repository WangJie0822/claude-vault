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


# ---------------------------------------------------------------------------
# A3: sanitize 加固
# ---------------------------------------------------------------------------

def test_sanitize_rejects_comma():
    assert E.sanitize_keywords(["foo,bar", "good"]) == ["good"]


def test_sanitize_rejects_control_chars():
    assert E.sanitize_keywords(["a\rb", "c\td", "e\x00f", "ok词"]) == ["ok词"]


# ---------------------------------------------------------------------------
# A4: enrich 可用性
# ---------------------------------------------------------------------------

def test_extract_json_strips_fence():
    assert E._extract_json('```json\n{"keywords": ["召回"]}\n```') == '{"keywords": ["召回"]}'


def test_enrich_note_accepts_fenced_output(tmp_path):
    note = tmp_path / "a.md"
    note.write_text("---\ntags: [t]\n---\n# x\n", encoding="utf-8")
    assert E.enrich_note(note, '前缀文字\n```json\n{"keywords": ["召回", "recall"]}\n```') is True
    assert "召回" in note.read_text(encoding="utf-8")


def test_main_limit_counts_attempted_calls(tmp_path, monkeypatch):
    # --limit 应按「已发起调用」封顶：3 篇候选、claude 全失败、limit=2 → 只处理 2 篇
    import json as _j
    vault = tmp_path / "v"
    vault.mkdir()
    for i in range(3):
        (vault / f"n{i}.md").write_text("---\ntags: [t]\n---\n# x\n", encoding="utf-8")
    calls = {"n": 0}
    def _fail(content):
        calls["n"] += 1
        return None  # 模拟 claude 失败
    monkeypatch.setattr(E, "_call_claude", _fail)
    rc = E.main(["--vault", str(vault), "--limit", "2"])
    assert rc == 0
    assert calls["n"] == 2   # 即便全失败，也不超过 limit 次付费调用
