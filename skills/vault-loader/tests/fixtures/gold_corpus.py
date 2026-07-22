# -*- coding: utf-8 -*-
"""构造式排序 gold 集：合成语料 + 由构造直接得出的 ground truth。

语料全合成（可入仓、无隐私风险、可复现），但统计形态按真实 Vault 实测参数：
tag 幂律分布（少数泛 tag 覆盖大量笔记 + 大量 singleton tag）、代码块噪声、
CJK bigram 碎片、时效差异。

**四类干扰项**（每类对应四维评审实测到的真实病理）：
  D1 泛 tag 干扰   —— 打高频 tag 但内容无关（真实 Vault: superpowers 142 篇 / Windows 45 篇）。
                    必须有查询命中 BROAD_TAGS 才能让这 80 篇真正参与排序竞争（见下方
                    query 列表末尾两条「superpowers 流程」「plan 文档」query，评审 Finding 1）——
                    早期版本 21 条 query 无一命中 BROAD_TAGS 字符串，D1 对全部查询恒得分 0，
                    形同虚设，测不出 Task 8 tag-IDF 改造要解决的核心病理。
  D2 代码块噪声   —— 查询词只出现在 fenced code block 内（真实 Vault: 42.6% 字符在代码块内）
  D3 功能词碎片   —— 正文含 CJK bigram 碎片（真实: `程怎` df=0 → IDF 7.22 反超 full-review 1.49）
  D4 时效权衡     —— 旧的高相关 vs 新的中相关。**已知 gap（评审 Finding 2，未修，仅文档化）**：
                    `NOW` 是固定纪元（保证语料确定性），但生产 `score()`（scripts/_scorer.py 的
                    mtime 衰减段）用**实时** `time.time()` 算 age_days——两者随真实时间流逝逐日
                    产生漂移（写下本注释时已漂移 ~169 天），漂移增大到一定程度会让 D4 两篇双双
                    落出 30d/90d 加分窗、时效权衡语义失真。**当前不生效**：Task 6/8 的消费方用的是
                    `topical_score()`，该函数只是 `_prompt_topical_hits` 的别名，不含 mtime 衰减，
                    D4 现无实际消费者。若未来消费方改用含 mtime 的 `score()` 路径，或需要 D4 真正
                    生效，必须由调用方注入可控时钟（而非在本模块引入 `time.time()`——那会破坏
                    本语料「两次调用 `build_gold_corpus()` 产物完全一致」的确定性硬约束）。
"""
from __future__ import annotations

from typing import NamedTuple

from scripts._frontmatter_reader import Entry

NOW = 1_770_000_000          # 固定基准时间，保证可复现
DAY = 86400

BROAD_TAGS = ["superpowers", "spec", "plan", "全链路实战", "评审"]


class GoldQuery(NamedTuple):
    prompt: str
    relevant: dict          # {path: 相关度}  2=高相关 1=中相关


def _entry(path, tags, summary, keywords=(), age_days=30) -> Entry:
    """构造一条语料。注意 Entry 无正文字段（现打分模型只读 frontmatter），
    故 D2「代码块噪声」干扰项通过 summary 表达，不引入会被丢弃的 body 参数。"""
    return Entry(path=path, tags=tuple(tags), summary=summary,
                 keywords=tuple(keywords), mtime=NOW - age_days * DAY)


