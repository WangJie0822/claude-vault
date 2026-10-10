---
name: vault-loader
description: 自动从知识库按相关性注入 summary 清单到 Claude Code 会话上下文（零配置，安装即生效）。SessionStart 注入项目相关笔记 + 近期工作日志；UserPromptSubmit 按用户问题动态深入。禁用逃生阀：VAULT_LOADER_DISABLE=1、~/.context-vault/.disabled 或 config enabled:false。
argument-hint: "[--doctor]"
allowed-tools: Read, Bash(python3 *)
---

# vault-loader：自动加载知识库

vault-loader 通过两个 hook 把 Obsidian Vault 的相关笔记自动注入 Claude Code 会话上下文，让 Claude Code 启动时即可看到项目历史决策、近期工作进展。

## 触发机制

| Hook | 时机 | 输出 |
|---|---|---|
| SessionStart | 每次会话启动 | 输出 JSON，含两个字段：`additionalContext`（注入正文，逐字喂模型，含完整 wikilink 清单）+ `systemMessage`（用户可见摘要，已清洗终端转义控制字符）；确定性项目固定上下文：项目相关笔记（项目目录∪标签匹配，mtime 倒序）+ 近 7 天工作日志 + 近期 git 提交（无打分排序）|
| UserPromptSubmit | 每次提交 prompt | 输出同结构 JSON（`additionalContext` + `systemMessage`）；prompt 强相关笔记 Top 3 清单；某篇同时满足「话题分 ≥ `relevance.fulltext_topical_threshold`（默认 6）」与「强证据档」时升级为全文（详见下方「全文升级判据」）|

**无候选**时静默退出（除非 `verbose_on_skip: true`）。**真失效**则不再静默——见下方「失效告知」。

### 失效告知

以下四类失效会在 `systemMessage` 里出现一行说明（此前它们只写 stderr，而 stderr 在 hook
上下文没有任何持久化，等于没有输出）：

| 情况 | 说明 |
|---|---|
| `config.json` 解析失败 | 整份回退默认值——丢的不只 `vault_path`，`scoring` 权重、`relevance` 阈值、`keyword_to_tags`、`opt_out_paths` 也一并失效 |
| 配置的 vault 路径不存在 | 本轮未加载任何笔记 |
| 索引文件损坏 / 异常膨胀 | 本轮未加载任何笔记，跑 `/summarize-session` 可重建 |
| 两个 skill 的 vault 路径不一致 | 写入与读取落在不同目录 |

设计上刻意保守，**健康时零新增输出**：
- 「索引尚未生成」（新装还没跑过 `/summarize-session`）、「索引版本待重建」、「vault 里确实没笔记」**都不报**——它们是正常状态，不是故障。
- 同一类失效在 `state_ttl_hours` 窗口内最多提示一次，不会逐条 prompt 刷屏。
- 受 `display.user_visible` 约束（设 `false` 即完全不显示）；不受 `display.verbosity` 约束——那个控制的是注入摘要的详略，不是「要不要告诉你坏了」。

### near-miss 提示（需先开启 `metrics.enabled`）

与上面「失效告知」的四类**真实故障**不同，这条提示的是**效果**而非故障——默认不出现，只有 `metrics.enabled: true`（见下方「本地数据」）时才可能触发：某篇笔记**跨会话累计**「话题分达标却始终没通过闸门入选」达 `metrics.nudge_threshold`（默认 10）次，经 `systemMessage` 出现一行说明，提示你跑 `analyze_metrics.py --review` 标注或检查该笔记的 tags/keywords。冷却是**全局**的（`metrics.nudge_ttl_hours`，默认 168 小时 = 一周），不是逐篇/逐 cwd，避免刷屏。

> **是跨会话不是单会话**（此前本节写作「单会话内」，是错的）：计数落在 `~/.claude/vault-loader-metrics/near_miss_counts.json`，全局共享、不随会话清零。差别很大——单会话内攒够 10 次几乎不可能，而跨会话实测半个月就有 200+ 篇达标。
>
> 只统计 **topical ≥ 3** 的条目：低于这个分的笔记不是「差一点」，是压根不相关，提示你去调它的 tags/keywords 是错的指引。所以某篇明明反复未入选却从不提示，多半是它的话题分本来就太低。

### 全文升级判据

