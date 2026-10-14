"""frontmatter 边界判据的唯一实现，以及单字段的精确替换。

边界口径（写端、读端 `rebuild_index.parse_frontmatter`、`_summary.fm_has_summary`、
`_summary_store.body_bytes` 共用，口径分裂过一次就会把摘要写进正文，见 2.3.0 full-review H-1）：
- 按 LF 切行（CRLF 的 CR 留在行尾，比较前 rstrip）；以 BOM 开头视为没有 frontmatter
- 首行 `strip() == "---"`；闭合行 `rstrip() == "---"`——不 lstrip：缩进的 `---` 属于块标量内容

单字段替换与 `_frontmatter.upsert_fields` 的区别（2.3.0 摘要写入只走本模块）：不前插新 frontmatter、
不改写已有行的行尾、BOM / 未闭合 / 无 frontmatter 一律返回 None，由调用方跳过并报告。
"""
from __future__ import annotations

import re

_BOM = chr(0xFEFF)
_CR = chr(13)
_LF = chr(10)
_TAB = chr(9)
_FORBIDDEN = ('"', chr(92), _LF, _CR)


def is_fm_open(line: str) -> bool:
    return line.strip() == "---"


def is_fm_close(line: str) -> bool:
    return line.rstrip() == "---"


def split_frontmatter(text: str):
    """返回 (首行, frontmatter 内各行, 闭合行及其后全部文本)；无 frontmatter / 未闭合 / BOM 返回 None。

    各行不含 LF、CRLF 文件的行尾 CR 保留在行内；三段以 LF 连接即还原原文。
    """
    if not isinstance(text, str) or text.startswith(_BOM):
        return None
    parts = text.split(_LF)
    if len(parts) < 2 or not is_fm_open(parts[0]):
        return None
    for i in range(1, len(parts)):
        if is_fm_close(parts[i]):
            return parts[0], parts[1:i], _LF.join(parts[i:])
    return None


def _key_re(key: str):
    return re.compile("^" + re.escape(key) + "[ " + _TAB + "]*:")


def _is_blank(line: str) -> bool:
    return line.strip() == ""


def _is_indented(line: str) -> bool:
    return not _is_blank(line) and line[:1] in (" ", _TAB)


def field_span(body: list, key: str) -> list:
    """key 行及其续行的下标（升序）。续行 = 缩进行；块内空行仅当其后还有缩进行时计入。"""
    kr = _key_re(key)
    owned, i, n = [], 0, len(body)
    while i < n:
        if not kr.match(body[i]):
            i += 1
            continue
        owned.append(i)
        j, pending = i + 1, []
        while j < n:
            if _is_blank(body[j]):
                pending.append(j)
            elif _is_indented(body[j]):
                owned.extend(pending)
                pending = []
                owned.append(j)
            else:
                break
            j += 1
        i = j
    return owned


def _strip_cr(line: str) -> str:
    return line[:-1] if line.endswith(_CR) else line


def get_field_lines(text: str, key: str):
    parts = split_frontmatter(text)
    if parts is None:
        return None
    body = parts[1]
    return [_strip_cr(body[k]) for k in field_span(body, key)]


def replace_field_lines(text: str, key: str, new_lines: list):
    parts = split_frontmatter(text)
    if parts is None:
        return None
    first, body, close_rest = parts
    cr = _CR if first.endswith(_CR) else ""
    owned = set(field_span(body, key))
    kept = [ln for k, ln in enumerate(body) if k not in owned]
    kept.extend(ln + cr for ln in new_lines)
    return _LF.join([first, *kept, close_rest])


def set_scalar_field(text: str, key: str, value):
    if value is None:
        return replace_field_lines(text, key, [])
    if any(ch in value for ch in _FORBIDDEN):
        raise ValueError("value contains a forbidden character")
    return replace_field_lines(text, key, [f'{key}: "{value}"'])
