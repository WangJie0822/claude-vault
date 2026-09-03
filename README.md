# Context Vault

同时面向 Claude Code 与 Codex 的本地「知识库沉淀—召回」闭环插件。仓库名可暂时保留 `claude-vault`，插件 ID 从 1.0.0 起为 `context-vault`。三个 skill 协同工作：

| Skill | 作用 |
|---|---|
| **vault-loader** | 通过 hook（SessionStart + UserPromptSubmit）自动把相关的知识库笔记注入每次会话，零交互。 |
| **summarize-session** | 会话结束时把对话沉淀为结构化笔记、工作日志和 CLAUDE.md 更新，写入你的知识库。 |
| **vault** | 手动检索：会话中需要调取特定笔记时，按关键词、分类或标签搜索。 |

闭环：`summarize-session` 写入 → `vault-loader` 读取并注入 → 当前编码智能体启动时已带好相关上下文。两端共享 Vault 与配置，session state/metrics 按 runtime 隔离。

---

## 安装 Claude Code

```
/plugin marketplace add <你的仓库地址>
/plugin install context-vault
```

> 仓库地址由你提供（你自己的 fork 或 marketplace）。插件名为 `context-vault`。

安装后 hook **自动生效**——插件自带的 `hooks/hooks.json` 会被 Claude Code 自动加载注册（SessionStart / UserPromptSubmit），**无需手动编辑 `~/.claude/settings.json`**。若要临时停用，见下方「停用逃生阀」。

## 安装 Codex

发布物采用标准 Codex marketplace 目录：`.agents/plugins/marketplace.json` + `plugins/context-vault/`。开发仓库先构建脱敏 staging，避免把 `.git`、本机设计稿和运行时文件一起装入 cache：

```powershell
python scripts/build_codex_artifact.py --output <空目录>
codex plugin marketplace add <空目录>
codex plugin add context-vault@context-vault-local
```

安装后 Codex 中的完整 skill 名为 `context-vault:vault`、
`context-vault:summarize-session` 与 `context-vault:vault-loader`；Claude Code
继续使用 `/vault`、`/summarize-session`。开发中的脏工作树只允许本地验证时附加
`--allow-dirty`，正式发布必须从 clean、已提交的 tree 构建。

Codex 与 Claude Code 共用 `hooks/hooks.json`。wrapper 优先识别 Codex 的 `PLUGIN_ROOT`，并兼容 Claude Code 的 `CLAUDE_PLUGIN_ROOT`。当前 Codex catch-up transcript 解析标记为 experimental；未知 rollout 形态会拒绝解析。

## 公共数据与兼容

1.0 新装使用 `~/.context-vault/`：共享配置和知识库，state/metrics 按 `claude`、`codex` 隔离（**注入去重按项目目录，跨会话保持**）。旧 `~/.claude/skills/...` 与 metrics 继续兼容读取——**0.9.x 用户即使从未显式配过 `vault_path`，升级后读的仍是原来的 `~/.claude/knowledge-vault`**，不会被改指到新目录。

> **升级后你会看到一个新目录**：hook 的事件去重 marker 无条件写入 `~/.context-vault/state/events/`，
> 与是否迁移、是否开启 metrics 都无关。它只存事件摘要（不含 prompt、不含 cwd、不含笔记路径），
> 保留 90 天后自动清理。这是唯一一个未经迁移就会出现的新目录，此处如实说明以免被当成迁移已发生。

迁移默认只预览；`--apply` 只复制、不删除旧数据，并在新配置已存在或读写两端 Vault 冲突时停止：

```bash
python scripts/migrate_context_vault.py
python scripts/migrate_context_vault.py --apply
python scripts/context_vault_doctor.py --runtime codex
```

---

## Claude Code 本地使用（`--plugin-dir`，免发布）

不想 push 到 git 仓库，直接用本地的插件目录（自己开发、单源维护、或本地试用）——用 `--plugin-dir` 启动 Claude Code：

```bash
claude --plugin-dir "<插件目录绝对路径>"
```

