"""生成包含 N 篇笔记的 Vault fixture，供 perf 测试使用。"""
from __future__ import annotations

import json
import random
from pathlib import Path


def build_large_vault(target: Path, n_notes: int = 500, seed: int = 42) -> None:
    """在 target 目录构造一个含 n_notes 篇笔记的 Vault（仅写 frontmatter-cache.json，
    不写真实笔记文件——评分逻辑只读 cache）。"""
    rng = random.Random(seed)
    categories = ["技术笔记", "项目笔记", "specs", "plans", "改进计划"]
    tag_pool = [
        "android", "ios", "swift", "kotlin", "hook", "skill", "spec",
        "ci", "test", "perf", "bug", "feature", "refactor",
        "ProjectA", "ProjectB", "vault-loader",
    ]

    (target / ".meta").mkdir(parents=True, exist_ok=True)

    kw_pool = ["召回", "扩展词", "相关性打分", "recall", "回归测试", "缓存契约"]
    entries = {}
    for i in range(n_notes):
        cat = rng.choice(categories)
        tags = rng.sample(tag_pool, k=rng.randint(1, 4))
        path = f"{cat}/note_{i:04d}.md"
        entries[path] = {
            "tags": tags,
            "category": cat,
            "summary": f"笔记 {i} — {' '.join(tags[:2])} 相关内容",
            "mtime": 1700000000 + rng.randint(0, 100_000_000),
            "updated": "2026-04-01",
            "keywords": rng.sample(kw_pool, k=rng.randint(0, 3)),
        }

    cache = target / ".meta" / "frontmatter-cache.json"
    cache.write_text(
        json.dumps({"_version": 1, "entries": entries}, ensure_ascii=False)
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: build_large_vault.py <target_dir> [n_notes]")
        sys.exit(1)
    target = Path(sys.argv[1])
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 500
    build_large_vault(target, n)
    print(f"Built {n} entries in {target}/.meta/frontmatter-cache.json")
