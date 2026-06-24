# tests/test_archive_note.py
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from archive_note import archive_note
from unarchive_note import unarchive_note


def _git_init(path):
    subprocess.run(['git', 'init', '-q'], cwd=path, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@t'], cwd=path, check=True)
    subprocess.run(['git', 'config', 'user.name', 't'], cwd=path, check=True)
    subprocess.run(['git', 'config', 'core.hooksPath', '/dev/null'], cwd=path, check=True)


def test_archive_moves_and_marks(tmp_vault):
    _git_init(tmp_vault)
    (tmp_vault / '缺陷全链路').mkdir()
    note = tmp_vault / '缺陷全链路' / 'x.md'
    note.write_text(
        '---\ncategory: 缺陷全链路\nstatus: active\n---\n内容\n', encoding='utf-8')
    subprocess.run(['git', 'add', '.'], cwd=tmp_vault, check=True)
    subprocess.run(['git', 'commit', '-m', 'init', '-q'], cwd=tmp_vault, check=True)

    archive_note(tmp_vault, '缺陷全链路/x.md', reason='功能废弃', date='2026-04-20')

    assert not (tmp_vault / '缺陷全链路' / 'x.md').exists()
    archived = tmp_vault / '缺陷全链路' / 'archive' / 'x.md'
    assert archived.exists()
    text = archived.read_text(encoding='utf-8')
    assert 'status: archived' in text
    assert 'archived_reason: 功能废弃' in text or 'archived_reason: "功能废弃"' in text
    assert 'archived_date: 2026-04-20' in text


def test_unarchive_restores(tmp_vault):
    _git_init(tmp_vault)
    arch_dir = tmp_vault / '缺陷全链路' / 'archive'
    arch_dir.mkdir(parents=True)
    note = arch_dir / 'y.md'
    note.write_text(
        '---\ncategory: 缺陷全链路\nstatus: archived\narchived_reason: 测试\narchived_date: 2026-04-20\n---\n',
        encoding='utf-8')
    subprocess.run(['git', 'add', '.'], cwd=tmp_vault, check=True)
    subprocess.run(['git', 'commit', '-m', 'init', '-q'], cwd=tmp_vault, check=True)

    unarchive_note(tmp_vault, '缺陷全链路/archive/y.md')

    restored = tmp_vault / '缺陷全链路' / 'y.md'
    assert restored.exists()
    text = restored.read_text(encoding='utf-8')
    assert 'status: active' in text
    assert 'archived_reason' not in text
    assert 'archived_date' not in text


# ---------- 新增 8 条边界测试 ----------


def test_archive_path_traversal_rejected(tmp_vault):
    """C1:绝对路径与含 .. 的相对路径都必须被拒绝。"""
    _git_init(tmp_vault)
    with pytest.raises(ValueError):
        archive_note(tmp_vault, '../../etc/passwd', reason='r')
    with pytest.raises(ValueError):
        archive_note(tmp_vault, '/tmp/abs', reason='r')


def test_archive_untracked_file_fallback(tmp_vault):
    """C2:文件未 git add,git mv 会失败,fallback 到 rename 仍归档成功。"""
    _git_init(tmp_vault)
    (tmp_vault / '缺陷全链路').mkdir()
    note = tmp_vault / '缺陷全链路' / 'x.md'
    note.write_text(
        '---\ncategory: 缺陷全链路\n---\n内容\n', encoding='utf-8')
    # 故意不 git add/commit:文件未纳入 git

    archive_note(tmp_vault, '缺陷全链路/x.md', reason='r', date='2026-04-20')

    assert not (tmp_vault / '缺陷全链路' / 'x.md').exists()
    assert (tmp_vault / '缺陷全链路' / 'archive' / 'x.md').exists()


def test_archive_no_frontmatter(tmp_vault):
    """纯正文文件归档后前置新 frontmatter 块,含 3 字段。"""
    _git_init(tmp_vault)
    (tmp_vault / '缺陷全链路').mkdir()
    note = tmp_vault / '缺陷全链路' / 'x.md'
    note.write_text('纯正文无 frontmatter\n', encoding='utf-8')
    subprocess.run(['git', 'add', '.'], cwd=tmp_vault, check=True)
    subprocess.run(['git', 'commit', '-m', 'init', '-q'], cwd=tmp_vault, check=True)

    archive_note(tmp_vault, '缺陷全链路/x.md', reason='r', date='2026-04-20')

    archived = tmp_vault / '缺陷全链路' / 'archive' / 'x.md'
    text = archived.read_text(encoding='utf-8')
    assert text.startswith('---\n')
    assert 'status: archived' in text
    assert 'archived_reason: r' in text
    assert 'archived_date: 2026-04-20' in text
    assert '纯正文无 frontmatter' in text


def test_archive_unclosed_frontmatter_raises(tmp_vault):
    """I2:frontmatter 未闭合时抛 ValueError,不再静默失败。"""
    _git_init(tmp_vault)
    (tmp_vault / '缺陷全链路').mkdir()
    note = tmp_vault / '缺陷全链路' / 'x.md'
    note.write_text('---\ncategory: 缺陷全链路\n没有闭合\n', encoding='utf-8')
    subprocess.run(['git', 'add', '.'], cwd=tmp_vault, check=True)
    subprocess.run(['git', 'commit', '-m', 'init', '-q'], cwd=tmp_vault, check=True)

    with pytest.raises(ValueError):
        archive_note(tmp_vault, '缺陷全链路/x.md', reason='r', date='2026-04-20')


def test_archive_double_archive_rejected(tmp_vault):
    """I1:已在 archive 目录下的笔记不得再次归档。"""
    _git_init(tmp_vault)
    arch_dir = tmp_vault / '缺陷全链路' / 'archive'
    arch_dir.mkdir(parents=True)
    note = arch_dir / 'x.md'
    note.write_text('---\nstatus: archived\n---\n', encoding='utf-8')
    subprocess.run(['git', 'add', '.'], cwd=tmp_vault, check=True)
    subprocess.run(['git', 'commit', '-m', 'init', '-q'], cwd=tmp_vault, check=True)

    with pytest.raises(ValueError):
        archive_note(tmp_vault, '缺陷全链路/archive/x.md', reason='r')


def test_archive_reason_with_colon_quotes(tmp_vault):
    """含 ':' 的 reason 必须加双引号。"""
    _git_init(tmp_vault)
    (tmp_vault / '缺陷全链路').mkdir()
    note = tmp_vault / '缺陷全链路' / 'x.md'
    note.write_text('---\ncategory: 缺陷全链路\n---\n', encoding='utf-8')
    subprocess.run(['git', 'add', '.'], cwd=tmp_vault, check=True)
    subprocess.run(['git', 'commit', '-m', 'init', '-q'], cwd=tmp_vault, check=True)

    archive_note(tmp_vault, '缺陷全链路/x.md', reason='a: b', date='2026-04-20')

    archived = tmp_vault / '缺陷全链路' / 'archive' / 'x.md'
    text = archived.read_text(encoding='utf-8')
    assert 'archived_reason: "a: b"' in text


def test_archive_then_unarchive_roundtrip(tmp_vault):
    """archive + unarchive:最终文件在原位、无 archived_* 字段、status=active。"""
    _git_init(tmp_vault)
    (tmp_vault / '缺陷全链路').mkdir()
    note = tmp_vault / '缺陷全链路' / 'x.md'
    note.write_text(
        '---\ncategory: 缺陷全链路\nstatus: active\n---\n内容\n', encoding='utf-8')
    subprocess.run(['git', 'add', '.'], cwd=tmp_vault, check=True)
    subprocess.run(['git', 'commit', '-m', 'init', '-q'], cwd=tmp_vault, check=True)

    archive_note(tmp_vault, '缺陷全链路/x.md', reason='r', date='2026-04-20')
    unarchive_note(tmp_vault, '缺陷全链路/archive/x.md')

    restored = tmp_vault / '缺陷全链路' / 'x.md'
    assert restored.exists()
    assert not (tmp_vault / '缺陷全链路' / 'archive' / 'x.md').exists()
    text = restored.read_text(encoding='utf-8')
    assert 'status: active' in text
    assert 'archived_reason' not in text
    assert 'archived_date' not in text


def test_unarchive_nested_subcategory(tmp_vault):
    """unarchive 能处理 archive 在 category 下、再套 subcategory 的场景:
    缺陷全链路/archive/bug-batch/x.md → 缺陷全链路/bug-batch/x.md
    """
    _git_init(tmp_vault)
    arch_dir = tmp_vault / '缺陷全链路' / 'archive' / 'bug-batch'
    arch_dir.mkdir(parents=True)
    note = arch_dir / 'x.md'
    note.write_text(
        '---\ncategory: 缺陷全链路\nsubcategory: bug-batch\nstatus: archived\narchived_reason: r\narchived_date: 2026-04-20\n---\n',
        encoding='utf-8')
    subprocess.run(['git', 'add', '.'], cwd=tmp_vault, check=True)
    subprocess.run(['git', 'commit', '-m', 'init', '-q'], cwd=tmp_vault, check=True)

    unarchive_note(tmp_vault, '缺陷全链路/archive/bug-batch/x.md')

    restored = tmp_vault / '缺陷全链路' / 'bug-batch' / 'x.md'
    assert restored.exists()
    text = restored.read_text(encoding='utf-8')
    assert 'status: active' in text
    assert 'archived_reason' not in text
    assert 'archived_date' not in text
