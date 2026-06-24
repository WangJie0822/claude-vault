"""Markdown frontmatter 通用 upsert/delete 工具。"""

import re
from pathlib import Path


def upsert_fields(path: Path, updates: dict, deletes: tuple = ()) -> None:
    """对 path 的 frontmatter 执行:删除指定 key,再 upsert 指定 key=value。

    - updates 中的 key 若已存在则替换,否则追加
    - deletes 中的 key 若存在则移除
    - 无 frontmatter 且 updates 非空时,前置一个新块
    - frontmatter 未闭合抛 ValueError
    """
    text = path.read_text(encoding='utf-8')
    lines = text.split('\n')

    if not lines or lines[0].strip() != '---':
        if not updates:
            return
        fm_lines = [f'{k}: {_yaml_scalar(v)}' for k, v in updates.items()]
        path.write_text(
            '---\n' + '\n'.join(fm_lines) + '\n---\n' + text,
            encoding='utf-8')
        return

    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        raise ValueError(f'frontmatter 未闭合: {path}')

    fm = lines[1:end]
    body = lines[end:]

    # 删除指定 key(精确 key: value 匹配,用 regex 避免 startswith 假阳性)
    all_keys_to_remove = set(deletes) | set(updates.keys())
    key_pattern = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*):')

    def should_keep(line):
        m = key_pattern.match(line)
        return not (m and m.group(1) in all_keys_to_remove)

    fm = [l for l in fm if should_keep(l)]

    # 追加 updates
    for k, v in updates.items():
        fm.append(f'{k}: {_yaml_scalar(v)}')

    path.write_text(
        '---\n' + '\n'.join(fm) + '\n' + '\n'.join(body),
        encoding='utf-8')


def _yaml_scalar(value) -> str:
    """最小化加引号:含 YAML 特殊字符时加双引号并转义。

    list 输出 YAML inline array 风格 [a, b]（非 Python repr [' a', ' b']）。"""
    if isinstance(value, list):
        items = [_yaml_scalar(v) for v in value]
        return '[' + ', '.join(items) + ']'
    if not isinstance(value, str):
        return str(value)
    specials = set(':#\n"*&?!|>@%')
    if any(c in value for c in specials) or value.startswith(('- ', '[', '{')):
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    return value
