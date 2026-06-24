"""tests for scripts/reclaim_and_prune.py"""
import sys
import json
import pathlib

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from reclaim_and_prune import reclaim_vault_paths, run


def test_unique_basename_hit_sets_vault_path(tmp_path):
    vault = tmp_path / 'V'; vault.mkdir()
    (vault / 'sub').mkdir()
    (vault / 'sub' / 'doc.md').write_text('# x', encoding='utf-8')
    pending = [{'path': '/old/doc.md', 'original_missing': True, 'settled': 'old note'}]
    reclaimed = reclaim_vault_paths(pending, str(vault))
    assert len(reclaimed) == 1
    assert pending[0]['vault_path'] == str(vault / 'sub' / 'doc.md')
    assert 'settled' not in pending[0]          # 旧自由文本归集记录被 vault_path 取代
    assert pending[0].get('original_missing')    # 保留：原文件确实不在 = archived_orig_gone


def test_no_hit_skips(tmp_path):
    vault = tmp_path / 'V'; vault.mkdir()
    pending = [{'path': '/old/missing.md', 'original_missing': True}]
    assert reclaim_vault_paths(pending, str(vault)) == []
    assert 'vault_path' not in pending[0]


def test_multi_hit_skips(tmp_path):
    vault = tmp_path / 'V'; vault.mkdir()
    (vault / 'a').mkdir(); (vault / 'b').mkdir()
    (vault / 'a' / 'dup.md').write_text('x', encoding='utf-8')
    (vault / 'b' / 'dup.md').write_text('y', encoding='utf-8')
    pending = [{'path': '/old/dup.md', 'original_missing': True}]
    assert reclaim_vault_paths(pending, str(vault)) == []


def test_skips_entry_with_vault_path(tmp_path):
    vault = tmp_path / 'V'; vault.mkdir()
    (vault / 'doc.md').write_text('x', encoding='utf-8')
    pending = [{'path': '/old/doc.md', 'vault_path': '/V/doc.md', 'original_missing': True}]
    assert reclaim_vault_paths(pending, str(vault)) == []


def test_index_skips_meta_and_git(tmp_path):
    """.git/.meta/.obsidian/.trash 内的同名文件不参与命中。"""
    vault = tmp_path / 'V'; vault.mkdir()
    (vault / '.meta').mkdir()
    (vault / '.meta' / 'doc.md').write_text('x', encoding='utf-8')
    pending = [{'path': '/old/doc.md', 'original_missing': True}]
    assert reclaim_vault_paths(pending, str(vault)) == []  # 只在 .meta 里 → 不算命中


def test_run_apply_reclaims_and_prunes_with_backup(tmp_path):
    vault = tmp_path / 'V'; vault.mkdir()
    (vault / 'keep.md').write_text('x', encoding='utf-8')
    pending_path = tmp_path / 'pending-docs.json'
    pending_path.write_text(json.dumps([
        {'path': '/old/keep.md', 'original_missing': True},   # reclaim
        {'path': '/old/gone.md', 'original_missing': True},   # prune（无 Vault 副本）
    ]), encoding='utf-8')
    out = run(str(vault), str(pending_path), apply=True)
    assert len(out['reclaimed']) == 1
    assert len(out['pruned']) == 1
    data = json.loads(pending_path.read_text(encoding='utf-8'))
    assert len(data) == 1
    assert data[0]['path'] == '/old/keep.md' and data[0]['vault_path']
    assert pathlib.Path(str(pending_path) + '.bak.1').exists()


def test_run_dry_run_no_write(tmp_path):
    vault = tmp_path / 'V'; vault.mkdir()
    pending_path = tmp_path / 'pending-docs.json'
    pending_path.write_text(json.dumps([
        {'path': '/old/gone.md', 'original_missing': True},
    ]), encoding='utf-8')
    out = run(str(vault), str(pending_path), apply=False)
    assert len(out['pruned_planned']) == 1
    # 未写：原文件条目仍在
    data = json.loads(pending_path.read_text(encoding='utf-8'))
    assert len(data) == 1


def test_run_keeps_alive_path_exists(tmp_path):
    """path 现存的 original_missing 条目 → kept_alive_path_exists，不删不 reclaim。"""
    vault = tmp_path / 'V'; vault.mkdir()
    live = tmp_path / 'live.md'; live.write_text('x', encoding='utf-8')
    pending_path = tmp_path / 'pending-docs.json'
    pending_path.write_text(json.dumps([
        {'path': str(live), 'original_missing': True},
    ]), encoding='utf-8')
    out = run(str(vault), str(pending_path), apply=True)
    assert len(out['pruned']) == 0
    assert len(out['kept_alive_path_exists']) == 1
    data = json.loads(pending_path.read_text(encoding='utf-8'))
    assert len(data) == 1