全文注入的资格是**两个条件同时成立**（`scripts/_decision.py::select_fulltext`，决策层与渲染层共用的唯一实现）：

1. **话题分达阈值**——该篇的 `topical_score`（只算 prompt 关键词在 tag / summary / keywords 上的命中，不含 context 底噪）≥ `relevance.fulltext_topical_threshold`，默认 **6**；
2. **强证据档**——命中词构成 ≥2 条独立证据链，且其中含「跨 bigram 合并链」（笔记含你输入的 ≥3 字连续词）或至少 1 个非 CJK-bigram 的完整 token。作用是让「看看日志输出」这类散 bigram 弱命中不再触发全文。

多篇合格时取 `topical` 最强者，同分再比总分 `total`——**不是**取总分排序首位（context 底噪可能把弱话题条目顶到首位、埋掉强话题命中）。

> `user_prompt_submit.fulltext_threshold`（旧值 10）**已废弃、运行时不读**，调它不产生任何效果；实际生效的是上面的 `relevance.fulltext_topical_threshold`。

## 安装（零配置）

作为 `context-vault` 插件安装即生效：Claude Code 从插件自带的 `hooks/hooks.json` 注册 SessionStart / UserPromptSubmit。hook 经 polyglot wrapper 运行，root 取 `CLAUDE_PLUGIN_ROOT`，取不到时按 wrapper 自身位置解析。

新装从 `~/.context-vault/config.json` 读取共享 `vault_path`，默认 `~/.context-vault/knowledge-vault`；公共配置不存在时继续兼容旧 `~/.claude/skills/vault-loader/config.json`，不会自动删除或覆盖旧数据。

> `vault_path` 只维护一处：`~/.context-vault/config.json`。两份 0.9.x legacy 配置仅在 canonical 不存在时只读兼容；若两值冲突，doctor/迁移会停止，不猜测正确路径。

1. 确认 `<vault>/.meta/frontmatter-cache.json` 存在（由 `/summarize-session` 首次运行后自动生成）。
2. 首次启动新会话即生效。

> 旧的「手动在 `~/.claude/settings.json` 注册 hook」装法已废弃；若你之前手动注册过同名 hook，需删除旧注册以免与插件双触发——见 `docs/MIGRATION.md`。

## 配置

配置文件：`~/.context-vault/config.json`。

**文件缺失时写入的是最小占位**（`_config_version` + 一行说明），**不是**全量默认值——你没有显式写进去的键永远走代码里的当前默认，因此默认值随版本演进对你即时生效。只需要写你要改的键，其余留空即可。

> 本节是**完整可配键清单**：占位文件的 `_comment` 指向这里，代码里的 `DEFAULT_CONFIG` 不再被物化到盘上，所以这份文档是分发物中唯一能查全键的地方。

<!-- config-table:begin -->
<!-- 这对锚点是机器契约，不是排版：`tests/test_skill_doc_config_coverage.py` 与将来读配置的
     工具都只在锚点之间解析「一行一个叶子键」的配置表。移动或删除锚点会让守卫响亮失败。
     新增配置键请在本区间内对应的段表里补一行，首列写全路径键名。 -->

### 顶层

| 键 | 默认 | 作用 |
|---|---|---|
| `enabled` | `true` | 总开关；false 永久关停 |
| `dry_run` | `false` | true 时**不真实注入**（`hookSpecificOutput` 字段缺失，不喂模型），仅输出标 `[DRY-RUN]` 的 `systemMessage`，用于灰度验证会注入什么 |
| `vault_path` | `~/.context-vault/knowledge-vault` | Vault 路径 |
| `runtimes.claude.enabled` | `true` | 启停 SessionStart 与 UserPromptSubmit 两个注入入口 |
| `keyword_to_tags` | `{}` | cwd 关键词 → tag 映射 |
| `opt_out_paths` | `["/tmp", "/private/tmp", <本机临时目录>]` | 路径前缀黑名单，命中即跳过 |
| `verbose_on_skip` | `false` | 跳过时输出短提示 |

### `display.*`（用户可见摘要）

