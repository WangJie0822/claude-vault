"""生成包含 N 篇笔记的 Vault fixture，供 perf 测试使用。

正文长度与形态按真实 Vault（977 篇）实测分布生成：
  正文字符数 中位 6395 / p90 33103 / p99 65493 / max 185690（右偏长尾）
  代码块占比 中位 9.8% / 均值 21.1% / p90 65.2%
  CJK 占比   中位 19.2%
不生成正文会让任何读正文的代码在 perf 测试下 ENOENT 秒返回。故 with_bodies
默认 True——真实正文的价值在**覆盖读代码路径的正确性**（test_fixture_writes_real_bodies
验证分布/代码块/CJK），以及让全文注入分支真正执行读+slice+sanitize。
诚实边界（full-review 3A-T3 实测）：真实正文读+slice+sanitize 对 UPS 端到端仅加 ~0ms
（被解释器启动+O(N) 打分循环主导），故 300ms 预算断言**不对读成本敏感**——它守护的是
整体 O(N) 打分开销，不是读路径局部回归。别据此以为改小读上限就会被 perf 守卫拦住。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

_CJK = "召回扩展词相关性打分回归测试缓存契约语义检索关键词匹配向量嵌入构建日志错误路径配置"
_ASCII = ["recall", "scorer", "cache", "vault", "loader", "hook", "index",
          "gradle", "pytest", "frontmatter", "keywords", "mangle"]


def _make_body(rng: random.Random, n_chars: int) -> str:
    """按目标长度合成正文：散文段 + fenced code block 混排。"""
    parts: list[str] = []
    written = 0
    # 代码块占比按 beta 型偏斜采样：多数笔记低占比，少数极高（对齐 p90=65%）
    code_ratio = min(0.9, rng.betavariate(1.4, 4.0) * 1.6)
    while written < n_chars:
        if rng.random() < code_ratio:
            n = min(rng.randint(120, 900), n_chars - written)
            lines = [f"    {rng.choice(_ASCII)}_{rng.randint(0, 999)} = {rng.randint(0, 9999)}"
                     for _ in range(max(1, n // 40))]
            block = "```python\n" + "\n".join(lines) + "\n```\n"
            parts.append(block)
            written += len(block)
        else:
            n = min(rng.randint(80, 600), n_chars - written)
            buf = []
            while sum(len(x) for x in buf) < n:
                if rng.random() < 0.35:
                    buf.append("".join(rng.choice(_CJK) for _ in range(rng.randint(2, 12))))
                else:
                    buf.append(rng.choice(_ASCII))
            para = " ".join(buf) + "\n\n"
            parts.append(para)
            written += len(para)
    return "".join(parts)


def _sample_len(rng: random.Random) -> int:
    """采样正文长度，复现真实 Vault 的右偏长尾。"""
    r = rng.random()
    if r < 0.25:
        return rng.randint(800, 3911)
    if r < 0.50:
        return rng.randint(3911, 6395)
    if r < 0.75:
        return rng.randint(6395, 13005)
    if r < 0.90:
        return rng.randint(13005, 33103)
    if r < 0.99:
        return rng.randint(33103, 65493)
    return rng.randint(65493, 185690)


def build_large_vault(target: Path, n_notes: int = 500, seed: int = 42,
                      with_bodies: bool = True) -> None:
    """在 target 目录构造一个含 n_notes 篇笔记的 Vault。

    with_bodies=True（默认）时同时写出真实 .md 正文文件；设为 False 只写
    frontmatter-cache.json（旧行为，仅用于确实不需要正文的场景）。
    """
    rng = random.Random(seed)
    categories = ["技术笔记", "项目笔记", "specs", "plans", "改进计划"]
    tag_pool = [
        "android", "ios", "swift", "kotlin", "hook", "skill", "spec",
        "ci", "test", "perf", "bug", "feature", "refactor",
        "ProjectA", "ProjectB", "vault-loader",
    ]

    (target / ".meta").mkdir(parents=True, exist_ok=True)

    kw_pool = ["召回", "扩展词", "相关性打分", "recall", "回归测试",
               "缓存契约", "语义检索", "关键词匹配", "向量嵌入"]
    entries = {}
    for i in range(n_notes):
        cat = rng.choice(categories)
        tags = rng.sample(tag_pool, k=rng.randint(1, 4))
        path = f"{cat}/note_{i:04d}.md"
        summary = f"笔记 {i} — {' '.join(tags[:2])} 相关内容"
        keywords = rng.sample(kw_pool, k=rng.randint(0, 8))
        mtime = 1700000000 + rng.randint(0, 100_000_000)
        entries[path] = {
            "tags": tags,
            "category": cat,
            "summary": summary,
            "mtime": mtime,
            "updated": "2026-04-01",
            "keywords": keywords,
        }
        if with_bodies:
            note = target / path
            note.parent.mkdir(parents=True, exist_ok=True)
            fm = (
                "---\n"
                f"tags: [{', '.join(tags)}]\n"
                f"category: {cat}\n"
                f"summary: {summary}\n"
                f"keywords: [{', '.join(keywords)}]\n"
                "---\n\n"
            )
            note.write_text(fm + _make_body(rng, _sample_len(rng)), encoding="utf-8")

    cache = target / ".meta" / "frontmatter-cache.json"
    cache.write_text(
        json.dumps({"_version": 1, "entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: build_large_vault.py <target_dir> [n_notes] [--no-bodies]")
        sys.exit(1)
    target = Path(sys.argv[1])
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    n = int(args[0]) if args else 500
    build_large_vault(target, n, with_bodies="--no-bodies" not in sys.argv)
    print(f"Built {n} entries in {target}/.meta/frontmatter-cache.json")
