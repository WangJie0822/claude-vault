"""一次性脚本：先为可重建的死条目找回 Vault 副本（reclaim vault_path → 转
archived_orig_gone），再清理（prune）真死条目。默认 dry-run，--apply 实际执行。

reclaim 方法：死条目 path 的 basename 在 Vault 内唯一命中同名 .md → 设 vault_path。
"""
import os
import sys
import json
import pathlib
import argparse
import collections

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _fs import atomic_write_json, _acquire_lock, _release_lock, LOCK_TIMEOUT
from prune import partition_dead, backup_pending

_VAULT_SKIP_DIRS = {'.git', '.obsidian', '.meta', '.trash'}


def _build_vault_index(vault_root):
    """basename -> [绝对路径] 索引（跳过 .git/.obsidian/.meta/.trash）。"""
    idx = collections.defaultdict(list)
    for root, dirs, files in os.walk(vault_root):
        dirs[:] = [d for d in dirs if d not in _VAULT_SKIP_DIRS]
        for f in files:
            if f.endswith('.md'):
                idx[f].append(os.path.join(root, f))
    return idx


def reclaim_vault_paths(pending, vault_root):
    """对死条目（无 vault_path + original_missing）basename 唯一命中则重建 vault_path。

    就地修改 pending 中的 dict：设 vault_path、清 settled（旧自由文本记录已被取代），
    保留 original_missing（原文件确实不在 = archived_orig_gone）。返回 reclaimed 列表。
    """
    idx = _build_vault_index(vault_root)
    reclaimed = []
    for e in pending:
        if e.get('vault_path') or not e.get('original_missing'):
            continue
        hits = idx.get(os.path.basename(e.get('path', '')), [])
        if len(hits) == 1:
            e['vault_path'] = hits[0]
            e.pop('settled', None)
            # 只接管 vault_path；vault_mtime/size/wikilink_form 交由下次 sync apply 补齐
            # （sync 的 wikilink 计算遍历所有 vault_path 条目；该条目 src 不存在会走
            #  sync 的 original_missing 分支，在 mtime 检查之前 return，故不需要 vault_mtime/size）
            reclaimed.append(e)
    return reclaimed


def run(vault_root, pending_path, apply):
    """主入口：reclaim + partition + （apply 时）备份并写回 alive。"""
    lock_path = pending_path + '.lock'
    if not _acquire_lock(lock_path, timeout=LOCK_TIMEOUT):
        return {'error': 'lock_timeout'}
    try:
        pending = json.loads(pathlib.Path(pending_path).read_text(encoding='utf-8'))
        reclaimed = reclaim_vault_paths(pending, vault_root)
        alive, dead = partition_dead(pending)
        # path 现存但 original_missing 且无 vault_path → 活文件，留主表（既不删也未 reclaim）
        kept = [e for e in alive
                if e.get('original_missing') and not e.get('vault_path')]
        out = {
            'reclaimed': reclaimed,
            ('pruned' if apply else 'pruned_planned'): dead,
            'kept_alive_path_exists': kept,
            'total_before': len(pending),
            'total_after': len(alive),
        }
        if apply:
            if dead or reclaimed:
                backup_pending(pending_path)
            atomic_write_json(pending_path, alive)
        return out
    finally:
        _release_lock(lock_path)


def main():
    p = argparse.ArgumentParser(description='一次性 reclaim + prune pending-docs 死条目')
    p.add_argument('--vault', required=True, help='Vault 根目录绝对路径')
    p.add_argument('--pending', default=None,
                   help='pending-docs.json 路径；默认 <vault>/.meta/pending-docs.json')
    p.add_argument('--apply', action='store_true', help='实际执行；不带则 dry-run')
    args = p.parse_args()
    pending = args.pending or str(
        pathlib.Path(args.vault) / '.meta' / 'pending-docs.json')
    result = run(args.vault, pending, args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
