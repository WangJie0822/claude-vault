#!/usr/bin/env python3
"""一次性 backfill：给无 keywords 的笔记生成扩展词写回 frontmatter。

调 `claude -p --model haiku`，含安全约束：
- 子进程 argv-list + stdin 传入笔记内容（不进 argv）、shell=False、timeout
- 模型返回 keyword 经 sanitize（拒 YAML 元字符/换行、长度约束、上限 8）
- 写回目标 = 扫描所得文件绝对路径（非 frontmatter 派生），resolve 落在 vault 内
- 失败/非法 → 跳过该篇、原文不动
手动 opt-in，不接 SessionEnd 自动管线。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_YAML_META = set(":[]{}#&*!|>'\"%@`\\")
_MAX_KEYWORDS = 8
_TIMEOUT = 60
_CJK = re.compile(r"[一-鿿]")


def sanitize_keywords(raw) -> list[str]:
    """质量+安全校验：剔非法字符/换行，长度约束（CJK≥2、ASCII≥3），去重，上限 8。"""
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, str):
            continue
        k = item.strip()
        if not k or "\n" in k or any(c in _YAML_META for c in k):
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


def _call_claude(content: str) -> str | None:
    """调 claude -p --model haiku，笔记内容经 stdin 传入。失败返回 None。"""
    prompt = (
        "为下面这篇笔记生成 3-8 个中文/英文检索扩展词（同义词、别名、跨语言术语），"
        "只输出 JSON：{\"keywords\": [...]}。笔记：\n"
    )
    env = dict(os.environ)
    env["VAULT_LOADER_DISABLE"] = "1"
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", "haiku"],
            input=prompt + content,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_TIMEOUT, env=env, shell=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _build_frontmatter_with_keywords(text: str, keywords: list[str]) -> str | None:
    """把 keywords 安全序列化进现有 frontmatter（已有则替换）。无 frontmatter 返回 None。"""
    m = re.match(r"^(---\n)(.*?)(\n---\n?)", text, re.DOTALL)
    if not m:
        return None
    head, body, tail = m.group(1), m.group(2), m.group(3)
    rest = text[m.end():]
    kw_line = "keywords: [" + ", ".join(keywords) + "]"
    body_no_kw = re.sub(r"^keywords:.*$", "", body, flags=re.MULTILINE).rstrip("\n")
    new_body = body_no_kw + "\n" + kw_line
    return head + new_body + tail + rest


def enrich_note(note_path: Path, model_output: str) -> bool:
    """解析模型输出、sanitize、写回。非法/无变更返回 False（原文不动）。"""
    try:
        data = json.loads(model_output)
    except (json.JSONDecodeError, TypeError):
        return False
    keywords = sanitize_keywords(data.get("keywords") if isinstance(data, dict) else None)
    if not keywords:
        return False
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return False
    new_text = _build_frontmatter_with_keywords(text, keywords)
    if new_text is None or new_text == text:
        return False
    try:
        from _fs import atomic_write_text
        atomic_write_text(str(note_path), new_text)
    except Exception:
        return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="给无 keywords 的笔记 backfill 扩展词")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(f"vault 不存在: {vault}", file=sys.stderr)
        return 1

    done = 0
    for note in vault.rglob("*.md"):
        if any(p in {".meta", ".obsidian", ".git", ".trash"} for p in note.relative_to(vault).parts):
            continue
        rp = note.resolve()
        if vault not in rp.parents:
            continue
        try:
            text = note.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(r"^keywords:", text, re.MULTILINE):
            continue
        if args.limit and done >= args.limit:
            break
        if args.dry_run:
            print(f"[dry-run] 待 enrich: {note.relative_to(vault)}")
            done += 1
            continue
        out = _call_claude(text)
        if out is None:
            print(f"跳过（claude 失败/缺失）: {note.relative_to(vault)}", file=sys.stderr)
            continue
        if enrich_note(rp, out):
            done += 1
            print(f"已 enrich: {note.relative_to(vault)}")
        else:
            print(f"跳过（校验不过）: {note.relative_to(vault)}", file=sys.stderr)
    print(json.dumps({"enriched": done}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
