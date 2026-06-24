"""批处理引擎：incremental / backfill 模式 + dry-run / apply。

调用方：summarize-session SKILL.md 第四步 + /summarize-session --backfill-archive
输出：JSON 报告含 new_archived / synced / adopted / skipped_unchanged /
       conflict_vault_edited / conflict_both_edited / original_missing /
       path_invalid / denied_sensitive / errors / expired_missing / items

设计要点：
- mtime + size 双指标短路：不变直接 skipped_unchanged，避免 hash 开销
- 副本正文手工编辑检测：source_content_hash + vault_content_hash 四分支决策
- vault_content_hash 用 _sha256_body（剥离 frontmatter 后的正文 sha256），
  避免 frontmatter 改写引发的"虚假冲突"——见 archive_doc._sha256_body 注释
- pending-docs.json 写入用 tempfile + os.replace atomic rename
- 跨平台文件锁（_fs._acquire_lock）保证并发安全
"""
import os
import sys
import re
import json
import pathlib
import datetime
import argparse
from typing import Optional

# scripts/ 同级模块导入（与 archive_doc.py:27-30 同款）
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _fs import _acquire_lock, _release_lock, LOCK_TIMEOUT, atomic_write_json
from archive_doc import archive_doc, _sha256_file, _sha256_body
import prune as _prune

# 原文件失踪超过此天数 → 在 sync 输出 expired_missing 列表，
# SKILL.md 第四步会显式呈现给用户决定是否清理 pending-docs 条目。
# 阈值参考 spec L297 + 决策表 L70（90 天）。
ORIGINAL_MISSING_PROMPT_DAYS = 90


