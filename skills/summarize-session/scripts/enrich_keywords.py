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
import shutil
import subprocess
import sys
from pathlib import Path

# sanitize / frontmatter 写入的权威实现在 _keywords.py——归集（archive_doc）与
# 流程补全（keywords_gap）共用同一口径。此处导入同时起 re-export 作用，保持
# `enrich_keywords.sanitize_keywords` 这一既有引用面（含测试）不变。
from _keywords import build_frontmatter_with_keywords, sanitize_keywords

_TIMEOUT = 60


def _call_claude(content: str) -> str | None:
    """调 claude -p --model haiku，笔记内容经 stdin 传入。失败返回 None。"""
    prompt = (
        "为下面这篇笔记生成 3-8 个中文/英文检索扩展词（同义词、别名、跨语言术语），"
        "只输出 JSON：{\"keywords\": [...]}。笔记：\n"
    )
    claude = shutil.which("claude")
    if claude is None:
        return None
    env = dict(os.environ)
    env["VAULT_LOADER_DISABLE"] = "1"
    try:
        r = subprocess.run(
            [claude, "-p", "--model", "haiku"],
            input=prompt + content,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_TIMEOUT, env=env, shell=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _extract_json(text) -> str | None:
    """从 claude 输出剥 ```json 围栏 / 提取首个 {...}，容忍前后缀文字。"""
    if not isinstance(text, str):
        return None
    # 贪婪 \{.*\}：捕获围栏内首 { 到末 }，容忍嵌套对象（非贪婪 .*? 会截到首个 } 致嵌套失败）
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j != -1 and j > i:
        return text[i:j + 1]
    return None


def enrich_note(note_path: Path, model_output: str) -> bool:
    """解析模型输出、sanitize、写回。非法/无变更返回 False（原文不动）。"""
    raw_json = _extract_json(model_output)
    if raw_json is None:
        return False
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return False
    keywords = sanitize_keywords(data.get("keywords") if isinstance(data, dict) else None)
    if not keywords:
        return False
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return False
    new_text = build_frontmatter_with_keywords(text, keywords)
    if new_text is None or new_text == text:
        return False
    try:
        from _fs import atomic_write_text
    except ImportError:
        print(f"[enrich] _fs 不可用，无法写回: {note_path}", file=sys.stderr)
        return False
    try:
        atomic_write_text(str(note_path), new_text)
    except OSError as exc:
        print(f"[enrich] 写回失败 {note_path}: {exc}", file=sys.stderr)
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

    processed = 0   # 受 --limit 约束：dry-run=候选篇、real=已发起 claude 调用篇
    enriched = 0
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
        if args.limit and processed >= args.limit:
            break
        if args.dry_run:
            print(f"[dry-run] 待 enrich: {note.relative_to(vault)}")
            processed += 1
            continue
        processed += 1
        out = _call_claude(text)
        if out is None:
            print(f"跳过（claude 失败/缺失）: {note.relative_to(vault)}", file=sys.stderr)
            continue
        if enrich_note(rp, out):
            enriched += 1
            print(f"已 enrich: {note.relative_to(vault)}")
        else:
            print(f"跳过（校验不过）: {note.relative_to(vault)}", file=sys.stderr)
    if enriched and not args.dry_run:
        print("提示：已改写 frontmatter，请运行 rebuild_index.py 刷新 frontmatter-cache.json 使召回生效",
              file=sys.stderr)
    print(json.dumps({"processed": processed, "enriched": enriched}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
