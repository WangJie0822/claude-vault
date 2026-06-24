from __future__ import annotations
from pathlib import Path
import json
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from apply_drafts import apply_all, ApplyResult


def _draft(dir_: Path, name: str, target: Path, body: str, type_: str = "claude_md_global"):
    f = dir_ / name
    f.write_text(
        f"---\ntarget_path: {target}\nsource_session: s\n"
        f"source_date: 2026-04-23\ntype: {type_}\n---\n\n{body}",
        encoding="utf-8")
    return f


def test_apply_appends_to_existing_target(tmp_path: Path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# Original\n\nrule 1\n", encoding="utf-8")
    drafts_root = tmp_path / "auto-drafts" / "2026-04-23"
    drafts_root.mkdir(parents=True)
    _draft(drafts_root, "x.draft.md", target, "## New rule\n\nrule 2\n")

    result = apply_all(drafts_root.parent)
    assert result.applied == 1
    assert result.failed == 0
    assert "rule 1" in target.read_text()
    assert "rule 2" in target.read_text()
    # 草稿应被删除
    assert not (drafts_root / "x.draft.md").exists()


def test_apply_creates_target_if_missing(tmp_path: Path):
    target = tmp_path / "subdir" / "new-target.md"
    drafts_root = tmp_path / "auto-drafts" / "2026-04-23"
    drafts_root.mkdir(parents=True)
    _draft(drafts_root, "x.draft.md", target, "## brand new\n")

    result = apply_all(drafts_root.parent)
    assert result.applied == 1
    assert target.exists()
    assert "brand new" in target.read_text()


def test_apply_cleans_empty_date_dir(tmp_path: Path):
    target = tmp_path / "t.md"
    target.write_text("a")
    drafts_root = tmp_path / "auto-drafts" / "2026-04-23"
    drafts_root.mkdir(parents=True)
    _draft(drafts_root, "x.draft.md", target, "b")

    apply_all(drafts_root.parent)
    # 日期目录应被清理
    assert not drafts_root.exists()


def test_apply_keeps_failed_draft_and_continues(tmp_path: Path, monkeypatch):
    target1 = tmp_path / "ok.md"
    target1.write_text("a")
    target2 = Path("/proc/no-such-place/forbidden")  # 写入会失败
    drafts_root = tmp_path / "auto-drafts" / "2026-04-23"
    drafts_root.mkdir(parents=True)
    ok = _draft(drafts_root, "ok.draft.md", target1, "ok body")
    bad = _draft(drafts_root, "bad.draft.md", target2, "bad body")

    result = apply_all(drafts_root.parent)
    assert result.applied == 1
    assert result.failed == 1
    # 成功的删除,失败的保留
    assert not ok.exists()
    assert bad.exists()


def test_apply_empty_root_returns_zero(tmp_path: Path):
    drafts_root = tmp_path / "auto-drafts"
    drafts_root.mkdir()
    result = apply_all(drafts_root)
    assert result.applied == 0
    assert result.failed == 0
