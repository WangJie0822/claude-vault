"""tests for scripts/git_commit_vault.py"""
import os
import sys
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import git_commit_vault as gcv  # noqa: E402


def _init_repo(path):
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    subprocess.run(['git', '-C', str(path), 'config', 'user.email', 't@t'], check=True)
    subprocess.run(['git', '-C', str(path), 'config', 'user.name', 't'], check=True)


def test_skipped_when_not_git(tmp_path):
    r = gcv.commit_vault(str(tmp_path), '标题')
    assert r['status'] == 'skipped' and r['reason'] == 'not_git'


def test_skipped_when_no_commit_flag(tmp_path):
    _init_repo(tmp_path)
    r = gcv.commit_vault(str(tmp_path), '标题', no_commit=True)
    assert r['status'] == 'skipped' and r['reason'] == 'no_commit_flag'


def test_sanitize_title_strips_control_and_truncates():
    assert '\n' not in gcv.sanitize_title('a\nb')
    assert gcv.sanitize_title('x"; rm -rf $(y) `z`')  # 不抛异常，返回字符串
    assert len(gcv.sanitize_title('一' * 100)) <= 60


def test_enumerate_only_md_in_include_dirs(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / '工作日志').mkdir()
    (tmp_path / '工作日志' / 'a.md').write_text('x', encoding='utf-8')
    (tmp_path / 'Claude Code').mkdir()
    (tmp_path / 'Claude Code' / 'b.md').write_text('y', encoding='utf-8')
    (tmp_path / 'CLAUDE.md').write_text('z', encoding='utf-8')
    (tmp_path / 'junk.txt').write_text('no', encoding='utf-8')  # 非 .md 不选
    (tmp_path / '.meta').mkdir()
    (tmp_path / '.meta' / 'pending-docs.json').write_text('[]', encoding='utf-8')  # 不在白名单
    changes = gcv.enumerate_changes(str(tmp_path))
    assert '工作日志/a.md' in changes
    assert 'Claude Code/b.md' in changes
    assert 'CLAUDE.md' in changes
    assert 'junk.txt' not in changes
    assert all(not c.startswith('.meta') for c in changes)


def test_commit_creates_commit(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / 'Claude Code').mkdir()
    (tmp_path / 'Claude Code' / 'note.md').write_text('内容', encoding='utf-8')
    r = gcv.commit_vault(str(tmp_path), '测试笔记')
    assert r['status'] == 'committed'
    log = subprocess.run(['git', '-C', str(tmp_path), 'log', '--oneline'],
                         capture_output=True, text=True, encoding='utf-8')
    assert '测试笔记' in log.stdout


def test_nothing_to_commit(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / 'a.txt').write_text('x', encoding='utf-8')  # 非知识库 .md
    r = gcv.commit_vault(str(tmp_path), '标题')
    assert r['status'] == 'nothing'


def test_baseline_preview_lists_files(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / 'Claude Code').mkdir()
    (tmp_path / 'Claude Code' / 'n.md').write_text('x', encoding='utf-8')
    r = gcv.baseline_preview(str(tmp_path))
    assert r['status'] == 'preview'
    assert any('n.md' in f for f in r['files'])


def test_commit_failed_does_not_raise(tmp_path, monkeypatch):
    """commit 失败 → status=failed，不抛异常（不阻塞 skill 后续，finding F）。"""
    _init_repo(tmp_path)
    (tmp_path / 'Claude Code').mkdir()
    (tmp_path / 'Claude Code' / 'n.md').write_text('x', encoding='utf-8')
    orig = gcv._git

    def fake(vault, args):
        if args and args[0] == 'commit':
            class R:
                returncode = 1
                stdout = ''
                stderr = 'simulated failure'
            return R()
        return orig(vault, args)

    monkeypatch.setattr(gcv, '_git', fake)
    res = gcv.commit_vault(str(tmp_path), '标题')
    assert res['status'] == 'failed' and 'git_commit' in res['reason']


def test_baseline_commits_all_including_non_md(tmp_path):
    """--baseline 全量 commit（含非 .md + 中文路径，finding D）。"""
    _init_repo(tmp_path)
    (tmp_path / '工作日志').mkdir()
    (tmp_path / '工作日志' / '中文.md').write_text('x', encoding='utf-8')
    (tmp_path / 'random.txt').write_text('y', encoding='utf-8')
    res = gcv.commit_vault(str(tmp_path), 't', baseline=True)
    assert res['status'] == 'committed' and res['files'] == -1
    show = subprocess.run(['git', '-C', str(tmp_path), '-c', 'core.quotepath=false',
                           'show', '--name-only', '--format='],
                          capture_output=True, text=True, encoding='utf-8')
    assert '中文.md' in show.stdout and 'random.txt' in show.stdout


def test_rename_stages_old_path_deletion(tmp_path):
    """知识库 .md 重命名时旧路径删除也被 stage（M2，避免孤立 + commit 记凭空新增）。"""
    _init_repo(tmp_path)
    d = tmp_path / 'Claude Code'
    d.mkdir()
    (d / 'old.md').write_text('内容相同足够触发 rename 检测的文本', encoding='utf-8')
    subprocess.run(['git', '-C', str(tmp_path), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'commit', '-qm', 'init'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'mv', 'Claude Code/old.md', 'Claude Code/new.md'], check=True)
    changes = gcv.enumerate_changes(str(tmp_path))
    assert 'Claude Code/new.md' in changes
    assert 'Claude Code/old.md' in changes  # 旧路径删除也纳入


from git_commit_vault import _is_knowledge_md


def test_is_knowledge_md_new_index_names():
    assert _is_knowledge_md('未分类 索引.md') is True
    assert _is_knowledge_md('Windows 系统/Windows 系统 索引.md') is True
    assert _is_knowledge_md('改进计划/改进计划 索引.md') is True


def test_is_knowledge_md_rejects_arbitrary_suffix_note():
    # 文件名 != 父目录名 + ' 索引.md' 且 top 不在白名单 → 拒绝
    assert _is_knowledge_md('随便目录/随便 索引.md') is False


def test_is_knowledge_md_path_traversal_still_blocked():
    assert _is_knowledge_md('../外部 索引.md') is False
    assert _is_knowledge_md('/abs/x 索引.md') is False
