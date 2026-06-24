#!/usr/bin/env python3
"""撤销归档:从 archive/ 移回 + 清除归档字段。"""

import argparse
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _fs import git_mv_or_rename
from _frontmatter import upsert_fields


def _resolve_in_vault(vault: Path, rel_path: str) -> Path:
    """校验 rel_path 确为 vault 内相对路径,返回解析后的绝对路径。"""
    p = Path(rel_path)
    if p.is_absolute() or '..' in p.parts:
        raise ValueError(f'rel_path 必须是 vault 内相对路径,且不含 ..: {rel_path}')
    resolved = (vault / rel_path).resolve()
    try:
        resolved.relative_to(vault)
    except ValueError:
        raise ValueError(f'rel_path 逃逸 vault: {rel_path}')
    return resolved


def unarchive_note(vault, rel_path):
    vault = Path(vault).expanduser().resolve()
    rel_path = str(Path(rel_path))

    src_abs = _resolve_in_vault(vault, rel_path)
    if not src_abs.exists():
        raise FileNotFoundError(rel_path)

    parts = Path(rel_path).parts
    if 'archive' not in parts:
        raise ValueError(f'{rel_path} 不在 archive 目录')

    # 目标:去掉 archive 段
    dst_parts = [p for p in parts if p != 'archive']
    dst_rel = '/'.join(dst_parts)
    dst_abs = vault / dst_rel
    dst_abs.parent.mkdir(parents=True, exist_ok=True)

    git_mv_or_rename(vault, rel_path, dst_rel)

    # 清除归档字段 + status 改回 active
    upsert_fields(
        dst_abs,
        {'status': 'active'},
        deletes=('archived_reason', 'archived_date'),
    )


def main():
    parser = argparse.ArgumentParser(description='撤销归档')
    parser.add_argument('--vault', required=True)
    parser.add_argument('path', help='相对 Vault 根的 archive/ 路径')
    args = parser.parse_args()
    unarchive_note(args.vault, args.path)
    print(f'已撤归档: {args.path}')


if __name__ == '__main__':
    main()
