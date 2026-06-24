"""tests for scripts/archive_doc.py (T1-T23 spec)"""
import os
import sys
import pathlib
import json
import re
import hashlib

import pytest

# conftest.py 已经把 scripts/ 加入 sys.path
from archive_doc import archive_doc


def _make_pending_entry(path, doc_type='spec', context='test'):
    return {
        'path': path,
        'type': doc_type,
        'context': context,
        'created': '2026-05-28T10:00:00+08:00',
    }


def _read_frontmatter_kv(file_path):
    """读 frontmatter 解析成 dict（去掉 YAML 双引号外壳）。"""
    text = pathlib.Path(file_path).read_text(encoding='utf-8')
    m = re.match(r'^---\n(.*?)\n---\n', text, re.DOTALL)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            val = v.strip()
            # 去掉 YAML _yaml_scalar 加的外层双引号
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1].replace('\\"', '"').replace('\\\\', '\\')
            out[k.strip()] = val
    return out


def _make_git_repo(path):
    import subprocess
    subprocess.run(['git', 'init', '-q'], cwd=str(path), check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@x.x'], cwd=str(path), check=True)
    subprocess.run(['git', 'config', 'user.name', 'test'], cwd=str(path), check=True)


# ========== T1-T8 基础归集 ==========

def test_T1_first_archive_no_frontmatter(tmp_path):
    """T1: 原文件无 frontmatter → 加全套 vault_* frontmatter"""
    repo = tmp_path / 'project'
    repo.mkdir()
    _make_git_repo(repo)
    src = repo / 'docs' / 'spec.md'
    src.parent.mkdir(parents=True)
    src.write_text('# Body\n\nbody content', encoding='utf-8')

    vault = tmp_path / 'Vault'
    vault.mkdir()
    entry = _make_pending_entry(str(src))
    result = archive_doc(entry, vault_root=str(vault))

    assert result['status'] == 'new_archived'
    vp = pathlib.Path(result['vault_path'])
    assert vp.exists()
    fm = _read_frontmatter_kv(vp)
    assert fm.get('vault_source_repo') == 'project'
    assert 'vault_source_path' in fm
    assert fm.get('vault_source_hash', '').startswith('sha256:')
    assert fm.get('vault_content_hash', '').startswith('sha256:')
    assert fm.get('vault_archived_at') == '2026-05-28'  # YYYY-MM-DD
    assert fm.get('tags') == '[spec, archived]'


def test_T2_first_archive_with_user_source_field_preserved(tmp_path):
    """T2: 原文件已有用户 source 字段 → 保留不动，vault_* 字段追加"""
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'spec.md'
    src.write_text(
        '---\nsource: 外部规范 v1.2\ntitle: My Spec\n---\n\nbody',
        encoding='utf-8')

    vault = tmp_path / 'Vault'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'new_archived'
    fm = _read_frontmatter_kv(result['vault_path'])
    assert fm.get('source') == '外部规范 v1.2'  # 用户字段保留
    assert fm.get('title') == 'My Spec'
    assert fm.get('vault_source_repo') == 'p'


def test_T3_unclosed_frontmatter_fail_fast(tmp_path):
    """T3: 原文件 frontmatter 未闭合 → fail-fast"""
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'spec.md'
    src.write_text('---\nsource: x\nbody without closing fence',
                   encoding='utf-8')
    vault = tmp_path / 'Vault'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'error'
    assert 'frontmatter' in result['reason'].lower()


def test_T4_worktree_path_uses_main_repo_toplevel(tmp_path):
    """T4: worktree 内 path → git common-dir 探测主仓库 toplevel basename"""
    import subprocess
    main = tmp_path / 'mainrepo'; main.mkdir(); _make_git_repo(main)
    (main / 'x').write_text('x', encoding='utf-8')
    subprocess.run(['git', 'add', '-A'], cwd=str(main), check=True)
    subprocess.run(['git', 'commit', '-qm', 'i'], cwd=str(main), check=True)
    wt = tmp_path / 'wt'
    subprocess.run(['git', 'worktree', 'add', '-q', str(wt), '-b', 'br'],
                   cwd=str(main), check=True)
    src = wt / 'spec.md'
    src.write_text('# body', encoding='utf-8')
    vault = tmp_path / 'Vault'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'new_archived'
    fm = _read_frontmatter_kv(result['vault_path'])
    assert fm.get('vault_source_repo') == 'mainrepo'


def test_T5_normal_git_toplevel(tmp_path):
    repo = tmp_path / 'myproj'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'docs' / 'x.md'; src.parent.mkdir()
    src.write_text('# x', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    fm = _read_frontmatter_kv(result['vault_path'])
    assert fm.get('vault_source_repo') == 'myproj'


def test_T6_non_git_fallback_to_xiangmu(tmp_path):
    src = tmp_path / 'spec.md'
    src.write_text('# x', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'new_archived'
    assert '项目笔记' in result['vault_path']


def test_T7_path_in_dot_claude_goes_to_claude_code(tmp_path, monkeypatch):
    # 模拟 path 在 ~/.claude/ 内
    fake_home = tmp_path / 'home' / 'u'
    fake_claude = fake_home / '.claude' / 'docs'
    fake_claude.mkdir(parents=True)
    src = fake_claude / 'spec.md'
    src.write_text('# x', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'new_archived'
    assert 'Claude Code' in result['vault_path']


def test_T8_sha256_matches(tmp_path):
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'
    content = b'# hello\n'
    src.write_bytes(content)
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    expected = 'sha256:' + hashlib.sha256(content).hexdigest()
    assert result['source_content_hash'] == expected


# ========== T9-T15 frontmatter / 冲突 / 路径校验 ==========

def test_T9_T10_category_inferred_from_path(tmp_path):
    """T9/T10: 路径自然推断 category/subcategory，不 fail-fast"""
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'spec.md'
    src.write_text('# body', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    (vault / '项目笔记' / 'p').mkdir(parents=True)
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    fm = _read_frontmatter_kv(result['vault_path'])
    assert fm.get('category') == '项目笔记'
    assert fm.get('subcategory') == 'p'


def test_T11a_conflict_default_fail_fast(tmp_path):
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'; src.write_text('# new', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    (vault / '项目笔记' / 'p').mkdir(parents=True)
    # 预先放一个带 vault_source_repo=other 的副本（模拟别处归集）
    target = vault / '项目笔记' / 'p' / 'specs' / 'x.md'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        '---\nvault_source_repo: other\nvault_source_path: y.md\n---\nold',
        encoding='utf-8')
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault),
                         allow_adopt=False)
    # adopt 关闭、source 不匹配 → conflict
    assert result['status'] == 'conflict'


def test_T11b_conflict_with_rename_on_conflict_adds_timestamp(tmp_path):
    """T11b: 有 vault_source_* 不匹配 + rename_on_conflict=True → 加 timestamp 后缀

    场景：
    - Vault 目标位置已有副本，frontmatter 含 vault_source_repo=other（属别处归集）
    - 不允许 adopt（allow_adopt=False）
    - 但启用 rename_on_conflict → 新归集走到旁路文件名（stem-YYYYMMDD-HHMMSS.md），不覆盖旧副本
    """
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'; src.write_text('# new', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    target_dir = vault / '项目笔记' / 'p' / 'specs'
    target_dir.mkdir(parents=True)
    target = target_dir / 'x.md'
    target.write_text(
        '---\nvault_source_repo: other\nvault_source_path: y.md\n---\nold',
        encoding='utf-8')
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault),
                         allow_adopt=False, rename_on_conflict=True)
    assert result['status'] == 'new_archived'
    # vault_path 应含 timestamp 后缀
    assert re.search(r'-\d{8}-\d{6}\.md$', result['vault_path'])
    # 旧副本不动
    assert target.exists()
    assert 'old' in target.read_text(encoding='utf-8')


