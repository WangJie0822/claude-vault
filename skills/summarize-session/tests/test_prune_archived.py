"""tests for scripts/prune_archived.py — 手动清理已归集超龄 pending-docs 条目。

只针对**已归集**（有 vault_path）且 archived_at 超 N 天的跟踪条目（原文已安全在
Vault，跟踪记录可弃）。修复 pending-docs.json 只增不减：原 prune.is_dead_entry
对任何有 vault_path 的条目直接保留，已归集条目永不清理。
"""
import sys
import json
import datetime
import pathlib

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from prune_archived import is_aged_archived, partition_aged_archived, run

# 固定 now = 2026-06-30T12:00:00+08:00 的 epoch（测试确定性，不依赖真实当下）
_TZ8 = datetime.timezone(datetime.timedelta(hours=8))
_NOW = datetime.datetime(2026, 6, 30, 12, 0, 0, tzinfo=_TZ8)
_NOW_EPOCH = _NOW.timestamp()


def _iso(days_ago):
    """生成 days_ago 天前的 archived_at 字符串（带 +08:00，与生产格式一致）。"""
    return (_NOW - datetime.timedelta(days=days_ago)).isoformat()


# ---- is_aged_archived ----

def test_aged_archived_is_aged():
    e = {'vault_path': '/V/a.md', 'archived_at': _iso(40)}
    assert is_aged_archived(e, _NOW_EPOCH, 30) is True


def test_recent_archived_not_aged():
    e = {'vault_path': '/V/a.md', 'archived_at': _iso(10)}
    assert is_aged_archived(e, _NOW_EPOCH, 30) is False


def test_no_vault_path_not_aged():
    # 未归集（无 vault_path）→ 不归本命令管（死条目由 reclaim_and_prune 处理）
    e = {'path': '/x/a.md', 'archived_at': _iso(40)}
    assert is_aged_archived(e, _NOW_EPOCH, 30) is False


def test_no_archived_at_not_aged():
    # 有 vault_path 但无时间戳 → 无法判龄 → 保守保留
    e = {'vault_path': '/V/a.md'}
    assert is_aged_archived(e, _NOW_EPOCH, 30) is False


def test_invalid_archived_at_not_aged():
    e = {'vault_path': '/V/a.md', 'archived_at': 'not-a-date'}
    assert is_aged_archived(e, _NOW_EPOCH, 30) is False


def test_boundary_exactly_n_days_not_aged():
    # 恰好 N 天（不严格超过）→ 不删（判据是严格 > N）
    e = {'vault_path': '/V/a.md', 'archived_at': _iso(30)}
    assert is_aged_archived(e, _NOW_EPOCH, 30) is False


def test_naive_archived_at_parses():
    # 无时区的 ISO（历史数据可能不带 +08:00）也能解析判龄
    naive = (_NOW.replace(tzinfo=None) - datetime.timedelta(days=40)).isoformat()
    e = {'vault_path': '/V/a.md', 'archived_at': naive}
    assert is_aged_archived(e, _NOW_EPOCH, 30) is True


# ---- partition_aged_archived ----

def test_partition_preserves_order_and_splits():
    pending = [
        {'vault_path': '/V/a.md', 'archived_at': _iso(40)},   # aged
        {'vault_path': '/V/b.md', 'archived_at': _iso(5)},    # keep（近期）
        {'path': '/x/c.md'},                                  # keep（未归集）
        {'vault_path': '/V/d.md', 'archived_at': _iso(100)},  # aged
    ]
    keep, aged = partition_aged_archived(pending, _NOW_EPOCH, 30)
    assert [e['vault_path'] for e in aged] == ['/V/a.md', '/V/d.md']
    assert keep == [pending[1], pending[2]]


# ---- run ----

def test_run_dry_run_no_write(tmp_path):
    pp = tmp_path / 'pending-docs.json'
    pp.write_text(json.dumps([
        {'vault_path': '/V/a.md', 'archived_at': _iso(40)},
        {'vault_path': '/V/b.md', 'archived_at': _iso(5)},
    ]), encoding='utf-8')
    out = run(str(pp), older_than_days=30, apply=False, now_epoch=_NOW_EPOCH)
    assert len(out['pruned_planned']) == 1
    assert out['total_before'] == 2 and out['total_after'] == 1
    # 未写：文件不变
    data = json.loads(pp.read_text(encoding='utf-8'))
    assert len(data) == 2


def test_run_apply_prunes_with_backup(tmp_path):
    pp = tmp_path / 'pending-docs.json'
    pp.write_text(json.dumps([
        {'vault_path': '/V/a.md', 'archived_at': _iso(40)},
        {'vault_path': '/V/b.md', 'archived_at': _iso(5)},
    ]), encoding='utf-8')
    out = run(str(pp), older_than_days=30, apply=True, now_epoch=_NOW_EPOCH)
    assert len(out['pruned']) == 1
    data = json.loads(pp.read_text(encoding='utf-8'))
    assert len(data) == 1
    assert data[0]['vault_path'] == '/V/b.md'
    assert pathlib.Path(str(pp) + '.bak.1').exists()


def test_run_apply_nothing_aged_no_backup(tmp_path):
    pp = tmp_path / 'pending-docs.json'
    pp.write_text(json.dumps([
        {'vault_path': '/V/b.md', 'archived_at': _iso(5)},
    ]), encoding='utf-8')
    out = run(str(pp), older_than_days=30, apply=True, now_epoch=_NOW_EPOCH)
    assert len(out['pruned']) == 0
    assert not pathlib.Path(str(pp) + '.bak.1').exists()
    data = json.loads(pp.read_text(encoding='utf-8'))
    assert len(data) == 1


def test_run_missing_pending(tmp_path):
    pp = tmp_path / 'nope.json'
    out = run(str(pp), older_than_days=30, apply=True, now_epoch=_NOW_EPOCH)
    assert out['pruned'] == []
    assert out['note'] == 'pending_not_found'
