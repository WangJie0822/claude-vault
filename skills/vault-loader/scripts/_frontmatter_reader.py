"""读取 ~/Vault/.meta/frontmatter-cache.json，输出规范化 Entry 字典。"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

MAX_CACHE_BYTES = 10 * 1024 * 1024  # 10 MB 上限，超出视为异常膨胀
CACHE_VERSION = 1  # 与写端（rebuild_index）保持对称；版本不符时静默丢弃旧 cache
# 读端每篇 keyword 条数上限（纵深防御）：防异常/恶意 cache 单篇塞数千 keyword 令
# O(N×M×K) 评分爆炸。注：写端主路径（rebuild_index 读手写 frontmatter）不 sanitize、
# 不 cap；仅可选 enrich_keywords.py 回填时 cap 8。故本上限 16 是手写 keywords 路径的
# 唯一实际防线，非余量。
MAX_KEYWORDS_PER_ENTRY = 16
# F6 对称护栏：tags 与 keywords 同理，防异常/恶意 cache 单篇塞海量 tags 拖垮评分
MAX_TAGS_PER_ENTRY = 32
MAX_TAG_CHARS = 128
# summary / path 的长度上限。二者都会直接进模型上下文，而 cache 里单条字段可以逼近
# MAX_CACHE_BYTES（10 MB）——那道闸只看文件总体积，管不住单字段。正常 summary 是
# 一两句话、path 是 Vault 内相对路径，这两个值是宽松余量而非贴身裁剪。
MAX_SUMMARY_CHARS = 4096
MAX_PATH_CHARS = 1024


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


class CacheStatus(str, Enum):
    """`load_cache_status` 的第二返回值：**空索引是怎么来的**。

    `load_cache` 在五种互不相同的情形下都返回 `{}`，调用方无从区分。这不是学术问题：
    任何「cache 空就告警」的诊断都会把下面两个**健康态**误报成故障——

      - `ABSENT`：cache 文件不存在。零配置新装用户还没跑过 summarize-session，
        这就是正常状态。对它告警＝每个新用户第一次开会话都被告知「知识库已死」。
      - `VERSION_MISMATCH`：`_version` 与读端不符。这是**预期过渡态**（本文件 :10 与
        原注释均写明「静默丢弃旧 cache，summarize-session 将在下次运行时重建」）。
        对它告警＝下一次 CACHE_VERSION bump 的那一刻，全部存量用户同时收到误报。

    故只有 `CORRUPT` / `OVERSIZE` 允许触发用户可见告警。**这条是硬约束，改动前先想清楚。**
    `EMPTY`（解析正常但 0 条目）同样是健康态：vault 里确实还没有笔记。
    """
    OK = "ok"
    ABSENT = "absent"
    EMPTY = "empty"
    VERSION_MISMATCH = "version_mismatch"
    CORRUPT = "corrupt"
    OVERSIZE = "oversize"

    def is_failure(self) -> bool:
        """是否属于「真失效」——只有这两种允许告警。"""
        return self in (CacheStatus.CORRUPT, CacheStatus.OVERSIZE)


def load_cache(vault_path: Path) -> dict[str, Entry]:
    """加载 Vault 索引（薄封装，签名与行为完全不变）。

    需要知道「空索引是怎么来的」时用 `load_cache_status`——本函数把五种成因塌缩成同一个
    `{}`，其中两种是健康态（见 `CacheStatus`）。
    """
    return load_cache_status(vault_path)[0]


def _coerce_mtime(raw: object) -> int:
    """把 cache 里的 mtime 归一成 int，非法值退化为 0 而不是抛异常。

    原实现是 `int(meta.get("mtime", 0) or 0)`：对 `"NaN"` / `"abc"` / `"1e999"`
    分别抛 ValueError / ValueError / OverflowError。mtime 只用于排序，取 0（排到最后）
    远比让这条笔记——乃至整份索引——消失合理。

    `bool` 单独挡掉：它是 `int` 的子类，`True` 会被当成时间戳 1。
    """
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, (float, str)):
        try:
            return int(float(raw))  # float() 先行：兼容 "1700000000.5" 这类写法
        except (ValueError, OverflowError):
            return 0
    return 0


def load_cache_status(vault_path: Path) -> tuple[dict[str, Entry], CacheStatus]:
    """加载 Vault 索引，并回报空索引的成因。
    缺失 / 损坏 / 超大 → 返回空 dict，stderr 警告。
    """
    cache_path = vault_path / ".meta" / "frontmatter-cache.json"

    if not cache_path.exists():
        return {}, CacheStatus.ABSENT

    try:
        size = cache_path.stat().st_size
        if size > MAX_CACHE_BYTES:
            print(
                f"[vault-loader] frontmatter-cache.json 异常膨胀 ({size} bytes)，跳过加载",
                file=sys.stderr,
            )
            return {}, CacheStatus.OVERSIZE

        data = json.loads(cache_path.read_text(encoding="utf-8"))
        # 根必须是对象。cache 是外部数据（写端是另一个 skill、用户可能手改、也可能来自
        # clone 的他人 Vault），根是 `[]` / `"str"` / `null` 都是合法 JSON。没有这道守卫时
        # 下一行的 `.get` 抛 AttributeError，而它**不在**下方 except 元组里 → 冒泡出函数
        # → hook 静默死亡、stdout 全空，连 CORRUPT 诊断都轮不到触发。
        if not isinstance(data, dict):
            print("[vault-loader] frontmatter-cache.json 根不是对象，已跳过", file=sys.stderr)
            return {}, CacheStatus.CORRUPT
        # 版本校验（与写端 rebuild_index 对称）：
        # 旧 cache 无 _version 字段或版本不符 → 丢弃，降级为空索引（静默早退，安全）。
        # 这是预期行为：summarize-session 将在下次运行时重建 cache。**故不算失效**。
        if data.get("_version") != CACHE_VERSION:
            return {}, CacheStatus.VERSION_MISMATCH
        raw_entries = data.get("entries", {})
        if not isinstance(raw_entries, dict):
            # entries 结构不对 —— 这是真损坏，不是过渡态。
            return {}, CacheStatus.CORRUPT

        result: dict[str, Entry] = {}
        skipped = 0
        for path, meta in raw_entries.items():
            # 逐条兜底：**爆炸半径必须收敛到单条**。原实现让一条笔记的坏字段（如
            # `mtime: "NaN"`）冒泡到最外层，整份索引判 CORRUPT——3 篇只坏 1 篇、存活 0 条。
            try:
                if not isinstance(meta, dict):
                    skipped += 1
                    continue
                if not isinstance(path, str) or len(path) > MAX_PATH_CHARS:
                    skipped += 1
                    continue
                # tags 必须是 list：写成标量字符串时 `or []` 保留原值，
                # `tuple(t for t in "foo")` 会逐字符迭代出 ('f','o','o') 三个假 tag。
                # 假 tag 会进 build_tag_df 参与 IDF 统计，而 tag-IDF 加权正是 0.5.0 的
                # 核心特性——每个只出现一次的单字符 tag 会被当成高信息量精确信号。
                # 与下面 kw_raw 的守卫对称（那侧本就有，这侧此前漏了）。
                tags_raw = meta.get("tags")
                if not isinstance(tags_raw, list):
                    tags_raw = []
                tags = tuple(t for t in tags_raw
                             if isinstance(t, str) and len(t) <= MAX_TAG_CHARS)[:MAX_TAGS_PER_ENTRY]
                kw_raw = meta.get("keywords")
                if not isinstance(kw_raw, list):
                    kw_raw = []
                keywords = tuple(
                    k for k in kw_raw
                    if isinstance(k, str) and len(k.strip()) >= 2
                )[:MAX_KEYWORDS_PER_ENTRY]
                result[path] = Entry(
                    path=path,
                    tags=tags,
                    category=str(meta.get("category", ""))[:MAX_SUMMARY_CHARS],
                    summary=str(meta.get("summary", ""))[:MAX_SUMMARY_CHARS],
                    mtime=_coerce_mtime(meta.get("mtime")),
                    updated=str(meta.get("updated", ""))[:MAX_TAG_CHARS],
                    keywords=keywords,
                )
            except Exception:  # noqa: BLE001 — 单条坏数据不得牵连其余
                skipped += 1
                continue
        if skipped:
            print(f"[vault-loader] frontmatter-cache.json 跳过 {skipped} 条异常记录",
                  file=sys.stderr)
        # 解析成功但 0 条目：vault 里确实还没有笔记，健康态。
        return result, (CacheStatus.OK if result else CacheStatus.EMPTY)

    except Exception as exc:  # noqa: BLE001 — 见下
        # 刻意用 `Exception` 而非窄元组。这是 fail-open 边界：函数的契约是
        # 「永远返回 (dict, status)」，任何逃逸都会让 hook 静默死亡。
        # 窄元组补不全——`json.loads` 对深嵌套输入抛 **RecursionError**，它是
        # `RuntimeError` 的子类，既不是 ValueError 也不是 TypeError/AttributeError，
        # 只补那几个照样漏（~80KB 输入即可触发，远在 10 MB 上限之内）。
        print(f"[vault-loader] frontmatter-cache.json 加载失败：{exc}", file=sys.stderr)
        return {}, CacheStatus.CORRUPT
