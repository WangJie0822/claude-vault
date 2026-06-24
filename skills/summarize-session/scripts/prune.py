"""死条目识别与清理：path 实时校验 + .bak 备份（纯逻辑模块）。

死条目判据：无 vault_path + original_missing=True + path 当前不存在。
path 实时校验避免误删"original_missing 但文件已回归"的活文件。
供 sync_pending_docs.py 与 reclaim_and_prune.py 复用。
"""
import os
import shutil
import pathlib


def is_dead_entry(entry, path_exists=os.path.exists):
    """死条目：无 vault_path + original_missing + path 当前不存在。

    path_exists 注入便于单测；判定抛 OSError 时保守视为"存在"（不误删）。
    """
    if entry.get('vault_path'):
        return False
    if not entry.get('original_missing'):
        return False
    p = entry.get('path') or ''
    if not p:
        # path 缺失/空（脏数据）→ 无法判定，保守保留（不删；
        # 且避免 os.path.exists(None) 抛 TypeError 使删除流程崩溃）
        return False
    try:
        exists = path_exists(p)
    except OSError:
        exists = True
    return not exists


def partition_dead(pending, path_exists=os.path.exists):
    """分离 (alive, dead)，各自保序。"""
    alive, dead = [], []
    for e in pending:
        (dead if is_dead_entry(e, path_exists) else alive).append(e)
    return alive, dead


def backup_pending(pending_path, keep=5):
    """轮转备份：.bak.1（最新）… .bak.<keep>（最老），超出删除。
    迁移存量旧单一 .bak → 纳入轮转（避免孤儿）。返回本次新建 bak 路径；源不存在返回 None。
    调用点须在 pending-docs.json.lock 内（sync:277 / reclaim:77 已满足），序号分配才原子。
    """
    p = pathlib.Path(pending_path)
    if not p.exists():
        return None
    base = str(p)
    legacy = base + '.bak'
    # 1) 迁移存量旧单一 .bak → .bak.1（仅当 .bak.1 空位）
    if os.path.exists(legacy) and not os.path.exists(base + '.bak.1'):
        os.rename(legacy, base + '.bak.1')
    # 2) 轮转：删最老，其余下移一位（.bak.k -> .bak.k+1）
    oldest = base + '.bak.%d' % keep
    if os.path.exists(oldest):
        os.remove(oldest)
    for i in range(keep - 1, 0, -1):
        src = base + '.bak.%d' % i
        if os.path.exists(src):
            os.rename(src, base + '.bak.%d' % (i + 1))
    # 3) 新建 .bak.1 = 当前内容
    newest = base + '.bak.1'
    shutil.copy2(base, newest)
    return newest