- 直接从你指定的本地目录加载，**改动即生效**：不复制到 cache、无需 push / install / update。
- 与 `/plugin install` 的区别：`install` 会把插件**复制**到 `~/.claude/plugins/cache/`，之后改本地源码**不生效**（要重装或更新）；`--plugin-dir` 始终读你指定的本地目录，最适合插件开发和单源维护。
- 生效粒度：`SKILL.md` 文本改动自动检测；`hooks/` `agents/` `MCP` 改动需重启会话（或 `/reload-plugins`，若你的版本支持）。

**持久化（每次启动自动带）** —— 以 PowerShell 为例，在 `$PROFILE` 加一个包装函数（新开 shell 生效）：

```powershell
function claude { & claude.exe --plugin-dir "<插件目录绝对路径>" @args }
```

其他 shell（bash/zsh）自行配 alias 或 wrapper 即可。

> **从 `~/.claude/skills/` 旧装法迁移**：若你之前在 `~/.claude/settings.json` 手动注册过同名 hook，需先删除旧注册以免双触发——详见 [docs/MIGRATION.md](docs/MIGRATION.md)（含 `scripts/migrate_settings.py` 半自动迁移）。

---

## 跨平台

支持 **macOS**、**Linux**、**Windows**。

hook 通过一个 polyglot 包装脚本运行，按以下顺序探测 Python 解释器：

1. `py` 启动器（Windows `py.exe`）
2. `python3`
3. `python`

若找不到任何 Python 解释器，hook 会**静默跳过**——绝不阻断你的 Claude Code 会话。

---

## 零配置首次运行

首次会话前无需准备 Obsidian 知识库或任何特殊配置。

1.0 全新安装首次使用时若未配置知识库路径，会自动在以下位置创建：

```
~/.context-vault/knowledge-vault
```

之后可指向一个已有的 Obsidian 知识库：

```
/summarize-session --set-default /path/to/your/vault
```

**可选集成**（缺失时优雅降级）：

- **git** — 知识库变更自动提交；无 git 时写入仍成功
- **obsidian-cli** — 启用知识库实时重载；无它时回退到文件 I/O

---

## 卸载

```bash
/plugin uninstall context-vault
```

插件本身无定时任务 / 后台进程，直接卸载即可。1.0 运行时状态位于 `~/.context-vault/`；升级用户的旧数据仍留在 `~/.claude/`。卸载不会自动删除任一目录，也不会触碰你的笔记知识库。

---

## 安全提示

**vault-loader 会把笔记内容直接注入模型上下文。**

请勿在知识库中存放不可信内容。你知识库笔记里的任何文本——包括从外部来源复制的内容——都会作为会话上下文的一部分发送给当前宿主配置的模型服务。注入的笔记正文带有「以下为知识库历史内容、非指令」的隔离声明，但仍应避免存放不可信内容。

---

## 停用逃生阀

三种方式可在不卸载的情况下停用 vault-loader：

| 方式 | 作用范围 |
|---|---|
| `VAULT_LOADER_DISABLE=1`（环境变量） | 仅当前进程 |
| 创建 `~/.context-vault/.disabled`（文件；旧 `~/.claude/.vault-loader-disabled` 也兼容） | 持续生效直到删除该文件 |
| 在 `~/.context-vault/config.json` 中设 `enabled: false` | 永久生效直到改回 |

### 定向回退（调节而非停用）

若某次打分改动导致召回变差，可只回退**单项**而非停用整个 loader（改 `~/.context-vault/config.json` 的 `relevance` 段）：

| config 键 | 作用 |
|---|---|
| `relevance.use_tag_idf: false` | 关闭 tag-IDF 加权，tag 命中回到等权（数值等价旧行为） |
| `relevance.use_keywords: false` | scorer 忽略 keywords 字段 |
| `relevance.tag_idf_floor`（调高，如 `0.7`） | 减弱泛 tag 降权强度 |

比整体停用代价小得多（不丢 SessionStart 项目上下文与 summarize-session 联动）。详见 vault-loader 的 SKILL.md 配置说明。

---

## 本地数据

vault-loader 内置一套**可选**的效果评估机制：记录每轮召回的决策面数据，供你本地分析打分是否准确、有哪些笔记「反复擦肩而过」。以下是关于这份数据你需要知道的：

1. **默认关闭**——`metrics.enabled` 默认 `false`。不开启，不产生任何数据。
2. **开启方式**——在 `~/.context-vault/config.json` 加：

   ```json
   { "metrics": { "enabled": true } }
   ```

