#!/usr/bin/env python3
"""归档一篇笔记:git mv 到 <category>/archive/ + frontmatter 改写。"""

import argparse
import sys
from datetime import date as _date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _fs import git_mv_or_rename
from _frontmatter import upsert_fields


def _resolve_in_vault(vault: Path, rel_path: str) -> Path:
    """校验 rel_path 确为 vault 内相对路径,返回解析后的绝对路径。

    拒绝:
    - 绝对路径
    - 含 '..' 的相对路径
    - 解析后逃逸 vault 根目录的路径
    """
    p = Path(rel_path)
    if p.is_absolute() or '..' in p.parts:
        raise ValueError(f'rel_path 必须是 vault 内相对路径,且不含 ..: {rel_path}')
    resolved = (vault / rel_path).resolve()
    try:
        resolved.relative_to(vault)
    except ValueError:
        raise ValueError(f'rel_path 逃逸 vault: {rel_path}')
    return resolved


def archive_note(vault, rel_path, reason, date=None):
    vault = Path(vault).expanduser().resolve()
    rel_path = str(Path(rel_path))

    # 已在 archive 目录下的笔记不得再次归档
    if 'archive' in Path(rel_path).parts:
        raise ValueError(f'{rel_path} 已在 archive 目录,拒绝重复归档')

    # 路径遍历校验
    src = _resolve_in_vault(vault, rel_path)
    if not src.exists():
        raise FileNotFoundError(rel_path)

    archive_date = date if date is not None else str(_date.today())

    # 目标:<category>/archive/<filename>(保留 subcategory 原名)
    parts = Path(rel_path).parts
    category = parts[0]
    name = parts[-1]
    dst_rel = f'{category}/archive/{name}'
    dst_abs = vault / dst_rel
    dst_abs.parent.mkdir(parents=True, exist_ok=True)

    git_mv_or_rename(vault, rel_path, dst_rel)

    upsert_fields(dst_abs, {
        'status': 'archived',
        'archived_reason': reason,
        'archived_date': archive_date,
    })


def main():
    parser = argparse.ArgumentParser(description='归档一篇笔记')
    parser.add_argument('--vault', required=True)
    parser.add_argument('path', help='相对 Vault 根的笔记路径')
    parser.add_argument('--reason', required=True)
    parser.add_argument('--date', default=None,
                        help='归档日期 YYYY-MM-DD(默认今日)')
    args = parser.parse_args()
    archive_note(args.vault, args.path, args.reason, date=args.date)
    print(f'已归档: {args.path}')


if __name__ == '__main__':
    main()
