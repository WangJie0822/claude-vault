"""keywords 字段的写期口径（唯一权威实现）。

三条写入路径共用本模块，保证口径一致——口径分裂过一次就再也对不齐：
- `enrich_keywords.py`  付费 backfill（LLM 生成 → sanitize → 写回）
- `archive_doc.py`      spec/plan 归集（pending-docs.json 的 keywords 字段透传）
- `keywords_gap.py`     summarize-session 流程内的缺口补全

读端（vault-loader `_frontmatter_reader.load_cache`）**不复用本模块**，另有更粗的
防御网（只剔单字）。两端独立是刻意的：读端要能容忍任意历史写入，不能因写端
口径演进而把存量笔记判为非法。见 `references/note-format.md`。
"""
from __future__ import annotations

import re
import unicodedata

_YAML_META = set(":[]{}#&*!|>'\"%@`\\,")
_MAX_KEYWORDS = 8
_CJK = re.compile(r"[一-鿿]")


def _is_unsafe_char(c: str) -> bool:
    """YAML 元字符 / 控制字符（C*，含 \\n \\r \\t \\x00）/ 行段分隔符（U+2028 Zl、U+2029 Zp）一律拒。
    普通空格（Zs）不拒——它不破坏 YAML flow 标量。"""
    cat = unicodedata.category(c)
    return c in _YAML_META or cat[0] == "C" or cat in ("Zl", "Zp")


def sanitize_keywords(raw) -> list[str]:
    """质量+安全校验：剔非法字符/换行，长度约束（CJK≥2、ASCII≥3），去重，上限 8。

    非 list 输入（None / str / dict / 数字）一律返回 []——调用方可无条件调用，
    不必先自己判类型。这一点对 archive_doc 尤其重要：那里的入参来自
    pending-docs.json，是 LLM 手填的，任何类型都可能出现。
    """
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, str):
            continue
        k = item.strip()
        if not k or any(_is_unsafe_char(c) for c in k):
            continue
        has_cjk = bool(_CJK.search(k))
        min_len = 2 if has_cjk else 3
        if len(k) < min_len:
            continue
        if k not in out:
            out.append(k)
        if len(out) >= _MAX_KEYWORDS:
            break
    return out


def build_frontmatter_with_keywords(text: str, keywords: list[str]) -> str | None:
    """把 keywords 安全序列化进现有 frontmatter（已有则替换）。无 frontmatter 返回 None。

    容忍 CRLF（\\r\\n）与 LF 两种行尾，避免 CRLF 笔记被静默跳过；写回时沿用
    原文件的行尾，不做归一——归一会让整个文件在 git diff 里全行变更。
    """
    m = re.match(r"^(---\r?\n)(.*?)(\r?\n---\r?\n?)", text, re.DOTALL)
    if not m:
        return None
    head, body, tail = m.group(1), m.group(2), m.group(3)
    rest = text[m.end():]
    nl = "\r\n" if head.endswith("\r\n") else "\n"
    kw_line = "keywords: [" + ", ".join(keywords) + "]"
    body_no_kw = re.sub(r"^keywords:.*$", "", body, flags=re.MULTILINE).rstrip("\r\n")
    new_body = body_no_kw + nl + kw_line
    return head + new_body + tail + rest
