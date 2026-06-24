"""tests for scripts/_path_resolver.py"""
import os
import sys
import pathlib
import subprocess
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from _path_resolver import (
    resolve_project_root,
    resolve_vault_target,
    is_inside_vault,
)


def _init_git_repo(path: pathlib.Path) -> None:
    subprocess.run(['git', 'init', '-q'], cwd=str(path), check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@x.x'], cwd=str(path), check=True)
    subprocess.run(['git', 'config', 'user.name', 'test'], cwd=str(path), check=True)


def test_resolve_project_root_in_normal_git(tmp_path):
    _init_git_repo(tmp_path)
    sub = tmp_path / 'docs' / 'superpowers' / 'specs'
    sub.mkdir(parents=True)
    f = sub / 'x.md'
    f.write_text('# x', encoding='utf-8')
    root = resolve_project_root(str(f))
    assert pathlib.Path(root).resolve() == tmp_path.resolve()


def test_resolve_project_root_in_worktree(tmp_path):
    """worktree 内调用应返回主仓库 toplevel 而非 worktree path。"""
    main = tmp_path / 'main'
    main.mkdir()
    _init_git_repo(main)
    (main / 'a.txt').write_text('a', encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=str(main), check=True)
    subprocess.run(['git', 'commit', '-qm', 'init'], cwd=str(main), check=True)
    wt = tmp_path / 'worktrees' / 'feat'
    subprocess.run(['git', 'worktree', 'add', '-q', str(wt), '-b', 'feat-branch'],
                   cwd=str(main), check=True)
    sub = wt / 'docs' / 'x.md'
    sub.parent.mkdir(parents=True)
    sub.write_text('# x', encoding='utf-8')
    root = resolve_project_root(str(sub))
    assert pathlib.Path(root).resolve() == main.resolve()


def test_resolve_project_root_non_git_returns_none(tmp_path):
    (tmp_path / 'x.md').write_text('# x', encoding='utf-8')
    root = resolve_project_root(str(tmp_path / 'x.md'))
    assert root is None


def test_is_inside_vault_match(tmp_path):
    assert is_inside_vault(str(tmp_path / 'note.md'), str(tmp_path)) is True


def test_is_inside_vault_mismatch(tmp_path):
    assert is_inside_vault('/other/path/x.md', str(tmp_path)) is False


def test_resolve_vault_target_existing_dir_match(tmp_path):
    """Glob $VAULT/*/<basename>/ 命中现有目录 → 用现有目录"""
    vault = tmp_path / 'Vault'
    (vault / 'Claude Code' / 'myrepo').mkdir(parents=True)
    (vault / '项目笔记' / 'other').mkdir(parents=True)
    project_basename = 'myrepo'
    target = resolve_vault_target(str(vault), project_basename, doc_type='spec')
    assert pathlib.Path(target).parent.name == 'specs' or pathlib.Path(target).name == 'specs'
    # 应命中 vault/Claude Code/myrepo/specs/
    assert 'myrepo' in target


def test_resolve_vault_target_fallback_to_xiangmu(tmp_path):
    """失配 → 默认 项目笔记/<basename>/"""
    vault = tmp_path / 'Vault'
    vault.mkdir()
    target = resolve_vault_target(str(vault), 'newproject', doc_type='plan')
    assert '项目笔记' in target
    assert 'newproject' in target
    assert pathlib.Path(target).name == 'plans' or pathlib.Path(target).parent.name == 'plans'


def test_resolve_vault_target_claude_code_for_dot_claude(tmp_path):
    """原路径在 ~/.claude/ 内 → Claude Code/"""
    vault = tmp_path / 'Vault'
    vault.mkdir()
    target = resolve_vault_target(str(vault), '.claude', doc_type='spec',
                                  original_path='/home/u/.claude/docs/x.md')
    assert 'Claude Code' in target
