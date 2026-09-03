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

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
# 只有**确实像插件根**才插入 sys.path。legacy 独立布局
# （`~/.claude/skills/<skill>/scripts/`）下 parents[3] 正是 `~/.claude` 本身——
# 把一个多插件共享、可被任意工具写入的目录放到 sys.path[0]，等于让任何能在那里
# 落一个 `context_vault/__init__.py` 的东西在每次 hook 进程内取得代码执行。
# 判据不成立时跳过，交给下面的 ImportError façade 兜底。
_LOOKS_LIKE_PLUGIN_ROOT = (_PLUGIN_ROOT / "context_vault" / "runtime.py").is_file()
if _LOOKS_LIKE_PLUGIN_ROOT and str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from context_vault.model import call_claude, call_codex, choose_backend

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
    return call_claude(prompt + content, timeout=_TIMEOUT, model="haiku")


def _call_codex(content: str) -> str | None:
    prompt = (
        "为下面这篇笔记生成 3-8 个中文/英文检索扩展词（同义词、别名、跨语言术语），"
        "只输出符合 schema 的 JSON。笔记：\n"
    )
    return call_codex(prompt + content, timeout=_TIMEOUT)


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
    ap.add_argument("--backend", choices=("auto", "claude", "codex"), default="auto",
                    help="模型后端；auto 跟随当前插件运行时，不跨提供商回退")
    args = ap.parse_args(argv)

    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(f"vault 不存在: {vault}", file=sys.stderr)
        return 1

    # backend 在循环**外**解析一次。两个理由：
    # 1. `choose_backend("auto")` 在两个 PLUGIN_ROOT 变量都不存在时抛 ValueError，而
    #    本脚本按设计就是「付费、手动 opt-in、不接自动管线」的 CLI——正常从普通终端跑，
    #    两个变量都不会有。放在循环里意味着用户看到的是一条裸 traceback（且已经先
    #    `processed += 1` 了），而不是可操作的提示。`--dry-run` 提前 continue，
    #    所以这个坑只在真跑时触发，更隐蔽。
    # 2. 每篇重复调用没有意义。
    backend = ""
    if not args.dry_run:
        try:
            backend = choose_backend(args.backend)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2

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
        out = _call_codex(text) if backend == "codex" else _call_claude(text)
        if out is None:
            print(f"跳过（{backend} 失败/缺失）: {note.relative_to(vault)}", file=sys.stderr)
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
