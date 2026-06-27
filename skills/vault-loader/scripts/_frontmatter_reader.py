"""读取 ~/Vault/.meta/frontmatter-cache.json，输出规范化 Entry 字典。"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_CACHE_BYTES = 10 * 1024 * 1024  # 10 MB 上限，超出视为异常膨胀
CACHE_VERSION = 1  # 与写端（rebuild_index）保持对称；版本不符时静默丢弃旧 cache


@dataclass(frozen=True)
class Entry:
    """单篇笔记的索引快照。"""
    path: str
    tags: tuple[str, ...] = field(default_factory=tuple)
    category: str = ""
    summary: str = ""
    mtime: int = 0
    updated: str = ""
    keywords: tuple[str, ...] = field(default_factory=tuple)


def load_cache(vault_path: Path) -> dict[str, Entry]:
    """加载 Vault 索引。
    缺失 / 损坏 / 超大 → 返回空 dict，stderr 警告。
    """
    cache_path = vault_path / ".meta" / "frontmatter-cache.json"

    if not cache_path.exists():
        return {}

    try:
        size = cache_path.stat().st_size
        if size > MAX_CACHE_BYTES:
            print(
                f"[vault-loader] frontmatter-cache.json 异常膨胀 ({size} bytes)，跳过加载",
                file=sys.stderr,
            )
            return {}

        data = json.loads(cache_path.read_text(encoding="utf-8"))
        # 版本校验（与写端 rebuild_index 对称）：
        # 旧 cache 无 _version 字段或版本不符 → 丢弃，降级为空索引（静默早退，安全）。
        # 这是预期行为：summarize-session 将在下次运行时重建 cache。
        if data.get("_version") != CACHE_VERSION:
            return {}
        raw_entries = data.get("entries", {})
        if not isinstance(raw_entries, dict):
            return {}

        result: dict[str, Entry] = {}
        for path, meta in raw_entries.items():
            if not isinstance(meta, dict):
                continue
            tags_raw = meta.get("tags") or []
            tags = tuple(t for t in tags_raw if isinstance(t, str))
            kw_raw = meta.get("keywords")
            if not isinstance(kw_raw, list):
                kw_raw = []
            keywords = tuple(
                k for k in kw_raw
                if isinstance(k, str) and len(k.strip()) >= 2
            )
            result[path] = Entry(
                path=path,
                tags=tags,
                category=str(meta.get("category", "")),
                summary=str(meta.get("summary", "")),
                mtime=int(meta.get("mtime", 0) or 0),
                updated=str(meta.get("updated", "")),
                keywords=keywords,
            )
        return result

    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"[vault-loader] frontmatter-cache.json 加载失败：{exc}", file=sys.stderr)
        return {}
