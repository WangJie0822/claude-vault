"""手动清理已归集且超龄的 pending-docs 跟踪条目（修复 pending-docs.json 只增不减）。

背景：`prune.is_dead_entry` 只清理「无 vault_path + 原文失踪」的死条目；任何**已归集**
（有 vault_path）的条目一律保留，导致 pending-docs.json 永不收缩、只增不减。本脚本补上
另一半——已归集条目的原文已安全在 Vault，其 pending 跟踪记录在归集若干天后即可弃。

判据：有 `vault_path` 且 `archived_at` 解析出的时间距今 **严格超过** N 天 → 淘汰。
无 vault_path（未归集，归 reclaim_and_prune 管）、无/非法 archived_at（无法判龄）→ 保守保留。

设计约束（按需求）：独立手动命令，**不接入 sync、无 config、无自动触发**。
默认 dry-run，--apply 才写回（写前 .bak 轮转备份，复用 prune.backup_pending）。
"""
import sys
import time
import json
import pathlib
import argparse
import datetime

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _fs import atomic_write_json, _acquire_lock, _release_lock, LOCK_TIMEOUT
from prune import backup_pending

DEFAULT_OLDER_THAN_DAYS = 30


def _parse_ts(s):
    """ISO 时间字符串 → epoch 秒；解析失败返回 None。

    生产 archived_at 形如 `2026-06-30T20:05:00.1+08:00`（带 +08:00）；
    历史数据可能为无时区裸 ISO。两者 fromisoformat 均可解析，
    `.timestamp()` 对 tz-aware 给正确 epoch、对 naive 按本地时区折算。
    """
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    try:
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def is_aged_archived(entry, now_epoch, older_than_days):
    """已归集（有 vault_path）且 archived_at 距 now 严格超过 older_than_days 天。

    now_epoch 注入便于单测；无 vault_path / 无 archived_at / 时间戳非法 → False（保守保留）。
    """
    if not entry.get('vault_path'):
        return False
    ts = _parse_ts(entry.get('archived_at'))
    if ts is None:
        return False
    age_days = (now_epoch - ts) / 86400.0
    return age_days > older_than_days


def partition_aged_archived(pending, now_epoch, older_than_days):
    """分离 (keep, aged)，各自保序。"""
    keep, aged = [], []
    for e in pending:
        (aged if is_aged_archived(e, now_epoch, older_than_days) else keep).append(e)
    return keep, aged


def run(pending_path, older_than_days=DEFAULT_OLDER_THAN_DAYS, apply=False, now_epoch=None):
    """主入口：分离超龄已归集条目；apply 时备份并写回 keep。

    now_epoch 注入便于单测；默认取当下。缺 pending 文件返回 note=pending_not_found（不报错）。
    """
    if now_epoch is None:
        now_epoch = time.time()
    key = 'pruned' if apply else 'pruned_planned'
    pp = pathlib.Path(pending_path)
    if not pp.exists():
        return {'older_than_days': older_than_days, key: [],
                'total_before': 0, 'total_after': 0, 'note': 'pending_not_found'}
    lock_path = str(pending_path) + '.lock'
    if not _acquire_lock(lock_path, timeout=LOCK_TIMEOUT):
        return {'error': 'lock_timeout'}
    try:
        pending = json.loads(pp.read_text(encoding='utf-8'))
        keep, aged = partition_aged_archived(pending, now_epoch, older_than_days)
        out = {
            'older_than_days': older_than_days,
            key: aged,
            'total_before': len(pending),
            'total_after': len(keep),
        }
        if apply and aged:
            backup_pending(pending_path)
            atomic_write_json(pending_path, keep)
        return out
    finally:
        _release_lock(lock_path)


def main():
    p = argparse.ArgumentParser(
        description='手动清理已归集且超龄的 pending-docs 跟踪条目（默认 dry-run）')
    p.add_argument('--vault', required=True, help='Vault 根目录绝对路径')
    p.add_argument('--pending', default=None,
                   help='pending-docs.json 路径；默认 <vault>/.meta/pending-docs.json')
    p.add_argument('--older-than', type=int, default=DEFAULT_OLDER_THAN_DAYS,
                   metavar='N', help='淘汰归集超过 N 天的条目（默认 %d）' % DEFAULT_OLDER_THAN_DAYS)
    p.add_argument('--apply', action='store_true', help='实际执行；不带则 dry-run')
    args = p.parse_args()
    pending = args.pending or str(
        pathlib.Path(args.vault) / '.meta' / 'pending-docs.json')
    result = run(pending, older_than_days=args.older_than, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
