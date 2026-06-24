"""tests for scripts/_linkgraph.py"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _linkgraph as lg  # noqa: E402


def test_extract_strips_frontmatter():
    md = '---\nrelated: [[fm-link]]\n---\n正文 [[real]]'
    assert lg.extract_wikilinks(md) == ['real']  # frontmatter 的 fm-link 不计入


def test_extract_masks_fenced_code():
    md = '正文 [[real]]\n```\n[[code]]\n```\n'
    assert lg.extract_wikilinks(md) == ['real']


def test_extract_masks_inline_double_backtick():
    md = '说明 ``[[syntax]]`` 与真链 [[real]]'
    assert lg.extract_wikilinks(md) == ['real']


def test_extract_normalizes_anchor_alias_subdir():
    md = '[[x#锚点]] [[y|别名]] [[specs/z]]'
    assert lg.extract_wikilinks(md) == ['x', 'y', 'z']


def test_analyze_unresolved(tmp_path):
    (tmp_path / 'a.md').write_text('指向 [[missing]] 和 [[b]]', encoding='utf-8')
    (tmp_path / 'b.md').write_text('我是 b', encoding='utf-8')
    res = lg.analyze(str(tmp_path))
    targets = {u['target'] for u in res['unresolved_links']}
    assert 'missing' in targets
    assert 'b' not in targets  # b.md 存在 → resolved


def test_analyze_specplan_no_backlink(tmp_path):
    specs = tmp_path / 'Claude Code' / 'specs'
    specs.mkdir(parents=True)
    (specs / 'lonely-spec.md').write_text('# spec 无出链', encoding='utf-8')
    (tmp_path / 'note.md').write_text('正文用反引号 `lonely-spec` 引用', encoding='utf-8')
    res = lg.analyze(str(tmp_path))
    no_bl = {s['stem'] for s in res['specplan_no_backlink']}
    assert 'lonely-spec' in no_bl  # 无 [[wikilink]] 指向它


def test_extract_masks_tilde_fence():
    md = '正文 [[real]]\n~~~\n[[code]]\n~~~\n'
    assert lg.extract_wikilinks(md) == ['real']  # ~~~ 块内不计入（M1）


def test_analyze_skips_non_utf8(tmp_path):
    """非 UTF-8 笔记被静默跳过，不崩（_linkgraph.py UnicodeDecodeError 分支）。"""
    (tmp_path / 'a.md').write_text('正文 [[b]]', encoding='utf-8')
    (tmp_path / 'bad.md').write_bytes(b'\xff\xfe\x00\xacbad')  # 非 UTF-8
    res = lg.analyze(str(tmp_path))  # 不抛异常
    assert isinstance(res['unresolved_links'], list)