def _sync_one_entry(entry: dict, vault_root: str, apply: bool,
                    rename_on_conflict: bool, allow_adopt: bool) -> dict:
    """单条同步：返回结果 dict 含 status / vault_path / 等。

    分支：
    1. 已有 vault_path 且存在 → 同步路径：mtime/size 短路 → hash 四分支
    2. 未归集（无 vault_path 或 vp 已被删）→ 调 archive_doc 走首次归集
    """
    src = entry.get('path', '')
    vp = entry.get('vault_path')

    # 已有 vault_path 且 vault 副本存在 → 走同步分支
    if vp and os.path.exists(vp):
        # 原文件不存在 → original_missing（vault 副本保留）
        if not os.path.exists(src):
            return {'status': 'original_missing', 'path': src, 'vault_path': vp}

        # mtime/size 短路：必须**同时**满足
        # 1) src mtime + size 未变
        # 2) vault 副本 mtime + size 未变（防止用户编辑了 vault 副本但未碰 src 的场景被漏掉）
        # 任一不满足 → 重算 hash 走四分支
        # size 作为 mtime 的兜底——Windows NTFS mtime 100ns 但 Python float 精度可能丢失，
        # 用户编辑若仅追加一行，size 必然变化，可作可靠 fallback
        try:
            src_mt = os.path.getmtime(src)
            src_sz = os.path.getsize(src)
            vault_mt = os.path.getmtime(vp)
            vault_sz = os.path.getsize(vp)
        except OSError as e:
            return {'status': 'error', 'path': src, 'vault_path': vp,
                    'reason': f'stat error: {e}'}

        if (entry.get('source_mtime') == src_mt
                and entry.get('source_size') == src_sz
                and entry.get('vault_mtime') == vault_mt
                and entry.get('vault_size') == vault_sz):
            return {'status': 'skipped_unchanged', 'path': src, 'vault_path': vp,
                    'source_mtime': src_mt, 'source_size': src_sz,
                    'vault_mtime': vault_mt, 'vault_size': vault_sz}

        # mtime/size 有变 → 重算 hash 走四分支
        try:
            src_hash = _sha256_file(src)
            vault_hash = _sha256_body(vp)
        except OSError as e:
            return {'status': 'error', 'path': src, 'vault_path': vp,
                    'reason': f'hash error: {e}'}

        last_src_hash = entry.get('source_content_hash')
        last_vault_hash = entry.get('vault_content_hash')

        src_changed = src_hash != last_src_hash
        vault_changed = vault_hash != last_vault_hash

        # 分支 A：双方都没变（mtime 漂移但内容不变，如 touch）→ skipped_unchanged
        if not src_changed and not vault_changed:
            return {'status': 'skipped_unchanged', 'path': src, 'vault_path': vp,
                    'source_mtime': src_mt, 'source_size': src_sz,
                    'vault_mtime': vault_mt, 'vault_size': vault_sz,
                    'source_content_hash': src_hash,
                    'vault_content_hash': vault_hash}

        # 分支 B：仅 Vault 副本被改 → conflict_vault_edited
        if vault_changed and not src_changed:
            return {'status': 'conflict_vault_edited', 'path': src, 'vault_path': vp,
                    'reason': 'vault copy edited by user; source unchanged'}

        # 分支 C：双方都改 → conflict_both_edited
        if vault_changed and src_changed:
            return {'status': 'conflict_both_edited', 'path': src, 'vault_path': vp,
                    'reason': 'both vault copy and source edited'}

        # 分支 D：仅 src 改 → 同步覆盖
        if not apply:
            return {'status': 'synced_planned', 'path': src, 'vault_path': vp,
                    'source_content_hash': src_hash}

        # 实际写入：删旧 vault 副本，重新调 archive_doc 走完整 frontmatter 合并
        # 用 allow_adopt=False 防止 archive_doc 误进 adopt 分支（旧副本可能没有 vault_source_*，
        # 但此处我们已经知道这是受控同步，不是用户首次手工归集）
        try:
            pathlib.Path(vp).unlink()
        except OSError as e:
            return {'status': 'error', 'path': src, 'vault_path': vp,
                    'reason': f'unlink old vault copy error: {e}'}

        # 包 try/except，避免单条 archive_doc 抛异常中止整批 sync
        try:
            result = archive_doc(entry, vault_root=vault_root,
                                 allow_adopt=False,
                                 rename_on_conflict=rename_on_conflict)
        except Exception as e:
            return {'status': 'error', 'path': src, 'vault_path': vp,
                    'reason': f'archive_doc raise: {type(e).__name__}: {e}'}
        if result.get('status') == 'new_archived':
            result['status'] = 'synced'
        # 补 vault_mtime + vault_size（archive_doc 不返回，由 sync 层补齐）
        # 同时更新 vault_content_hash：archive_doc 写完正文后 upsert_fields 改写了
        # frontmatter，整文件 mtime/size 改变，但 _sha256_body 只看正文不会变；
        # 保险起见在 sync 层重新算一次确保与持久化字段对齐
        if result.get('vault_path') and os.path.exists(result['vault_path']):
            try:
                result['vault_mtime'] = os.path.getmtime(result['vault_path'])
                result['vault_size'] = os.path.getsize(result['vault_path'])
                result['vault_content_hash'] = _sha256_body(result['vault_path'])
            except OSError:
                pass
        return result

    # 未归集（无 vault_path 或 vp 已删）→ 首次归集
    if not apply:
        # dry-run：标 new_archived_planned；不实际调 archive_doc
        return {'status': 'new_archived_planned', 'path': src,
                'proposed_vault_path': '(by archive_doc)'}

    # 包 try/except，避免单条 archive_doc 抛异常中止整批 sync
    try:
        result = archive_doc(entry, vault_root=vault_root,
                             allow_adopt=allow_adopt,
                             rename_on_conflict=rename_on_conflict)
    except Exception as e:
        return {'status': 'error', 'path': src,
                'reason': f'archive_doc raise: {type(e).__name__}: {e}'}
    # 补 vault_mtime + vault_size + 重算 vault_content_hash
    if result.get('vault_path') and os.path.exists(result['vault_path']):
        try:
            result['vault_mtime'] = os.path.getmtime(result['vault_path'])
            result['vault_size'] = os.path.getsize(result['vault_path'])
            result['vault_content_hash'] = _sha256_body(result['vault_path'])
        except OSError:
            pass
    return result


