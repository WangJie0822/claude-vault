"""tests for scripts/fix_links.py"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fix_links as fl  # noqa: E402


def test_replace_unresolved_in_body(tmp_path):
    (tmp_path / 'b.md').write_text('我是 b', encoding='utf-8')
    note = tmp_path / 'a.md'
    note.write_text('真链 [[b]] 悬空 [[missing]]', encoding='utf-8')
    fl.run(str(tmp_path), apply=True)
    txt = note.read_text(encoding='utf-8')
    assert '[[b]]' in txt           # resolved 保留
    assert '[[missing]]' not in txt  # unresolved 改写
    assert '`missing`' in txt        # 改成反引号


def test_never_touches_frontmatter(tmp_path):
    note = tmp_path / 'a.md'
    note.write_text('---\nrelated: [[missing]]\n---\n正文 [[missing2]]',
                    encoding='utf-8')
    fl.run(str(tmp_path), apply=True)
    txt = note.read_text(encoding='utf-8')
    assert 'related: [[missing]]' in txt  # frontmatter 原样保留（finding B）


def test_never_touches_code_block(tmp_path):
    note = tmp_path / 'a.md'
    note.write_text('```\n[[missing]]\n```\n正文 [[missing2]]', encoding='utf-8')
    fl.run(str(tmp_path), apply=True)
    txt = note.read_text(encoding='utf-8')
    assert '[[missing]]' in txt  # 代码块内保留


def test_dry_run_does_not_write(tmp_path):
    note = tmp_path / 'a.md'
    note.write_text('悬空 [[missing]]', encoding='utf-8')
    fl.run(str(tmp_path), apply=False)
    assert '[[missing]]' in note.read_text(encoding='utf-8')  # dry-run 不写


def test_never_touches_tilde_fence(tmp_path):
    """~~~ tilde fenced code block 内 wikilink 不被改写（M1）。"""
    note = tmp_path / 'a.md'
    note.write_text('~~~\n[[missing]]\n~~~\n正文 [[missing2]]', encoding='utf-8')
    fl.run(str(tmp_path), apply=True)
    txt = note.read_text(encoding='utf-8')
    assert '[[missing]]' in txt  # ~~~ 块内保留
