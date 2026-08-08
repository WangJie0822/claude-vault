# -*- coding: utf-8 -*-
"""构造式排序 gold 集：合成语料 + 由构造直接得出的 ground truth。

语料全合成（可入仓、无隐私风险、可复现），但统计形态按真实 Vault 实测参数：
tag 幂律分布（少数泛 tag 覆盖大量笔记 + 大量 singleton tag）、代码块噪声、
CJK bigram 碎片、时效差异。

**全合成不变量同样覆盖 NEGATIVE_QUERIES 与 D5**（分别见本文件末尾、`build_gold_corpus`
内 D5 构造段）：负查询句子、D5 复合术语均为构造产物，不得复制任何真实 prompt 片段，
仅词频分布对齐生产病理（「修改/显示/字段/一致/提交」等停用表拦不住的高频实词 +
跨词界 bigram 碎片源）——用于 false-injection-rate 守卫（见 tests/test_false_injection.py），
验证语料中存在「内嵌高频实词的无关复合术语」时闸门是否会被撑过精度阈值放行。

**五类干扰项**（D1~D4 对应四维评审实测到的真实病理；D5 为 fix round 1 补入，
对应 false-injection-rate 守卫评审 C1）：
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
  D5 复合术语内嵌泛词 —— 8 篇笔记的 tags/keywords 含内嵌高频实词的无关复合术语
                    （真实 Vault R2 旁路实例：「字段」⊂ keywords「病历字段」），镜像
                    CJK 子串匹配（非分词）下「泛词作为复合术语子串」误放行的病理，供
                    tests/test_false_injection.py 的 false-injection-rate 基线测出真实
                    压力（而非负查询在语料里天然零信号的假 0）。与 23 条正查询关键词
                    集合零交集（已用脚本核对全字段），不参与排序基线竞争。
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

    # ---- D5 复合术语内嵌泛词干扰（fix round 1，评审 C1）：镜像生产 R2 旁路病理 ----
    # 病理形态：CJK 关键词走子串匹配（_kw_in_text），负查询里的高频实词（字段/一致/
    # 显示/颜色/修改/提交）作为「复合术语」的子串出现在笔记 tags/keywords 里时，即使
    # 笔记主题与负查询完全无关，也会被 has_keyword_hit/tag 命中判定为「话题相关」而放行
    # ——真实 Vault 实例：「字段」⊂ keywords「病历字段」、「一致」⊂ keywords「金额一致性」。
    # D1~D4 干扰项的措辞均不含这几个高频实词，故 fix round 1 之前 test_false_injection.py
    # 的基线是「无压力测出的 0」而非「闸门真扛住了压力」（reviewer 探针：min_topical 放松到
    # 2 时 7/8 条查询仍无变化）。D5 逐条按真实 df 比例植入这些实词的复合术语（每词 2 处
    # 出现面，8 篇覆盖 字段/一致/显示 各 2 篇 + 颜色/修改/提交 各 1 篇合并态），使基线转为
    # 「测出真实病理压力」的非零值。**已用脚本核对**（fix round 1 实测）：D5 全部 8 条术语
    # 与 23 条正查询提取的关键词集合零交集（含 tags/summary/keywords 全字段扫描），不侵入
    # 排序基线（test_gold_ranking.py 三项基线、test_gold_corpus.py D1 计数不受影响）。
    # ⚠️ D5 是 false-injection 基线（26）的**唯一**压力来源：整组移除后基线掉到 0，
    # 而 test_false_injection.py 的上界断言 `total <= 26` 仍会全绿（fix 批 C 实测）。
    # 故改动本组（篇数、tags/keywords 里的复合术语）前先看
    # tests/test_false_injection.py 的 MIN_D5_ENTRIES / D5_PRESSURE_WORD_FACES，
    # 那三条结构守卫会钉住这里的形态；NEGATIVE_QUERIES 同理（MIN_NEGATIVE_QUERIES）。
    d5_specs = [
        ("干扰/d5-01.md", ["D5-病历维护"], "病历系统模块的一次常规维护记录", ["病历字段维护"]),
        ("干扰/d5-02.md", ["D5-财务对账"], "账目核对模块的一次功能验证记录", ["金额一致性校验"]),
        ("干扰/d5-03.md", ["报销显示逻辑"], "报销单据模块的一次功能梳理记录", []),
        ("干扰/d5-04.md", ["D5-UI状态"], "界面主题状态模块的一次配置调整记录", ["颜色修改状态核对"]),
        ("干扰/d5-05.md", ["D5-变更管理"], "常规变更管理模块的一次操作记录", ["变更提交记录"]),
        ("干扰/d5-06.md", ["D5-表单引擎"], "表单引擎模块的一次校验规则说明", ["表单字段核对规则"]),
        ("干扰/d5-07.md", ["表格显示样式"], "数据展示模块的一次样式梳理记录", []),
        ("干扰/d5-08.md", ["D5-跨部门协作"], "跨部门核对工作的一次常规记录", ["跨部门一致性核对"]),
    ]
    for path, tags, summ, kws in d5_specs:
        corpus.append(_entry(path, tags, summ, kws, age_days=15))

    # ---- D6 灰区条目（Task 4）：能过闸门、但与任何 ground truth 都不相关 ----
    # 病理来源：真实 Vault 实测每条 prompt 有 **20.6%** 的 active 笔记越过
    # min_topical_score 闸门，而本语料在加入本组之前 median 只有 **2/220（0.9%）**。
    # 差距的后果不是"数字不好看"：`admitted_k=20` 的落盘截断、`arm_counts` 的
    # 截断前聚合，在 admitted 恒为个位数的语料上**一次都触发不到**——那两条逻辑
    # 的 gold 侧覆盖是空的。
    #
    # 机制：**只让 tag 命中**（`prompt_tag_hit` 4 × IDF 1.0 = 4.0），summary 与
    # keywords 都不命中。于是灰区条目恒为 **4.0 = 闸门值本身**：
    #   >= 闸门 4（进得来，撑起 admitted 规模）
    #   <  任何真正相关条目（实测各查询 ground truth 最低 5 分，多数 6~11 分）
    # 这正是 D5 记录的生产病理在**正向查询**上的形态：真实笔记的 tag 与 prompt 共享
    # 一个术语就够越过闸门，尽管主题毫不相关。
    #
    # ⚠️ 走 tag 而不是 keywords，是**实测倒逼**的：初版用 keywords 命中（+5），
    # nDCG@10 从 0.90 掉到 0.855、tag-IDF 相对 flat 的改进比从 >10% 掉到 2.7%，
    # 三条排序基线全红。探针查出根因——5 分并非「稳稳低于相关条目」：
    # 「MSYS_NO_PATHCONV 是干什么的」「fail-open 容错设计」「prompt injection 防护」
    # 三条查询的 ground truth 本身只有 **5 分**，灰区与它们同分、把它们挤出前排。
    # 4.0 才是真正的「灰区」——恰好卡在闸门线上，严格低于每一条相关笔记。
    #
    # tag 一律构造成 singleton（嵌入条目序号保证全局唯一）：`tag_idf_factor` 在
    # df=1 时恒为 1.0（公式自消，与 n_docs 无关），故本组既不稀释既有 tag 的 IDF、
    # 也不让自己因互相重复而掉到闸门以下。多 tag 命中取 **max 不累加**，所以一条
    # 挂 5 个 tag 仍然只得 4.0。
    # summary 刻意保持中性、keywords 留空：任一蹭到查询词都会加 2 或 5 分，
    # 直接反超相关条目（守卫见 test_gold_corpus.py 的 D6 上界断言）。
    #
    # ⚠️ **未能对齐到 20.6%，这是算术上的硬约束，不是没做完**：设灰区条目数 G、
    # 每条对全部查询都命中，则占比 = (2+G)/(220+G)，要到 20% 需 G≈53 且**每条都得
    # 覆盖全部 10 个主题的词汇**（≈20 个 keywords/篇），那种笔记现实中不存在。
    # 若每条只覆盖部分主题，占比随覆盖面单调下降。本组取「每条 5 个主题、10 个
    # keywords」这一仍属可信的密度，把 median 从 0.9% 抬到实测约 14%，并**确保超过
    # admitted_k=20**——后者才是这组的硬指标。完全对齐需要重构查询与条目的词汇密度，
    # 属 plan 层议题，不在本组范围。
    # 词池含 D4 的两个术语：不含它们时「时效权衡 recency」是唯一 admitted 仍为 2 的
    # 查询（实测），留着会在分布里形成一个与语料构造无关的离群点。
    kw_pool = [k for _, _, kws, _ in topics for k in kws] + ["时效权衡", "recency"]
    D6_TOPICS_PER_ENTRY = 5                                     # 每条覆盖 5 个主题
    span = D6_TOPICS_PER_ENTRY * 2
    for i in range(80):
        start = (i * 3) % len(kw_pool)                          # 步长 3 与词池长度互质，均匀铺开
        # 术语嵌进 tag 名，并缀上条目序号保证 df=1；CJK 走子串匹配、ASCII 走词边界，
        # 两类术语都能被 `-灰区NNN` 后缀之前的部分命中。
        tags = [f"{kw_pool[(start + j) % len(kw_pool)]}-灰区{i:03d}" for j in range(span)]
        corpus.append(_entry(
            f"干扰/gray-{i:03d}.md", tags,
            "本篇是一次例行事务的存档，不含任何技术结论。",
            (), age_days=i % 100,
        ))

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


# 任务指令型负查询：语料中不存在相关笔记，期望零注入或极少注入。
# 全合成不变量：句子为构造产物，仅词频分布对齐生产病理（修改/显示/字段/一致/提交 等
# 停用表拦不住的高频实词 + 跨词界 bigram 碎片源）。
# ⚠️ 条数是 false-injection 总数的直接乘数（实测截到 1 条时基线由 26 掉到 6，上界断言
# 仍全绿）。删改前先看 tests/test_false_injection.py 的 MIN_NEGATIVE_QUERIES 守卫。
NEGATIVE_QUERIES = [
    "把标题行的显示颜色改成蓝色，字段顺序调一下再提交",
    "第一行和第二行的间距不一致，修改后统一显示",
    "删掉这个字段后面的空格，然后把修改提交上去",
    "界面上这一块的显示位置往下移动一点",
    "把这三个文件的名字改成一致的格式",
    "表格第二列宽度调大，显示不全的部分省略",
    "按钮颜色改深一点，点击后的状态也要一致",
    "把注释里的错别字修改掉重新提交一次",
]