def sync(vault_root: str, pending_path: str, mode: str = 'incremental',
         apply: bool = False, rename_on_conflict: bool = False,
         allow_adopt: bool = True) -> dict:
    """主入口：扫 pending-docs.json，逐条 _sync_one_entry。

    参数：
    - vault_root: Vault 根目录绝对路径
    - pending_path: pending-docs.json 路径
    - mode: 'incremental'（默认，遍历全部）/ 'backfill'（仅无 vault_path 的条目）
    - apply: False=dry-run 只输出计划；True=实际写入 Vault + 回写 pending-docs.json
    - rename_on_conflict: 透传给 archive_doc，冲突时是否自动 rename
    - allow_adopt: 透传给 archive_doc，目标存在但无 vault_source_* 时是否走 adopt

    返回 dict 含：
    - new_archived / synced / adopted / skipped_unchanged
    - conflict_vault_edited / conflict_both_edited
    - original_missing / path_invalid / denied_sensitive / errors
    - expired_missing：original_missing_since 距今 > 90 天的条目
    - items（按 pending-docs 顺序的全量 result）
    """
    lock_path = pending_path + '.lock'
    # 用 LOCK_TIMEOUT（_fs.py:36 设计为 300s 兜底 backfill 等长操作）
    # 避免硬编码 30s 导致 100+ 条 backfill 锁等待超时返回 lock_timeout
    if not _acquire_lock(lock_path, timeout=LOCK_TIMEOUT):
        return {'error': 'lock_timeout', 'items': []}

    try:
        if pathlib.Path(pending_path).exists():
            pending = json.loads(
                pathlib.Path(pending_path).read_text(encoding='utf-8'))
        else:
            pending = []

        items = []
        evicted = []  # 被 prune（apply）/ 将 prune（dry-run）的死条目
        # idx_map: items 下标 → pending 下标（mode=backfill 时跳过已归集条目，下标不连续）
        idx_map = []
        for i, entry in enumerate(pending):
            if mode == 'backfill' and entry.get('vault_path'):
                continue
            r = _sync_one_entry(entry, vault_root, apply,
                                rename_on_conflict, allow_adopt)
            # 把 path 兜底回填，便于阅读 result
            if 'path' not in r:
                r['path'] = entry.get('path', '')
            items.append(r)
            idx_map.append(i)

        # 回写 pending-docs.json（仅 apply=True 时）
        if apply:
            today_iso = datetime.date.today().isoformat()
            today_full = datetime.datetime.now().isoformat() + '+08:00'
            for k, r in enumerate(items):
                i = idx_map[k]
                status = r.get('status')
                if status in ('new_archived', 'adopted', 'synced',
                              'in_vault_short_circuit'):
                    if r.get('vault_path'):
                        pending[i]['vault_path'] = r['vault_path']
                    if r.get('source_content_hash'):
                        pending[i]['source_content_hash'] = r['source_content_hash']
                    if r.get('vault_content_hash'):
                        pending[i]['vault_content_hash'] = r['vault_content_hash']
                    if r.get('source_mtime') is not None:
                        pending[i]['source_mtime'] = r['source_mtime']
                    if r.get('source_size') is not None:
                        pending[i]['source_size'] = r['source_size']
                    if r.get('vault_mtime') is not None:
                        pending[i]['vault_mtime'] = r['vault_mtime']
                    if r.get('vault_size') is not None:
                        pending[i]['vault_size'] = r['vault_size']
                    if status in ('new_archived', 'adopted'):
                        pending[i].setdefault('archived_at', today_full)
                    pending[i]['last_synced_at'] = today_full
                    if status == 'adopted':
                        pending[i]['adopted_from_existing'] = True
                    # 清 settled（旧字段，已被 vault_path 取代）
                    pending[i].pop('settled', None)
                elif status == 'original_missing':
                    pending[i]['original_missing'] = True
                    pending[i].setdefault('original_missing_since', today_iso)
                elif status == 'path_invalid':
                    pending[i]['path_invalid'] = True
                elif status == 'denied_sensitive':
                    pending[i]['denied_sensitive'] = True
                elif status == 'skipped_unchanged':
                    if r.get('source_mtime') is not None:
                        pending[i]['source_mtime'] = r['source_mtime']
                    if r.get('source_size') is not None:
                        pending[i]['source_size'] = r['source_size']
                    if r.get('vault_mtime') is not None:
                        pending[i]['vault_mtime'] = r['vault_mtime']
                    if r.get('vault_size') is not None:
                        pending[i]['vault_size'] = r['vault_size']
                    if r.get('source_content_hash'):
                        pending[i]['source_content_hash'] = r['source_content_hash']
                    if r.get('vault_content_hash'):
                        pending[i]['vault_content_hash'] = r['vault_content_hash']

            # ===== 死条目轻量 prune（评审 H1：必须在字段回写循环之后）=====
            # 判据：无 vault_path + original_missing + path 当前不存在（活文件不删）
            alive_entries, evicted = _prune.partition_dead(pending)
            if evicted:
                _prune.backup_pending(pending_path)
            pending = alive_entries

            # wikilink_form：basename 同名时带子目录消歧
            # 统计 Vault 内 basename 出现次数
            basename_count = {}
            for d in pending:
                vp = d.get('vault_path')
                if not vp:
                    continue
                bn = pathlib.Path(vp).stem
                basename_count[bn] = basename_count.get(bn, 0) + 1
            for d in pending:
                vp = d.get('vault_path')
                if not vp:
                    continue
                bn = pathlib.Path(vp).stem
                if basename_count.get(bn, 0) > 1:
                    # 同名，带子目录消歧 → [[sub/bn|label]]
                    parts = pathlib.Path(vp).parts
                    sub = parts[-2] if len(parts) >= 2 else ''
                    label_map = {'specs': 'spec', 'plans': 'plan'}
                    label = label_map.get(sub, sub)
                    d['wikilink_form'] = f'[[{sub}/{bn}|{label}]]'
                else:
                    d['wikilink_form'] = f'[[{bn}]]'

            atomic_write_json(pending_path, pending)

        # 扫 expired_missing：original_missing_since 距今 > 90 天 → 提示清理
        # 注意必须遍历 pending 而非 items（mode=backfill 时已归集条目被过滤出 items；
        # 但 original_missing_since 标在 pending 整张表上，需要全表扫描）
        today = datetime.date.today()
        expired_missing = []
        for d in pending:
            since_str = d.get('original_missing_since')
            if not since_str:
                continue
            try:
                # 支持 ISO date 与 ISO datetime（兜底处理：截断到 date 部分）
                since = datetime.date.fromisoformat(str(since_str)[:10])
                days = (today - since).days
                if days > ORIGINAL_MISSING_PROMPT_DAYS:
                    expired_missing.append({
                        'path': d.get('path'),
                        'vault_path': d.get('vault_path'),
                        'days_missing': days,
                        'original_missing_since': since_str,
                    })
            except (ValueError, TypeError):
                # since_str 格式异常 → 静默跳过，不影响主流程
                continue

        # dry-run 预览：apply 路径已把 evicted 从 pending 删除；dry-run 路径在此算将删的死条目
        if not apply:
            _, evicted = _prune.partition_dead(pending)
        evicted_paths = {e.get('path') for e in evicted}
        # 去重（评审 M2）：被删/将删条目只出现在 pruned/pruned_planned
        items = [r for r in items if r.get('path') not in evicted_paths]
        expired_missing = [r for r in expired_missing
                           if r.get('path') not in evicted_paths]

        # 聚合分类输出
        out = {
            'new_archived': [r for r in items if r.get('status') == 'new_archived'],
            'synced': [r for r in items if r.get('status') == 'synced'],
            'adopted': [r for r in items if r.get('status') == 'adopted'],
            'skipped_unchanged': [r for r in items if r.get('status') == 'skipped_unchanged'],
            'conflict_vault_edited': [r for r in items if r.get('status') == 'conflict_vault_edited'],
            'conflict_both_edited': [r for r in items if r.get('status') == 'conflict_both_edited'],
            'original_missing': [r for r in items if r.get('status') == 'original_missing'],
            'path_invalid': [r for r in items if r.get('status') == 'path_invalid'],
            'denied_sensitive': [r for r in items if r.get('status') == 'denied_sensitive'],
            'errors': [r for r in items if r.get('status') == 'error'],
            'expired_missing': expired_missing,
            ('pruned' if apply else 'pruned_planned'): evicted,
            'items': items,
        }
        return out
    finally:
        _release_lock(lock_path)