def build_gold_corpus() -> tuple[list[Entry], list[GoldQuery]]:
    """返回 (语料, 标注查询)。语料 ≥200 篇，查询 ≥20 条。"""
    corpus: list[Entry] = []

    # ---- 主题组：每个主题 1 篇高相关 + 1 篇中相关 ----
    topics = [
        ("mangle", "路径穿越 mangle", ["MSYS 路径 mangle", "MSYS_NO_PATHCONV"],
         "Git Bash 调用外部命令时路径被转换的根因与规避"),
        ("gradle-mem", "gradle 内存", ["Gradle 内存压力", "OOM"],
         "gradle 构建内存不足的诊断与 Xmx 调整"),
        ("worktree-idx", "worktree index", ["index.lock", "并发污染"],
         "并发 git 操作导致 worktree index 污染与恢复"),
        ("recall-score", "召回打分", ["相关性打分", "topical score"],
         "vault-loader 召回打分模型与阈值设计"),
        ("hook-failopen", "hook 兜底", ["fail-open", "hook 容错"],
         "hook 顶层兜底保证永不阻断会话"),
        ("cjk-token", "中文分词", ["CJK bigram", "分词切片"],
         "中文查询的 bigram 切分与误命中治理"),
        ("cache-ver", "缓存版本", ["cache 版本", "schema 契约"],
         "读写双端 cache 版本契约与降级行为"),
        ("perf-budget", "性能预算", ["性能预算", "耗时基线"],
         "hook 耗时预算与性能守卫设计"),
        ("sanitize", "注入净化", ["控制字符净化", "prompt injection"],
         "注入文本的控制字符净化与隔离声明"),
        ("archive", "归档过滤", ["archived 过滤", "召回池"],
         "archived 笔记排除出召回池的机制"),
    ]
    for key, tag, kws, summ in topics:
        corpus.append(_entry(f"主题/{key}-high.md", [tag], summ, kws, age_days=60))
        corpus.append(_entry(f"主题/{key}-mid.md", [tag], f"{summ}的补充说明", (), age_days=20))

    # ---- D1 泛 tag 干扰：大量笔记打泛 tag，内容与任何查询无关 ----
    for i in range(80):
        corpus.append(_entry(
            f"干扰/broad-{i:03d}.md",
            [BROAD_TAGS[i % len(BROAD_TAGS)]],
            "本篇讲述一次常规实施过程与评审收尾，无特定技术主题",
            age_days=i % 90,
        ))

    # ---- D2 代码块噪声：查询词只出现在代码块内 ----
    # Entry 无正文字段（现打分模型只读 frontmatter），故不引入会被丢弃的 body 参数；
    # 改为把「查询词只作为命令片段出现、主题实为无关」这一形态压进 summary 表达——
    # 效果等价：该篇会命中查询词但不应排在真正相关的笔记之前。
    for i, (key, tag, kws, summ) in enumerate(topics):
        corpus.append(_entry(
            f"干扰/code-{key}.md", [BROAD_TAGS[i % len(BROAD_TAGS)]],
            f"一次无关的构建流程记录。```bash\n# {key} {' '.join(kws)}\nrun --{key}\n```",
            age_days=5,
        ))

    # ---- D3 功能词碎片：summary 含高频功能词，易被 bigram 切片误命中 ----
    for i in range(40):
        corpus.append(_entry(
            f"干扰/frag-{i:03d}.md", [f"singleton-{i}"],
            "这个流程怎么走才对，怎么做才能让它跑起来，如何做取舍",
            age_days=i,
        ))

    # ---- D4 时效权衡：同主题，旧的高相关 vs 新的低相关 ----
    # 已知 gap（评审 Finding 2，不在本次修复范围，故意不用 time.time() 换算，见模块 docstring）：
    # mtime 锚定固定 NOW，score() 却用实时 time.time() 算 age_days，二者逐日漂移；当前消费方
    # topical_score() 不含 mtime 衰减，D4 暂无实际效果，仅保留供未来注入可控时钟的消费方使用。
    corpus.append(_entry("主题/timeliness-old-high.md", ["时效测试"],
                         "时效测试主题的完整权威说明与根因分析",
                         ["时效权衡", "recency"], age_days=300))
    corpus.append(_entry("主题/timeliness-new-low.md", ["时效测试"],
                         "时效测试主题的一句话备忘", age_days=1))

    # ---- 补足到 200+ 篇的背景噪声 ----
    for i in range(60):
        corpus.append(_entry(f"背景/bg-{i:03d}.md", [f"bgtag-{i}"],
                             f"背景笔记 {i}，记录一些与检索无关的日常内容",
                             age_days=i % 120))

    queries = [
        GoldQuery("Windows 路径 mangle 问题", {"主题/mangle-high.md": 2, "主题/mangle-mid.md": 1}),
        GoldQuery("gradle 构建内存不足怎么办",
                  {"主题/gradle-mem-high.md": 2, "主题/gradle-mem-mid.md": 1}),
        GoldQuery("worktree index 被并发污染怎么恢复",
                  {"主题/worktree-idx-high.md": 2, "主题/worktree-idx-mid.md": 1}),
        GoldQuery("召回打分模型怎么改",
                  {"主题/recall-score-high.md": 2, "主题/recall-score-mid.md": 1}),
        GoldQuery("hook 怎么保证不阻断会话",
                  {"主题/hook-failopen-high.md": 2, "主题/hook-failopen-mid.md": 1}),
        GoldQuery("中文分词 CJK bigram 误命中",
                  {"主题/cjk-token-high.md": 2, "主题/cjk-token-mid.md": 1}),
        GoldQuery("缓存版本契约怎么设计",
                  {"主题/cache-ver-high.md": 2, "主题/cache-ver-mid.md": 1}),
        GoldQuery("性能预算和耗时基线",
                  {"主题/perf-budget-high.md": 2, "主题/perf-budget-mid.md": 1}),
        GoldQuery("注入文本的控制字符净化",
                  {"主题/sanitize-high.md": 2, "主题/sanitize-mid.md": 1}),
        GoldQuery("archived 笔记怎么排除出召回池",
                  {"主题/archive-high.md": 2, "主题/archive-mid.md": 1}),
        GoldQuery("MSYS_NO_PATHCONV 是干什么的", {"主题/mangle-high.md": 2}),
        GoldQuery("Gradle 内存压力排查", {"主题/gradle-mem-high.md": 2}),
        GoldQuery("index.lock 并发污染", {"主题/worktree-idx-high.md": 2}),
        GoldQuery("topical score 阈值", {"主题/recall-score-high.md": 2}),
        GoldQuery("fail-open 容错设计", {"主题/hook-failopen-high.md": 2}),
        GoldQuery("分词切片怎么做", {"主题/cjk-token-high.md": 2}),
        GoldQuery("schema 契约与降级", {"主题/cache-ver-high.md": 2}),
        GoldQuery("耗时基线怎么定", {"主题/perf-budget-high.md": 2}),
        GoldQuery("prompt injection 防护", {"主题/sanitize-high.md": 2}),
        GoldQuery("召回池过滤机制", {"主题/archive-high.md": 2}),
        GoldQuery("时效权衡 recency", {"主题/timeliness-old-high.md": 2}),
        # ---- D1 激活 query（评审 Finding 1）：以上 21 条均不含 BROAD_TAGS 任一字符串，
        # 致 D1 干扰笔记恒得分 0、从未参与排序竞争。以下两条显式含泛 tag 词（真实场景：
        # 用户确实会问「superpowers 流程怎么走」这类含流程词的问题），让 D1 真正与正确笔记
        # 竞争排序——ground truth 仍标向真正相关笔记，80 篇 D1 干扰笔记不标（应被压下去）。
        # 已用 collect_signal_j_prompt_keywords + topical_score 实测验证（见
        # test_d1_broad_tag_noise_is_real 与 task-5-report.md 附实测输出）：
        #   "superpowers 流程里怎么设计召回打分模型" → tag "superpowers" 命中 16 篇
        #   干扰/broad-*.md（score=4），真正相关的 recall-score-high/mid 各得 6 分、排名更高。
        GoldQuery("superpowers 流程里怎么设计召回打分模型",
                  {"主题/recall-score-high.md": 2, "主题/recall-score-mid.md": 1}),
        #   "plan 文档里怎么排查 worktree index 并发污染" → tag "plan" 命中另 16 篇
        #   干扰/broad-*.md（score=4），真正相关的 worktree-idx-high/mid 各得 9/6 分、排名更高。
        GoldQuery("plan 文档里怎么排查 worktree index 并发污染",
                  {"主题/worktree-idx-high.md": 2, "主题/worktree-idx-mid.md": 1}),
    ]
    return corpus, queries
