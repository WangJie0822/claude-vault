"""相关性评分函数。纯计算，不读 IO。"""
from __future__ import annotations

import math
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
    # 信号 K（2026-09-02）：会话主题词，由首轮异步提炼、后续轮次复用。
    # **刻意与 prompt_keywords 分开**：并入后会进 prompt_submit_load 的 shown_hits_str
    # 回显路径（该行不经 sanitize_injected_text），且按 _hit_keywords 全口径膨胀
    # （实测裸主题词使 admitted 涨 5.5 倍）。作为独立信号则两者都不发生。
    session_topic_words: set[str] = field(default_factory=set)


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


def is_archived(entry: Entry, exclude_tags: set[str]) -> bool:
    """笔记是否命中排除 tag（archived 等，大小写不敏感）。第1层召回池过滤单点，
    供 prompt_submit_load(UPS) / session_start_load(SessionStart) / 回归 gold 集共用，
    避免过滤逻辑三处漂移。exclude_tags 应为已 lower 的 set；空 set → 恒 False。"""
    if not exclude_tags:
        return False
    return any(t.lower() in exclude_tags for t in entry.tags)


def _keyword_hits_tags(keyword: str, entry: Entry) -> bool:
    return any(_kw_in_text(keyword, t) for t in entry.tags)


def _keyword_hits_summary(keyword: str, entry: Entry) -> bool:
    return _kw_in_text(keyword, entry.summary)


def _keyword_hits_keywords(keyword: str, entry: Entry) -> bool:
    return any(_kw_in_text(keyword, k) for k in entry.keywords)


def has_keyword_hit(entry: Entry, prompt_keywords, use_keywords: bool = True) -> bool:
    """entry.keywords 是否被任一 prompt 关键词命中。

    打分（_prompt_topical_hits）与候选闸门（prompt_submit_load / _decision）共用此单点判定，
    避免逻辑漂移。

    **BUG-1（2026-08-06）**：此前语义为「既命中 tag 又命中 keyword 的词只算 tag、不计
    keyword」。该去重在 prompt_keyword_hit=3 < prompt_tag_hit=4 时合理，但权重调为 5 后
    产生惩罚性反转——tag 与 keywords 双命中（作者既打标签又列检索词，是最强相关性信号）
    反而只拿 tag 的 4×IDF≤4，比仅 keywords 单命中的 5 分更低。实测该反转正是真实案例中
    目标笔记落后 Top1 恰好 1.20 分（5−3.80）的唯一成因。故取消去重、允许双计。
    """
    if not (use_keywords and entry.keywords and prompt_keywords):
        return False
    return any(_keyword_hits_keywords(kw, entry) for kw in prompt_keywords)


def build_tag_df(entries) -> dict[str, int]:
    """统计每个 tag（小写）覆盖的笔记数。entries 为 {path: Entry}。

    数据全在 cache 内存里，无额外 IO；实测 ~1ms@1000 篇、~7ms@5000 篇
    （线性 O(N·avg_tags)，封顶 MAX_TAGS=32）。
    """
    df: dict[str, int] = {}
    for e in entries.values():
        for t in set(t.lower() for t in e.tags):
            df[t] = df.get(t, 0) + 1
    return df


def tag_idf_factor(tag: str, tag_df: dict, n_docs: int, floor: float = 0.5) -> float:
    """tag 的 IDF 加权因子，值域 [floor, 1.0]。

    泛 tag（如 superpowers 覆盖 142/680 篇）与 singleton tag 此前完全等权，
    是「噪声」与「漏召回」同时发生的直接成因。这里按 IDF 降权，但**保底 floor**：
    全归零会让大量原本 topical=6 的条目掉到 min_topical_score 以下被整体过滤，属行为剧变。
    保底后**同时有 summary/keyword 命中**的泛 tag 条目仍可召回、只排在精确命中之后；
    但**仅有泛 tag 命中**（无 summary/keyword）的孤 tag 条目会因降权后低于 min_topical
    被候选闸门**有意剔除**（降噪）——即 tag-IDF 不只是重排序，它**改变了召回集**。
    隐式耦合不变量：孤共享 tag 是否存活 ⇔ prompt_tag_hit × tag_idf_floor ≥ min_topical_score；
    默认（4×0.5=2 < 4）下 df≥2 孤 tag 被剔除、仅 singleton（factor=1.0→4.0）存活。改
    tag_idf_floor / min_topical_score / prompt_tag_hit 任一都会移动这条召回边界
    （守卫见 tests/test_tag_idf.py::test_orphan_df2_tag_drops_below_gate）。
    """
    if not tag_df or n_docs <= 1:
        return 1.0
    df = tag_df.get(tag.lower(), 1)
    if df <= 0:
        return 1.0
    # ln(1+N/df) / ln(1+N) → df=1 时为 1.0，df 越大越接近 0
    idf_norm = math.log(1.0 + n_docs / df) / math.log(1.0 + n_docs)
    idf_norm = max(0.0, min(1.0, idf_norm))
    return floor + (1.0 - floor) * idf_norm