def run_cli():
    """CLI 入口：--vault / --pending / --mode / --apply / --rename-on-conflict /
    --no-adopt / --output-json。"""
    p = argparse.ArgumentParser(
        description='批处理归集引擎：扫 pending-docs.json 同步到 Vault')
    p.add_argument('--vault', required=True, help='Vault 根目录绝对路径')
    p.add_argument('--pending', required=False, default=None,
                   help='pending-docs.json path; 默认 <vault>/.meta/pending-docs.json')
    p.add_argument('--mode', choices=['incremental', 'backfill'],
                   default='incremental')
    p.add_argument('--apply', action='store_true',
                   help='实际写入；不带则仅 dry-run')
    p.add_argument('--rename-on-conflict', action='store_true',
                   help='冲突时自动 rename 为 stem-YYYYMMDD-HHMMSS.suffix')
    p.add_argument('--no-adopt', action='store_true',
                   help='禁用 archive_doc 的 adopt 分支')
    p.add_argument('--output-json', default=None,
                   help='把 result JSON 写到指定路径；不带则 stdout')
    args = p.parse_args()

    pending = args.pending or str(
        pathlib.Path(args.vault) / '.meta' / 'pending-docs.json')
    result = sync(vault_root=args.vault, pending_path=pending,
                  mode=args.mode, apply=args.apply,
                  rename_on_conflict=args.rename_on_conflict,
                  allow_adopt=not args.no_adopt)
    if args.output_json:
        out_p = pathlib.Path(args.output_json)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding='utf-8')
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    run_cli()