| 键 | 默认 | 作用 |
|---|---|---|
| `display.user_visible` | `true` | 生成用户可见的 `systemMessage` 摘要清单；false 时只静默注入 `additionalContext` |
| `display.verbosity` | `"list"` | `"list"`（默认，多行清单）/ `"compact"`（压成单行）/ `"off"`（不产 `systemMessage`）。**只有 `"compact"` 与 `"off"` 被特判**，其余任何值都按 `"list"` 渲染 |
| `display.show_size` | `true` | 在 `systemMessage` 显示注入体积估算 |

### `session_start.*`

| 键 | 默认 | 作用 |
|---|---|---|
| `session_start.enabled` | `true` | SessionStart hook 开关 |
| `session_start.max_notes` | `5` | 注入的项目相关笔记条数上限 |
| `session_start.max_recent_worklogs` | `3` | 近期工作日志条数上限 |
| `session_start.recent_worklog_days` | `7` | 「近期」工作日志的天数窗口 |
| `session_start.max_commits` | `5` | 展示的近期 git 提交条数上限 |
| `session_start.include_tag_matched_notes` | `true` | 是否把标签匹配的笔记并入项目笔记集合 |

> `session_start.min_score` **已废弃**（startup 不再打分，改为 mtime 倒序的确定性上下文），运行时不读。

### `user_prompt_submit.*`

| 键 | 默认 | 作用 |
|---|---|---|
| `user_prompt_submit.enabled` | `true` | UserPromptSubmit hook 开关 |
| `user_prompt_submit.max_notes` | `3` | 清单注入的笔记条数上限（不含 `session_pin_enabled` 的固定注入篇，那是额外名额） |
| `user_prompt_submit.fulltext_max_bytes` | `8192` | 全文注入的字节上限，超出截断 |
| `user_prompt_submit.min_keyword_count` | `2` | prompt 关键词数下限，不足即静默早退（纯 CJK 单词可被 `relax_pure_cjk_single` 放宽） |
| `user_prompt_submit.state_ttl_hours` | `24` | 去重状态、兜底提示冷却的有效期（小时）。**同一个值也约束 `relevance.session_topic` 主题词**：既是已提炼主题词的有效期，也是"本会话是否已尝试过提炼（含失败）"负缓存的有效期——过期后才会重新拉起提炼子进程（F6，整分支终审 2026-09-02） |

> ⚠️ `user_prompt_submit.min_score` 与 `user_prompt_submit.fulltext_threshold` **均已废弃、运行时不读**，改它们不产生任何效果。实际生效的是 `relevance.min_topical_score`（注入闸门）与 `relevance.fulltext_topical_threshold`（全文升级）。两键仍保留在默认表中仅为兼容旧 config。

### `scoring.*`（评分权重表）

| 键 | 默认 | 作用 |
|---|---|---|
| `scoring.exact_project_dir` | `5` | 笔记落在当前项目目录下 |
| `scoring.tag_target_set_hit` | `3` | 笔记 tag 命中项目声明的目标 tag 集合 |
| `scoring.commit_keyword_hit` | `2` | 每命中一个 commit 关键词加一次 |
| `scoring.commit_keyword_cap` | `6` | commit 关键词累计加分上限 |
| `scoring.worklog_cooccur` | `2` | 与近期工作日志关键词共现（单次，不累加） |
| `scoring.mtime_recent_30d` | `1` | 30 天内修改过（与 90d 互斥） |
| `scoring.mtime_recent_90d` | `0.5` | 90 天内修改过 |
| `scoring.prompt_tag_hit` | `4` | prompt 关键词命中笔记 tag。多 tag 命中取**最大** IDF 因子而非累加（防堆砌 tag 刷分） |
| `scoring.prompt_summary_hit` | `2` | prompt 关键词命中笔记 summary |
| `scoring.prompt_keyword_hit` | `5` | prompt 关键词命中笔记 frontmatter 的 `keywords`。刻意 > `prompt_tag_hit`(4)，使策展的精确 keywords 能胜过泛 tag |
| `scoring.session_topic_hit` | `2` | 会话主题词命中一篇笔记的加分（tag/summary/keywords 三面任一，不含 path，多命中取一次不累加）。刻意 < `relevance.min_topical_score`(4)，使主题词零 prompt 关键词命中时无法单独越过精度闸门。**但可以**叠加 prompt 关键词已算出的 topical 分把笔记推过 `relevance.fulltext_topical_threshold`（触发数千字全文注入）——这是已知且被测试锁定的行为，非 bug，见 `tests/test_session_topic_scoring.py::test_topic_word_alone_can_cross_fulltext_threshold` |