def test_T11c_adopt_when_no_vault_source(tmp_path):
    """T11c: 目标存在 + 无 vault_source_* + basename 匹配 → adopt"""
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'; src.write_text('# new', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    target_dir = vault / '项目笔记' / 'p' / 'specs'
    target_dir.mkdir(parents=True)
    target = target_dir / 'x.md'
    # 早期手工副本，无 vault_source_*
    target.write_text('---\ntitle: hand-archived\n---\nold body', encoding='utf-8')
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'adopted'
    # 正文不动
    text = target.read_text(encoding='utf-8')
    assert 'old body' in text
    # frontmatter 加了 vault_source_*
    fm = _read_frontmatter_kv(target)
    assert fm.get('vault_source_repo') == 'p'
    assert fm.get('adopted_from_existing') == 'true'


def test_T11d_no_adopt_fail_fast(tmp_path):
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'; src.write_text('# n', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    target_dir = vault / '项目笔记' / 'p' / 'specs'; target_dir.mkdir(parents=True)
    (target_dir / 'x.md').write_text('# old', encoding='utf-8')
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault),
                         allow_adopt=False)
    assert result['status'] == 'conflict'


def test_T12_path_with_tilde(tmp_path):
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry('~/docs/x.md'),
                        vault_root=str(vault))
    assert result['status'] == 'path_invalid'