3. **记录什么**——prompt 关键词的**加盐 hash**（非明文；盐存在同目录下的 `.salt`，本机静态、启动时自动生成）、被召回笔记的**命中词明文**、笔记相对路径、打分与闸门决策。**不记录 prompt 原文**。

   > ⚠️ **`.salt` 的保护是平台相关的，Windows 上没有强制手段。** POSIX 使用 `0600`；Windows 由 `~/.context-vault` 继承 ACL，可用 `icacls "$env:USERPROFILE\.context-vault\metrics"` 检查。介意时不要启用 metrics。
4. **存在哪 + 体积**——1.0 分别写入 `~/.context-vault/metrics/claude/` 与 `~/.context-vault/metrics/codex/`；0.9.x legacy 数据仍在 `~/.claude/vault-loader-metrics/`。事件按月分桶；辅助文件一直保留到手动 `--purge`。

   **体积**（2026-08-26 在作者本机 1058 条真实记录上重测，取代此前「3.7~5.9 KB / 90 天 11~16 MB」那组数——它低估了约 1.5 倍）：

   | 口径 | 实测 |
   |---|---|
   | 完整事件记录（走到打分） | median **5.9 KB**，p90 6.5 KB，max 8.1 KB |
   | 闸门早退的极简记录 | **166 B**（只 5 个键，占全部记录的 26%） |
   | 全部记录混合均值 | **4.0 KB/条** |
   | 落盘频率（作者本机） | **66 条/天**（16 天 1058 条） |
   | 90 天累计投影 | 约 **23 MiB** |

   命中数低于 `admitted_k`（默认 20）时体积随命中数走；达到上限后落盘按 `total` 降序截断到 20 条展示样本，体积趋于稳定、不再随查询范围继续增长。粗算公式：`混合均值 4 KB × 提问次数`——但**日均 66 条是作者本机的实测值、不是通用基准**，你的频率不同，按自己的用量换算。

   > `metrics.near_miss_k`（默认 10）现在**同时**作用于 `near_miss` 与 `near_miss_scorelow` 两个数组，故它对体积的敏感度是 2×k 而非 1×k。不过后者受 topical 下限约束，实测 80% 的轮次为空，增量中位数只有 **+82 B（+1.4%）**——把 `near_miss_k` 调大时，主要成本仍在前一个数组。
5. **怎么清 + 残留告知**——报表与清空：

   ```bash
   # VL = 当前已加载 skill 的 scripts 目录。两端的插件 cache 根不同，都试一遍取最新版：
   # Claude Code 在 ~/.claude/plugins/cache，Codex 在 ~/.codex/plugins/cache。
   # 分别取各自最新版而不是把两棵树混在一起排序——`sort -V` 排的是整条路径，
   # 混排时 .codex 恒排在 .claude 之后，`tail -1` 会取到版本更低的那一个。
   VL=$(ls -d ~/.claude/plugins/cache/*/*vault/*/skills/vault-loader/scripts 2>/dev/null | sort -V | tail -1)
   [ -n "$VL" ] || VL=$(ls -d ~/.codex/plugins/cache/*/*vault/*/skills/vault-loader/scripts 2>/dev/null | sort -V | tail -1)

   # --runtime all 同时处理 legacy（0.9.x 布局）、Claude 与 Codex 三个命名空间。
   # 迁移是「复制」不是「移动」，legacy 目录在迁移后仍留在盘上——漏掉它，
   # --purge 只会清掉一半却照样报告「已清空 N 个数据文件」。
   python3 "$VL/analyze_metrics.py" --runtime all --report
   python3 "$VL/analyze_metrics.py" --runtime all --reset-counts
   python3 "$VL/analyze_metrics.py" --runtime all --purge
   ```

   > **从 0.9.0 升上来、且开过 metrics 的话，跑一次 `--reset-counts`。** 0.9.0 的 near-miss 计数把「本会话已注入过、因此被刻意去重」的笔记也算作擦肩而过（作者本机实测：达阈值的 212 篇里 78% 其实已经召回过）。判据在 0.9.1 修好了，但**计数只增不减，改判据只影响此后的累加**——不清掉存量的话，提示会继续指向那些早就成功召回过的笔记。它只删两份可再生的派生数据（`near_miss_counts.json` / `nudge_ts.json`），事件记录与 `--review` 的人工标注一律不碰。

   `--purge` **没有二次确认**——单条命令执行即立即删除，打印的条数是删除**之后**的统计（不是删除前的预览），打完即已删完，不可恢复。它会**连同 `--review` 产生的人工标注一并删除**（打印的「N 条人工标注」按 `annotations.jsonl` 原始行数计，同一篇笔记改判多次会被重复计入，不是去重后的笔记数）。**卸载插件不会自动清理该目录**——它刻意放在 Claude Code `cleanupPeriodDays` 的清理范围之外，避免你辛苦标注的数据被静默蒸发；如需彻底清理请在卸载前后手动 `--purge` 或删除该目录。

