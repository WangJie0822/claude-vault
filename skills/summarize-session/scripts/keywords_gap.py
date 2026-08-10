#!/usr/bin/env python3
"""keywords 缺口的查询与补全 —— 供 /summarize-session 流程内联调用。

与 `enrich_keywords.py` 的分工（两者不重复、不互相替代）：
- `enrich_keywords.py`：**付费**一次性 backfill，自己 spawn `claude -p --model haiku`
  逐篇生成，手动 opt-in，用于清存量。
- 本脚本：**零额外模型调用**。会话里的 LLM 本来就在跑，它读笔记、自己想 keywords，
  再调 `--set` 写回。用于 /summarize-session 每次收尾时补掉增量缺口。

缺口判定复用 `rebuild_index._health_check`，不另写判据——这是刻意的：
覆盖率统计（rebuild_index 报告）与补全清单（本脚本）一旦各算各的就会漂移，
出现「报告说缺 5 篇、补全只看到 3 篇」这种查不出来的偏差。

用法：
  keywords_gap.py --vault <path> --list [--limit N]
  keywords_gap.py --vault <path> --set <path> --keywords "词1,词2,词3"
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _keywords import build_frontmatter_with_keywords, sanitize_keywords


def load_entries(vault: pathlib.Path, cache_path: pathlib.Path | None = None) -> dict:
    """读 frontmatter-cache 的 entries。

    cache 由 rebuild_index.py 写；本脚本在 /summarize-session 流程里总是排在
    rebuild_index 之后，故 cache 是当次最新的。cache 缺失时返回 None 让调用方
    报错而非静默返回空——空清单与「真的没有缺口」无法区分，正是最该避免的假通过。
    """
    cp = cache_path or (vault / '.meta' / 'frontmatter-cache.json')
    if not cp.is_file():
        return None
    try:
        with cp.open(encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    entries = data.get('entries')
    return entries if isinstance(entries, dict) else None


def find_missing(vault: pathlib.Path, entries: dict) -> list[str]:
    """返回缺 keywords 的笔记相对路径列表，口径 == rebuild_index 的覆盖率分子。"""
    from rebuild_index import _health_check
    issues = _health_check(entries, vault, indexes_written=[])
    return list(issues.get('keywords_missing', []))


def resolve_in_vault(vault: pathlib.Path, target: str) -> pathlib.Path | None:
    """把 target（相对 vault 或绝对）解析成 vault 内的真实路径；越界返回 None。

    与 enrich_keywords 的写回同款约束：resolve 之后必须仍在 vault 内，
    挡住 `../` 穿越与 symlink 逃逸。
    """
    p = pathlib.Path(target)
    if not p.is_absolute():
        p = vault / p
    try:
        rp = p.resolve()
        vr = vault.resolve()
    except OSError:
        return None
    if vr != rp and vr not in rp.parents:
        return None
    return rp if rp.is_file() else None


def set_keywords(vault: pathlib.Path, target: str, raw_keywords: list) -> dict:
    """给单篇笔记写 keywords。返回 dict 含 status/reason。"""
    kws = sanitize_keywords(raw_keywords)
    if not kws:
        return {'status': 'skipped', 'reason': 'no valid keywords after sanitize',
                'path': target}
    rp = resolve_in_vault(vault, target)
    if rp is None:
        return {'status': 'error', 'reason': 'path outside vault or not a file',
                'path': target}
    try:
        text = rp.read_text(encoding='utf-8')
    except OSError as e:
        return {'status': 'error', 'reason': f'read error: {e}', 'path': target}
    new_text = build_frontmatter_with_keywords(text, kws)
    if new_text is None:
        return {'status': 'skipped', 'reason': 'no frontmatter', 'path': target}
    if new_text == text:
        return {'status': 'skipped', 'reason': 'unchanged', 'path': target}
    try:
        from _fs import atomic_write_text
        atomic_write_text(str(rp), new_text)
    except (ImportError, OSError) as e:
        return {'status': 'error', 'reason': f'write error: {e}', 'path': target}
    rel = str(rp.relative_to(vault.resolve())).replace('\\', '/')
    synced = _sync_cache_entry(vault, rel, kws)
    return {'status': 'ok', 'keywords': kws, 'path': rel, 'cache_synced': synced}


def _sync_cache_entry(vault: pathlib.Path, rel: str, kws: list[str]) -> bool:
    """把新 keywords 同步进 frontmatter-cache 的对应 entry。返回是否同步成功。

    **这一步不是优化，是正确性必需**：rebuild_index 的增量判据是
    `int(md_file.stat().st_mtime)`（`rebuild_index.py:163`，秒级截断），而本脚本
    写完之后通常在同一秒内就跑 rebuild_index —— 它会判定文件未变、跳过重读，
    cache 于是永远停在「无 keywords」。后果是补了等于白补：vault-loader 读的是
    cache，召回拿不到 keywords，而下次会话的报告仍显示缺失、再补一遍。

    实测（2026-08-10）：写入前后文件 mtime 为 ...860.0094 → ...860.1227，
    int() 同为 1786345860；二次 rebuild 后 cache 里 keywords 仍是 None。

    fail-open：cache 同步失败不让 --set 失败 —— 笔记正文已经写对了，
    cache 迟早会被下一次跨秒的 rebuild 修正。但要在返回值里如实报出。
    """
    cp = vault / '.meta' / 'frontmatter-cache.json'
    if not cp.is_file():
        return False
    try:
        with cp.open(encoding='utf-8') as f:
            data = json.load(f)
        entry = data.get('entries', {}).get(rel)
        if not isinstance(entry, dict):
            # cache 里没有这条（新笔记）→ 无需同步：rebuild 见 cached=None 必然重读
            return False
        entry['keywords'] = kws
        try:
            entry['mtime'] = int((vault / rel).stat().st_mtime)
        except OSError:
            pass
        from _fs import atomic_write_text
        atomic_write_text(str(cp), json.dumps(data, ensure_ascii=False))
    except (OSError, ValueError, ImportError):
        return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='keywords 缺口查询与补全')
    ap.add_argument('--vault', required=True)
    action = ap.add_mutually_exclusive_group(required=True)
    action.add_argument('--list', action='store_true', dest='do_list',
                        help='列出缺 keywords 的笔记（口径同 rebuild_index 覆盖率）')
    action.add_argument('--set', dest='set_path',
                        help='给指定笔记写 keywords，需配合 --keywords')
    ap.add_argument('--limit', type=int, default=0,
                    help='--list 时最多返回几条（0=不限）。total 字段始终是全量计数')
    ap.add_argument('--keywords', default='',
                    help='--set 时的词，逗号分隔')
    args = ap.parse_args(argv)

    vault = pathlib.Path(args.vault).expanduser()
    if not vault.is_dir():
        print(json.dumps({'status': 'error', 'reason': f'vault not found: {vault}'},
                         ensure_ascii=False))
        return 1

    if args.do_list:
        entries = load_entries(vault)
        if entries is None:
            print(json.dumps(
                {'status': 'error',
                 'reason': 'frontmatter-cache.json 缺失或损坏，请先运行 rebuild_index.py'},
                ensure_ascii=False))
            return 1
        missing = find_missing(vault, entries)
        shown = missing[:args.limit] if args.limit else missing
        print(json.dumps({'status': 'ok', 'total': len(missing),
                          'missing': shown}, ensure_ascii=False))
        return 0

    if not args.keywords.strip():
        print(json.dumps({'status': 'error', 'reason': '--set 需要 --keywords'},
                         ensure_ascii=False))
        return 1
    raw = [k.strip() for k in args.keywords.split(',')]
    result = set_keywords(vault, args.set_path, raw)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('ok', 'skipped') else 1


if __name__ == '__main__':
    sys.exit(main())