def test_T13_source_unreadable(tmp_path, monkeypatch):
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'; src.write_text('# x', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    # mock _sha256_file 抛 OSError
    import archive_doc as ad
    def boom(p):
        raise OSError('mock io error')
    monkeypatch.setattr(ad, '_sha256_file', boom)
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'error'


def test_T14_strip_bom(tmp_path):
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'
    src.write_bytes('﻿# body'.encode('utf-8'))
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'new_archived'
    # vault 副本应该没 BOM
    vault_bytes = pathlib.Path(result['vault_path']).read_bytes()
    assert not vault_bytes.startswith('﻿'.encode('utf-8'))


def test_T15_path_separator_normalize(tmp_path):
    """Windows backslash 与 forward slash 同效"""
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'; src.write_text('# x', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    src_str = str(src).replace('/', os.sep)
    result = archive_doc(_make_pending_entry(src_str), vault_root=str(vault))
    assert result['status'] == 'new_archived'


# ========== T16-T23 short-circuit / 敏感文件 / atomic rename ==========

def test_T16_path_in_vault_short_circuit(tmp_path):
    vault = tmp_path / 'V'; vault.mkdir()
    src = vault / 'Claude Code' / 'x.md'
    src.parent.mkdir(parents=True)
    src.write_text('# x', encoding='utf-8')
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'in_vault_short_circuit'
    assert result['vault_path'] == str(src)


def test_T17_sensitive_path_denied(tmp_path):
    vault = tmp_path / 'V'; vault.mkdir()
    # 模拟一个 .env 文件
    src = tmp_path / 'project' / '.env'
    src.parent.mkdir(parents=True)
    src.write_text('SECRET=x', encoding='utf-8')
    result = archive_doc(_make_pending_entry(str(src), doc_type='other'),
                         vault_root=str(vault))
    assert result['status'] == 'denied_sensitive'