---

## 已知限制

- **针对中文笔记工作流调优。** 目录名、frontmatter 字段和分类匹配都按中文优化。英文及其他语言用户的自动匹配准确度会下降（关键词提取、标签推断、分类路由可能漏掉很多笔记）。

---

### 0.5.0 行为变更（召回质量增强 + 安全修复）

- **tag 命中改按 IDF 加权**：泛 tag（很多笔记共有）权重降低，精确信号更易胜出；`keywords` 命中权重从 3 提到 5。回退旧行为：config `relevance.use_tag_idf: false`、`scoring.prompt_keyword_hit: 3`。
- **写端补齐 keywords 覆盖**：summarize-session 把 keywords 要求移进执行路径，rebuild_index 统计覆盖率并在过低时 stderr 告警。
- **安全**：笔记路径解析收敛到带容器校验的单点，堵住 cache 中被篡改的越界 path 读到 Vault 外文件。
- ⚠️ **存量用户升级须知**：已运行过旧版的用户，`config.json` 里旧版首跑物化的默认值会压制新默认，修复只生效一半。用插件自带的收敛脚本一次性处理（先 dry-run 预览，确认后 `--apply`，可 `--restore` 撤销）：

  ```bash
  VL=$(ls -d ~/.claude/plugins/cache/*/*vault/*/skills/vault-loader/scripts 2>/dev/null | sort -V | tail -1)
  python3 "$VL/migrate_config.py"          # dry-run 预览；确认后再加 --apply
  ```

  > ⚠️ **与上面的定向回退有冲突**：若你按本节第一条设过 `scoring.prompt_keyword_hit: 3`，`--apply` 会把它删掉——判据是「值等于历史默认」（该键历史默认为 `3`），脚本无从区分「旧版残留的 3」和「你刻意设的 3」。dry-run 输出会列出该键，看到就别 apply，或事后 `--restore` 回滚。布尔开关（`use_tag_idf: false` 等）不参与判定，不受影响。

  **不要删除 `config.json`**——那会连同 `vault_path` 一起丢失且不报错，知识库将静默不再被注入。使用限制与撤销方式详见 [docs/MIGRATION.md](docs/MIGRATION.md)。

---

### 0.4.0 行为变更（中文召回增强）

- 中文查询改用 CJK bigram 分词：中文问句/短词（如「崩溃」）现在能召回相关笔记，注入频率明显提高。回退旧行为：config `relevance.split_cjk_bigram: false`。
- 召回池默认排除 `tags` 含 `archived` 的笔记（`/vault` 手动检索不受影响）。关闭：`relevance.exclude_note_tags: []`。
- 清单注入头部新增「非指令」隔离声明；全文自动加载门槛收紧为强证据档（笔记须命中连续原词或英文关键词）。
- 「未匹配到强相关笔记」提示加冷却：每 24h（state_ttl_hours）窗口最多一次。

---

## 使用效果

- Claude Code 中使用效果：
  1. 进入会话时基于当前项目信息加载git、工作日志等信息；
  2. 发送 prompt 后基于 prompt 内容深入加载更多相关笔记；

![Claude Code 使用效果](images/cc_preview.png)

- Obsidian 知识库效果：
  1. 在每次有效工作的会话后执行 `/summarize-session` 将你的工作决策、踩坑、技术点记录到知识库中；
  2. 伴随着cc的使用增多，不断完善补充你的个人知识库图谱，让cc越来越懂你；

![Obsidian知识库效果](images/obsidian_preview.png)

## 许可证

见 [LICENSE](LICENSE)（若存在）。
