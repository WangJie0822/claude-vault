"""相关性评分函数。纯计算，不读 IO。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from functools import lru_cache

from scripts._frontmatter_reader import Entry

# A：纯 ASCII 词字符（[a-z0-9_-]）构成的关键词须在「词边界」命中——命中处左右不得紧邻另一个
# [a-z0-9_-]。连字符/下划线视作复合标识内部（demo-release 是一个整体标识），故通用词
# release 不再误命中 demo-release。含 CJK 的关键词不走此规则、保持子串匹配（CJK 无分隔符，
# 语音⊂语音助手 等子概念命中需保留）。仅作用于 topical/J 路径（_keyword_hits_tags/summary）。
# 取舍：裸 ASCII 子词跨连字符不再命中复合 tag（如 claude ⊄ claude-code）——这是有意收紧；
# 用户完整输入 claude-code 时 J 信号切词会同时产出原 token claude-code（精确命中）+子片，故不漏。
# 复合标识字符集（同步约束）：_ASCII_TOKEN_RE 与下方 lookaround 边界必须用同一字符集，单点定义。
_ASCII_WORD_CHARS = r"a-z0-9_-"
_ASCII_TOKEN_RE = re.compile(rf"[{_ASCII_WORD_CHARS}]+")
_ASCII_BOUNDARY_LB = rf"(?<![{_ASCII_WORD_CHARS}])"
_ASCII_BOUNDARY_LA = rf"(?![{_ASCII_WORD_CHARS}])"


@lru_cache(maxsize=512)
def _boundary_re(k: str) -> "re.Pattern[str]":
    """按关键词缓存编译后的词边界正则——_kw_in_text 在主循环对每篇笔记调用（N 篇 × M 词），
    关键词集小且跨笔记复用，缓存避免重复编译开销（perf：500 笔记 fixture）。"""
    return re.compile(_ASCII_BOUNDARY_LB + re.escape(k) + _ASCII_BOUNDARY_LA)


def _kw_in_text(keyword: str, text: str) -> bool:
    """关键词是否命中文本（大小写不敏感）。ASCII 词→词边界匹配；含 CJK→子串匹配。"""
    k = keyword.lower()
    if not k:
        return False   # 兜底：空串不应命中所有文本（防 _ASCII_TOKEN_RE.fullmatch('')=None 落入子串分支恒 True）
    t = text.lower()
    if _ASCII_TOKEN_RE.fullmatch(k):
        return _boundary_re(k).search(t) is not None
    return k in t


@dataclass
class Signals:
    """SessionStart + UserPromptSubmit 共用的信号包。

    SessionStart 不填 prompt_keywords；UserPromptSubmit 通常仅追加 prompt_keywords。
    """
    project_dir_paths: set[str] = field(default_factory=set)    # 信号 A：直接命中的 path
    target_tags: set[str] = field(default_factory=set)          # 信号 B ∪ I：目标 tag 集
    commit_keywords: set[str] = field(default_factory=set)      # 信号 D：commit 关键词
    worklog_keywords: set[str] = field(default_factory=set)     # 信号 F：工作日志条目关键词
    prompt_keywords: set[str] = field(default_factory=set)      # 信号 J：仅 UserPromptSubmit


def _keyword_hits_entry(keyword: str, entry: Entry) -> bool:
    """判定关键词是否命中 tags / summary / path 中任一字段（大小写不敏感）。
    刻意保留裸子串匹配（不走 A 的 _kw_in_text 词边界）：本函数仅服务 commit(D)/worklog(F)
    信号，二者在 UserPromptSubmit 注入闸门链路无生产调用方（Signals 仅填 target_tags+
    prompt_keywords，score 的 D/F 分支恒不触发）。若未来复活 D/F 打分，需评估是否同步走词边界。"""
    k = keyword.lower()
    if any(k in t.lower() for t in entry.tags):
        return True
    if k in entry.summary.lower():
        return True
    if k in entry.path.lower():
        return True
    return False


def _keyword_hits_tags(keyword: str, entry: Entry) -> bool:
    return any(_kw_in_text(keyword, t) for t in entry.tags)


def _keyword_hits_summary(keyword: str, entry: Entry) -> bool:
    return _kw_in_text(keyword, entry.summary)


def score(entry: Entry, signals: Signals, weights: dict) -> float:
    """计算单篇笔记的相关性分数。"""
    total: float = 0

    # A：项目目录直接命中
    if entry.path in signals.project_dir_paths:
        total += weights["exact_project_dir"]

    # B ∪ I：目标 tag 集与笔记 tags 交集
    if signals.target_tags & set(entry.tags):
        total += weights["tag_target_set_hit"]

    # D：commit 关键词（每个关键词单独命中 +N，去重，上限 cap）
    if signals.commit_keywords:
        hit_count = sum(1 for kw in signals.commit_keywords if _keyword_hits_entry(kw, entry))
        commit_score = hit_count * weights["commit_keyword_hit"]
        total += min(commit_score, weights["commit_keyword_cap"])

    # F：工作日志关键词命中（单次 +N，不累加）
    if signals.worklog_keywords:
        if any(_keyword_hits_entry(kw, entry) for kw in signals.worklog_keywords):
            total += weights["worklog_cooccur"]

    # mtime 衰减（30d / 90d 互斥）
    if entry.mtime:
        age_days = (time.time() - entry.mtime) / 86400
        if age_days <= 30:
            total += weights["mtime_recent_30d"]
        elif age_days <= 90:
            total += weights["mtime_recent_90d"]

    # J：UserPromptSubmit 模式追加
    if signals.prompt_keywords:
        if any(_keyword_hits_tags(kw, entry) for kw in signals.prompt_keywords):
            total += weights["prompt_tag_hit"]
        if any(_keyword_hits_summary(kw, entry) for kw in signals.prompt_keywords):
            total += weights["prompt_summary_hit"]

    return total


def topical_score(entry: Entry, signals: Signals, weights: dict) -> float:
    """仅 prompt 关键词的话题命中分（tag/summary），不含 context（target_tags/mtime）。

    供精度闸门判定'真话题相关'用，与 score() 解耦。
    默认权重（prompt_tag_hit=4 / prompt_summary_hit=2）下值域 {0,2,4,6}；relevance 段阈值
    （min_topical_score / fulltext_topical_threshold / confidence_bands.high）默认值假定该权重，
    改 scoring 权重需同步调阈值。"""
    total: float = 0
    if signals.prompt_keywords:
        if any(_keyword_hits_tags(kw, entry) for kw in signals.prompt_keywords):
            total += weights["prompt_tag_hit"]
        if any(_keyword_hits_summary(kw, entry) for kw in signals.prompt_keywords):
            total += weights["prompt_summary_hit"]
    return total