def _prompt_topical_hits(entry: Entry, signals: Signals, weights: dict,
                         use_keywords: bool = True, tag_df=None,
                         n_docs: int = 0, tag_idf_floor: float = 0.5) -> float:
    """prompt 关键词对 tag/summary/keywords 的话题命中分（去重 + 门控）。
    score() 的 J 段与 topical_score() 共用此单点，消除重复、防漂移。

    tag_df 为 None 时数值等价保持旧行为（tag 命中拿满 prompt_tag_hit；注：factor=1.0
    使返回 float 4.0 而非 int 4，`==` 成立故数值等价，非逐字节相同）；
    传入 tag_df 时按 IDF 降权，多 tag 命中取 max 而非累加（防堆砌 tag 刷分）。
    """
    # 早退条件须同时看 prompt_keywords 与 session_topic_words：早期实现只判前者，
    # 会让「无 prompt_keywords、仅有 session_topic_words」的调用（如
    # test_session_topic_scoring.py::test_topic_word_adds_score）在下面的信号 K
    # 分支被触及前就已 return 0，主题词命中永远算不到分。
    if not signals.prompt_keywords and not signals.session_topic_words:
        return 0
    total: float = 0
    hit_tags = [t for t in entry.tags
                if any(_kw_in_text(kw, t) for kw in signals.prompt_keywords)]
    if hit_tags:
        if tag_df:
            factor = max(tag_idf_factor(t, tag_df, n_docs, tag_idf_floor) for t in hit_tags)
        else:
            factor = 1.0
        total += weights["prompt_tag_hit"] * factor
    if any(_keyword_hits_summary(kw, entry) for kw in signals.prompt_keywords):
        total += weights["prompt_summary_hit"]
    if has_keyword_hit(entry, signals.prompt_keywords, use_keywords):
        total += weights["prompt_keyword_hit"]
    # 信号 K：会话主题词命中（tag/summary/keywords 三面任一，与 _decision.py::_hit_keywords
    # 同一口径——**不含 path**，避免仅靠文件名命中就计入话题相关性），**只加一次**
    # —— 与 tag 面「多命中取 max 不累加」同构，防堆砌刷分。
    # 注：此前误用 _keyword_hits_entry（只查 tags/summary/path，漏 keywords、误含 path），
    # 评审 Finding 1 实测两个方向都错，已改用与 _hit_keywords 一致的三个单点函数。
    if signals.session_topic_words and any(
            _keyword_hits_tags(w, entry) or _keyword_hits_summary(w, entry)
            or _keyword_hits_keywords(w, entry)
            for w in signals.session_topic_words):
        total += weights.get("session_topic_hit", 0)
    return total


def score(entry: Entry, signals: Signals, weights: dict,
          use_keywords: bool = True, tag_df=None,
          n_docs: int = 0, tag_idf_floor: float = 0.5) -> float:
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

    # J：UserPromptSubmit 模式追加（与 topical_score 共用 _prompt_topical_hits 单点）
    total += _prompt_topical_hits(entry, signals, weights, use_keywords,
                                  tag_df, n_docs, tag_idf_floor)

    return total


def topical_score(entry: Entry, signals: Signals, weights: dict,
                  use_keywords: bool = True, tag_df=None,
                  n_docs: int = 0, tag_idf_floor: float = 0.5) -> float:
    """仅 prompt 关键词（含会话主题词）的话题命中分（tag/summary/keywords + 主题词），
    不含 context。

    值域：tag 命中 [prompt_tag_hit*floor, prompt_tag_hit] + summary + keywords
    + session_topic。默认权重（tag=4 / summary=2 / keywords=5、floor=0.5、
    session_topic=2）下上界为 13（= 4+2+5+2）。relevance 段阈值
    （min_topical_score / fulltext_topical_threshold / confidence_bands.high）
    假定的仍是旧权重下的上界 11，改 scoring 权重需同步调阈值；session_topic_hit
    刻意小于 min_topical_score（断言见
    tests/test_session_topic_scoring.py::test_upper_bound_invariant_documented），
    故三个阈值目前仍然有效，但这条不变量是靠该断言钉住的，不是靠本函数值域自动保证。
    """
    return _prompt_topical_hits(entry, signals, weights, use_keywords,
                                tag_df, n_docs, tag_idf_floor)


# ===== 第0层 §3.5：证据链合并 + 强证据档（纯计算，供 prompt_submit_load 门槛/置信） =====

_CJK_BIGRAM_FULL_RE = re.compile(r"[一-鿿]{2}")


def evidence_chain_count(hits: list[str]) -> int:
    """union-find 证据链合并：两个 2 字 CJK bigram 任一方向首尾重叠一字 → 同链
    （同一连续源词切出的相邻 bigram 归并为一条证据）；非 bigram token 各自成链。
    修复：裸 len(hits) 会被相邻 bigram 膨胀（4 字词→3 hits）击穿 dist≥2 纵深防御；
    实现必须全对判定而非 sorted 相邻对（CJK codepoint 序破坏相邻性，评审 R3）。"""
    n = len(hits)
    parent = list(range(n))

    def _find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = hits[i], hits[j]
            if (_CJK_BIGRAM_FULL_RE.fullmatch(a) and _CJK_BIGRAM_FULL_RE.fullmatch(b)
                    and (a[1] == b[0] or b[1] == a[0])):
                parent[_find(i)] = _find(j)
    return len({_find(i) for i in range(n)})


def has_strong_evidence(hits: list[str]) -> bool:
    """全文/高置信强证据档（用户拍板，spec §3.5）：链数 ≥2 且（存在多 bigram 合并链
    ——即笔记含用户输入的 ≥3 字连续词——或 ≥1 个非 bigram 命中 token）。
    效果（PoC v2 实证）：「看看日志输出」类散 bigram 弱命中不再升全文/标高置信。"""
    chains = evidence_chain_count(hits)
    if chains < 2:
        return False
    return chains < len(hits) or any(not _CJK_BIGRAM_FULL_RE.fullmatch(h) for h in hits)
