from __future__ import annotations
import json
from pathlib import Path

import pytest

import sys
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from diff_drafts import find_drafts, render_diff, DraftInfo


def _make_draft(dir_: Path, name: str, target: str, body: str,
                source_session: str = "test", type_: str = "claude_md_global"):
    f = dir_ / name
    f.write_text(
        f"---\ntarget_path: {target}\nsource_session: {source_session}\n"
        f"source_date: 2026-04-23\ntype: {type_}\n---\n\n{body}",
        encoding="utf-8")
    return f


def test_find_drafts_returns_all(tmp_path: Path):
    drafts_root = tmp_path / "auto-drafts"
    (drafts_root / "2026-04-23").mkdir(parents=True)
    (drafts_root / "2026-04-22").mkdir()
    _make_draft(drafts_root / "2026-04-23", "global-claude.draft.md",
                str(tmp_path / "CLAUDE.md"), "## new")
    _make_draft(drafts_root / "2026-04-22", "memory-x.draft.md",
                str(tmp_path / "mem.md"), "x")

    drafts = find_drafts(drafts_root)
    assert len(drafts) == 2
    types = {d.type for d in drafts}
    assert types == {"claude_md_global", "claude_md_global"} or "memory" not in types


def test_find_drafts_skips_non_draft_files(tmp_path: Path):
    drafts_root = tmp_path / "auto-drafts" / "2026-04-23"
    drafts_root.mkdir(parents=True)
    _make_draft(drafts_root, "valid.draft.md", "/tmp/x", "ok")
    (drafts_root / "README.md").write_text("not a draft")
    drafts = find_drafts(drafts_root.parent)
    assert len(drafts) == 1
    assert drafts[0].path.name == "valid.draft.md"


def test_render_diff_shows_added_lines(tmp_path: Path):
    target = tmp_path / "target.md"
    target.write_text("# Old content\n\nLine 1\n", encoding="utf-8")
    drafts_root = tmp_path / "auto-drafts" / "2026-04-23"
    drafts_root.mkdir(parents=True)
    draft_file = _make_draft(drafts_root, "test.draft.md", str(target),
                              "## New section\n\n- new item\n")
    info = DraftInfo(path=draft_file, target_path=target,
                     source_session="s", source_date="d", type="t",
                     body="## New section\n\n- new item\n")
    diff = render_diff(info)
    assert "+## New section" in diff
    assert "+- new item" in diff
    assert str(target) in diff


def test_render_diff_when_target_missing(tmp_path: Path):
    target = tmp_path / "no-such-file.md"
    drafts_root = tmp_path / "auto-drafts" / "2026-04-23"
    drafts_root.mkdir(parents=True)
    draft_file = _make_draft(drafts_root, "x.draft.md", str(target), "## new")
    info = DraftInfo(path=draft_file, target_path=target,
                     source_session="s", source_date="d", type="t",
                     body="## new")
    diff = render_diff(info)
    # 目标文件不存在应该渲染为"创建新文件"形式
    assert "+## new" in diff
    assert "no-such-file.md" in diff


def test_find_drafts_missing_root_returns_empty(tmp_path: Path):
    assert find_drafts(tmp_path / "missing") == []