def test_T18_sensitive_whitelist_design_doc(tmp_path):
    """文件名含 'design' 关键词的 markdown 豁免"""
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / '2026-05-26-credentials-path-unify-design.md'
    src.write_text('# spec', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'new_archived'


def test_T19_tags_yaml_inline_style(tmp_path):
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'; src.write_text('# x', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    text = pathlib.Path(result['vault_path']).read_text(encoding='utf-8')
    # 不应有 Python repr 单引号
    assert "['spec'" not in text
    assert "tags: [spec, archived]" in text


def test_T21_symlink_blocked(tmp_path):
    if os.name == 'nt':
        pytest.skip('symlink requires admin on Windows')
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'; src.write_text('# new', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    target_dir = vault / '项目笔记' / 'p' / 'specs'
    target_dir.mkdir(parents=True)
    target = target_dir / 'x.md'
    other = tmp_path / 'other.md'; other.write_text('# o', encoding='utf-8')
    os.symlink(str(other), str(target))
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'symlink_blocked'


def test_T22_atomic_rename_no_partial_write(tmp_path):
    """中断写入不应留下半成品（os.replace 原子）。"""
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'; src.write_text('# new', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    # 完整文件已落到 vault_path
    assert pathlib.Path(result['vault_path']).exists()
    # 目标目录里不应残留 .archive_doc_*.tmp
    leftover = list(pathlib.Path(result['vault_path']).parent.glob('.archive_doc_*.tmp'))
    assert leftover == []


def test_T23_vault_source_no_absolute_path_in_frontmatter(tmp_path):
    """vault_source_repo / vault_source_path 用相对形式，不含本机绝对路径前缀。"""
    repo = tmp_path / 'projx'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'sub' / 'x.md'
    src.parent.mkdir()
    src.write_text('# x', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    fm = _read_frontmatter_kv(result['vault_path'])
    assert fm.get('vault_source_repo') == 'projx'
    assert fm.get('vault_source_path') == 'sub/x.md'
    # 绝对路径前缀不应出现
    text = pathlib.Path(result['vault_path']).read_text(encoding='utf-8')
    assert str(repo).replace('\\', '/').lower() not in text.lower()


# ========== T24-T25 Critical bug regression: vault_content_hash 时序 ==========

def test_T24_vault_content_hash_matches_body_after_write(tmp_path):
    """T24 (Critical bug regression): archive_doc 返回的 vault_content_hash
    必须等于"重读 Vault 副本 + strip frontmatter + sha256"，否则 Task 6 sync
    会第一次跑就把所有副本误判为 conflict_vault_edited。"""
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'; src.write_text('# Body\n\nbody content', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'new_archived'
    stored = result['vault_content_hash']

    # 重新计算 body-only hash
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
    from archive_doc import _sha256_body
    on_disk_body_hash = _sha256_body(result['vault_path'])
    assert stored == on_disk_body_hash, (
        f'stored vault_content_hash {stored} != body-only hash on disk {on_disk_body_hash}; '
        'Task 6 sync 会首跑就误报 conflict_vault_edited')


def test_T25_vault_content_hash_stable_across_repeated_archive(tmp_path):
    """T25: 同一文件归集（虽然 archive_doc 不重复调用，但 adopt 流程下） vault_content_hash
    应等于 body-only hash"""
    repo = tmp_path / 'p'; repo.mkdir(); _make_git_repo(repo)
    src = repo / 'x.md'; src.write_text('# new', encoding='utf-8')
    vault = tmp_path / 'V'; vault.mkdir()
    target_dir = vault / '项目笔记' / 'p' / 'specs'; target_dir.mkdir(parents=True)
    target = target_dir / 'x.md'
    target.write_text('---\ntitle: hand-archived\n---\nold body', encoding='utf-8')
    result = archive_doc(_make_pending_entry(str(src)), vault_root=str(vault))
    assert result['status'] == 'adopted'
    stored = result['vault_content_hash']

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
    from archive_doc import _sha256_body
    on_disk_body_hash = _sha256_body(result['vault_path'])
    assert stored == on_disk_body_hash


def test_T26_source_not_exist_returns_original_missing(tmp_path):
    """T26: 原文件不存在 → archive_doc 返回 original_missing 而非 error
    （spec L70-74 backfill 流程要求）"""
    nonexistent = tmp_path / 'gone' / 'x.md'  # 永不创建
    vault = tmp_path / 'V'; vault.mkdir()
    result = archive_doc(_make_pending_entry(str(nonexistent)),
                         vault_root=str(vault))
    assert result['status'] == 'original_missing'
    assert 'not exist' in result['reason'].lower()