> `commit_keyword_hit` / `commit_keyword_cap` 对应的 commit 关键词信号**当前无生产调用方**（两个 hook 都不传 `commit_keywords`），调这两个键暂不影响实际打分；SessionStart 展示近期提交走的是另一条路径。

### `relevance.*`（相关性判定与阈值）

| 键 | 默认 | 作用 |
|---|---|---|
| `relevance.strip_slash_command` | `true` | 剥掉 prompt 开头的 slash 命令名 token，避免命令名本身参与匹配 |
| `relevance.skip_slash_commands` | `[]` | **跳过名单**：prompt 以名单内的 slash 命令开头时，该轮完全不注入（gate 记 `skipped_slash_command`）。用于 `/bug-batch` 这类自带完整任务上下文、不需要知识库的 skill。默认空 ⇒ 不配就零行为变化。写法 `["bug-batch", "commit"]`，大小写不敏感、前导 `/` 可省。**名单项不含 `:`** 时同时匹配裸名与任意 plugin 前缀（`vault` 命中 `/vault` 与 `/context-vault:vault`）；**含 `:`** 则逐字精确匹配（`context-vault:vault` 不会命中裸 `/vault`）。⚠️ 两条边界见下方说明 |
| `relevance.use_keywords` | `true` | keywords 字段参与打分；false 为止血开关，scorer 完全忽略 keywords（不杀整个 loader） |
| `relevance.min_topical_score` | `4` | **注入精度闸门**：话题分低于此值的笔记不进候选 |
| `relevance.fulltext_topical_threshold` | `6` | **全文升级阈值**：话题分达此值**且**满足强证据档才升级为全文（见「全文升级判据」） |
| `relevance.confidence_bands.high` | `6` | 话题分 ≥ 此值且强证据档 → 标「高置信」，否则「中置信」。写进 config 时是嵌套对象：`"confidence_bands": {"high": 6}` |
| `relevance.short_summary_chars` | `20` | summary 短于此字符数时，清单改用文件名作标题 |
| `relevance.split_english_token` | `true` | 英文 token 再按 `[_-]` 切分，治路径碎片黏连 |
| `relevance.en_subtoken_min` | `4` | 英文子片最小长度。设 3 经实证是召回灾难（`bug` 会命中 146 个 tag） |
| `relevance.fallback_hint` | `true` | 话题全失配时输出一行用户可见提示（受 `state_ttl_hours` 冷却） |
| `relevance.skip_non_user_prompts` | `true` | 拦截非用户手输的 prompt（后台 task-notification / 系统注入），这类内容含 UUID、tool-id、路径碎片会污染关键词 |
| `relevance.max_prompt_keywords` | `30` | prompt 关键词数软上限。巨型 prompt（大段粘贴）会让 O(N×M×K) 打分破预算；超限取确定性子集。`0` / `null` 表示不限 |
| `relevance.split_cjk_bigram` | `true` | CJK 按 bigram 分词。false 回到旧 mega-token 行为（中文短词几乎召不回） |
| `relevance.relax_pure_cjk_single` | `true` | 纯 CJK 单 token 放宽 `min_keyword_count` 闸门（配合 relaxed 静默） |
| `relevance.deictic_gate` | `true` | 纯指代/应答型 prompt（「继续执行」「ok」「好的」）直接不注入。**整串匹配**，「继续执行 F1，另外…」不受影响 |
| `relevance.deictic_words` | `null` | `null` 用内置词表；给列表则**完全替换**它（不是追加），用于关掉误伤词或换语言 |
| `relevance.exclude_note_tags` | `["archived"]` | 召回池排除这些 tag 的笔记（SessionStart 与 UserPromptSubmit 共用；`[]` 关闭；`/vault` 手动检索不受影响） |
| `relevance.use_tag_idf` | `true` | tag 命中按 IDF 加权（泛 tag 降权、singleton tag 满分）；false 为止血开关，数值等价回到旧等权行为 |
| `relevance.tag_idf_floor` | `0.5` | 泛 tag 的保底加权因子，值域下界。设 `0` 会让泛 tag 归零、行为剧变；调高（如 `0.7`）减弱降权强度 |
| `relevance.session_topic` | `true` | 会话首轮异步提炼 3-8 个主题词并点名至多 `session_pin_max` 篇，后续轮次用作辅助召回信号（让「继续执行」这类短 prompt 也能召回会话相关笔记）。**1.1.0 起默认开**，代价请先读完：它会拉起一个 `claude -p` 子进程（模型固定 `haiku`、每会话首轮一次、**消耗你的额度**），并**把你本轮 prompt 原文与 `session_topic_top_n` 篇候选笔记的路径+摘要一并发给它**用于提炼关键词。prompt 原文与候选内容经 stdin 传给子进程（不落 argv/进程表），提炼产物（主题词与点名篇）落盘在本机 state 文件；开启 `metrics` 后另落主题词的加盐 hash 与点名篇路径到 metrics 记录（`--purge` 可清）。提炼失败/超时不会每轮重试（负缓存见 `user_prompt_submit.state_ttl_hours`）。**关掉**：本段显式写 `"session_topic": false` |
| `relevance.session_topic_top_n` | `30` | 首轮送进提炼子进程的 admitted 候选篇数；与 `session_topic_project_top_k` 并集后封顶 `session_topic_pool_max`。显式写过的值不会被 `migrate_config` 动 |
| `relevance.session_topic_project_top_k` | `20` | 当前项目 `项目笔记/<仓库名>/`（含项目 CLAUDE.md 的 `extra_paths`）下按 mtime 最新的 N 篇并入首轮提炼候选池；worktree 内按主仓库名取。显式写过的值不会被 `migrate_config` 动 |
| `relevance.session_topic_pool_max` | `50` | 首轮提炼候选池封顶：admitted 前 `session_topic_top_n` 全保留，**被截掉的是项目笔记那一片**——本轮 admitted 实际篇数达到本值时项目笔记一篇都进不了池（要保住项目片就把本值随 `session_topic_top_n` 一起调大；开 `metrics` 后报表「候选池」行会点破「项目笔记恒为 0」）。显式写过的值不会被 `migrate_config` 动 |
| `relevance.session_pin_enabled` | `true` | 会话级固定注入总开关（**需 `relevance.session_topic` 同时为 `true`**——点名来自同一个提炼子进程，`session_topic: false` 时本开关无效）：首轮 LLM 点名的笔记在整个会话（`state_ttl_hours` 内）每轮注入其 summary，**独占额外名额**（不占 `user_prompt_submit.max_notes`），机械召回为空时也注入；闸门早退（指代型/关键词不足/subagent 事件通知/**命中 `skip_slash_commands`**）轮次不注入。关掉则提炼 prompt 与候选池回到 1.2.1 形态。不影响 `/vault-injected` 里的手动钉住（用户的显式操作） |
| `relevance.session_pin_max` | `1` | 首轮最多点名几篇；读端每轮也按此值收口（TTL 内调低立即少注入；`0` 等价于关掉点名），读写两侧硬上限 8。首版取 1：点错时只污染一篇。本会话有手动钉住时，点名实际上限为 `min(本值, 8 − 手动钉住篇数)`（手动先占位，合计不超过 8） |

> ⚠️ **`skip_slash_commands` 的两条边界**（2026-09-16 实测，本机 metrics 加盐 hash × transcript 对拍 335 个 slash 命令轮）：
>
> 1. **只拦命令名所在的那一轮。** skill 跑起来后的后续对话轮次，prompt 不以 `/` 开头，照常注入。这是刻意的粒度选择——想让整段工作都不注入，用 `opt_out_paths`（按目录）或项目 CLAUDE.md 的 `<!-- vault-loader: disable -->`。
> 2. **只对「UPS 收到 `/cmd` 原文」的命令生效。** 实测 `/bug-batch` 12/12 是原文形态、本机制对它成立；但另有一类命令（`/context-vault:summarize-session` 等，涉 83 个 session）UPS 有记录、gate 为空、**确实注入了**，其 prompt 却对不上任何命令形态——把它们加进名单**拦不住，且失败静默**（与「没配这条」现象完全一致）。**若某个命令在名单里却仍触发注入，多半就是这一类，改用上面那两个替代手段。**
>
> 另两条预期（都是既有设计，非缺陷）：**以 `/` 开头且首个 token 命中名单的任何 prompt 都算该命令**，包括你粘贴的代码或路径 —— 注意首个 token 是按**空白**切分的，`/usr/bin/foo 有 bug` 的 token 是 `usr/bin/foo`（不是 `usr`），所以只有名单里写了 `usr/bin/foo` 才会被跳过。失败方向是「这一轮没注入」，复述一遍即可。**命中的一轮不吞诊断**，配置错误、near-miss 提示等 `systemMessage` 仍会显示，跳过的只是 vault 注入。
>
> 顺带影响：命中的轮次也不会触发 `session_topic` 首轮提炼（那是好事——省一次 haiku 调用），但若该会话首轮恰好是被跳过的命令，本会话的主题词与点名会延后到下一轮才建立。

> ⚠️ **阈值与权重耦合**：`relevance` 的三个阈值（`min_topical_score` / `fulltext_topical_threshold` / `confidence_bands.high`）是按当前 `scoring` 权重标定的——默认权重（tag 4 / summary 2 / keywords 5、floor 0.5）下话题分上界为 11。**改 `scoring` 权重后必须同步复核这三个阈值**，否则闸门会整体偏松或偏紧（例如把 `prompt_tag_hit` 提到 8，单个泛 tag 命中就能越过全文阈值）。

### `metrics.*`（决策面指标落盘）

| 键 | 默认 | 作用 |
|---|---|---|
| `metrics.enabled` | `false` | 落盘总开关，**opt-in**：默认关，避免对存量用户在 `/plugin update` 后静默启用新的按提问逐条落盘 |
| `metrics.near_miss_k` | `10` | 每轮记录 excluded 候选中 topical 分最高的 K 条（near-miss，用于事后评估召回边界）。**该上限分别作用于 `near_miss` 与 `near_miss_scorelow` 两个数组**，故单轮最多落 2K 条；后者额外受 topical ≥ 3 约束，实测 80% 的轮次为空 |
| `metrics.admitted_k` | `20` | 每轮落盘 `admitted` 按 `total` 降序保留的展示样本条数上限；真实 Vault 实测单轮 admitted 可达 58~156 条，未截断落盘体积远超预期。截断只影响展示样本，`n_admitted`/`arm_counts` 统计口径基于截断前全量计算，不受影响 |
| `metrics.retention_days` | `90` | 超过此天数的**月份事件目录**（`<YYYY-MM>/*.jsonl`）自动清理，按「每天至多一次」的频率闸门在正常使用时自动触发，无需手动操作；**只清理这部分**——`.salt`/`near_miss_counts.json`/`nudge_ts.json`/`prune_ts.json`/`annotations.jsonl` 都不受它约束，会一直留到你手动 `--purge` |
| `metrics.nudge_threshold` | `10` | 某篇笔记累计 near-miss 次数达到此阈值才提示（77 轮真实 prompt 实测标定，判据偏向沉默，勿放宽） |
| `metrics.nudge_ttl_hours` | `168` | near-miss 提示的**全局**冷却窗口（小时），窗口内所有笔记合计最多提示一次 |

<!-- config-table:end -->

1.0 起数据落 `~/.context-vault/metrics/claude/`；0.9.x legacy 使用 `~/.claude/vault-loader-metrics/`；2.0.0 之前还可能有 `~/.context-vault/metrics/codex/`（主要是被错判的 SessionStart 记录）。`--runtime all --purge` 会不可逆清空这三处（含人工标注）。

想只清 near-miss 计数（比如从 0.9.0 升上来、存量计数按旧判据累加过）用 `--reset-counts`：它只删两份可再生的派生数据（`near_miss_counts.json` / `nudge_ts.json`），事件记录与人工标注一律不碰。**别为这个目的去跑 `--purge`**——那会把不可再生的标注一起删掉。

`.salt` 的保密是加盐 hash 的安全前提，但**该保护平台相关**：POSIX 上由 `0600` 保证，Windows 上 `chmod` 不生效（NTFS 走 ACL），实际可读范围取决于 `~/.claude` 继承的 ACL。详见 README「本地数据」一节。

### 升级后：清理旧版物化残留

旧版首跑会把当时的全量默认值物化写进 `config.json`。这些盘上旧值在 deep-merge 中被当作「用户显式覆盖」，会**压制后续版本的新默认**——该键的默认值演进对你静默失效（典型症状：升级到召回质量修复版后 `scoring.prompt_keyword_hit` 仍停在旧值 3，只拿到半套修复）。本版本起首跑只写最小占位，新装用户不受影响。

插件自带收敛脚本清理这类残留。**先 dry-run 预览**（只读，不改任何文件）：

```bash
VL=$(ls -d ~/.claude/plugins/cache/*/*vault/*/skills/vault-loader/scripts 2>/dev/null | sort -V | tail -1)
python3 "$VL/migrate_config.py"
```

逐条核对清单后再执行 `--apply`（会先备份），如需撤销用 `--restore <备份路径>`。**不要直接删除 `config.json`**——会连同 `vault_path` 一起丢失且不报错。完整流程、三条使用限制（判据是「值等于历史默认」而非「用户没改过」／只处理数值键／备份目录不受 `--path` 影响）见 `docs/MIGRATION.md`。

## 项目级控制

在项目 CLAUDE.md 中添加注释：

- `<!-- vault-loader: disable -->` — 该项目完全停用
- `<!-- vault-loader: tags=[a, b, c] -->` — 显式声明项目关心的 tags
- `<!-- vault-loader: extra_paths=[ProjectA/specs/] -->` — 额外的"项目目录"

## 运行时开关

| 方式 | 作用 |
|---|---|
| `VAULT_LOADER_DISABLE=1` | 本次进程跳过 |
| `~/.claude/.vault-loader-disabled` 文件存在 | 持续跳过直到删除 |
| `config.json.enabled: false` | 持久关停 |

## 故障排查

**先跑健康自检**（只读，不改动任何文件）——它一次性回答下面大部分问题：

```bash
VL=$(ls -d ~/.claude/plugins/cache/*/*vault/*/skills/vault-loader/scripts 2>/dev/null | sort -V | tail -1)
python3 "$VL/migrate_config.py" --doctor
```

输出涵盖：config 是否可解析、vault 路径与是否存在、索引状态与条目数、两个 skill 的
vault 路径是否一致、各开关值。刻意不打印 `keyword_to_tags` / `opt_out_paths` 的内容
（含项目代号与本机路径），贴到 issue 里也安全。

- **没看到注入**：先看 `--doctor` 的「索引状态」。若为 `absent` 说明索引还没生成，跑一次 `/summarize-session`。⚠️ 注意如果 config 解析失败过，`vault_path` 会回退默认值，此时去查 `<vault>/.meta/frontmatter-cache.json` 会查到**错误的目录**——以 `--doctor` 打印的路径为准
- **注入了不相关笔记**：调整 `keyword_to_tags` 映射；或在项目 CLAUDE.md 加 `<!-- vault-loader: tags=[...] -->` 精准声明
- **想看跳过原因**：临时改 `config.json.verbose_on_skip: true`
- **想看会注入什么不实际注入**：改 `config.json.dry_run: true`；`systemMessage` 会标 `[DRY-RUN]` 且不含 `additionalContext`（不喂模型）
- **不想看用户侧提示（仍正常注入模型）**：改 `config.display.user_visible: false`；hook 将只输出 `additionalContext` 无 `systemMessage`

## 与其他系统的关系

- **/summarize-session**：写入 Vault 笔记并维护 frontmatter-cache.json（数据源）；vault-loader 只读
- **/vault skill**：手动深加载通道；vault-loader 注入清单后引导用户/Claude 调 `/vault` 加载全文

## 验收

测试随插件一起分发，跑在插件的 cache 安装目录下（`~/.claude/skills/vault-loader/` 现在只剩运行时的 `config.json`，**没有**测试，在那里跑必然收集不到用例）：

```bash
VL=$(ls -d ~/.claude/plugins/cache/*/*vault/*/skills/vault-loader 2>/dev/null | sort -V | tail -1)
cd "$VL" && python3 -m pytest -q
```

应全部通过。其中 `tests/integration/test_perf.py` 的两条性能守卫是 **500 篇合成 fixture 下的参考基线**（SessionStart < 500 ms、UserPromptSubmit < 300 ms），**不是**对任意规模 Vault 的性能承诺——真实 Vault 的笔记数与 frontmatter 密度都更高，端到端耗时会显著超出该基线（详见该文件的注释）。
