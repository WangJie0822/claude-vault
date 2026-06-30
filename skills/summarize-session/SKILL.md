---
name: summarize-session
description: 会话结束时总结对话并沉淀到知识库：记录今日进展、固化技术决策、归档 spec/plan 文档、更新工作日志、批量补录历史会话（--catch-up）。含实质性工作（代码修改/技术决策/问题排查）的会话结束前，主动建议用户运行 /summarize-session 沉淀知识；对话中产生了历史决策或踩坑记录时，在提示语中同步说明。
argument-hint: "[--no-log] [--quick] [-f] [--catch-up [N]] [--vault <路径>] [--log-dir <路径>] [--set-default <路径>] [--show-config] [--backfill-archive]"
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(date *), Bash(basename *), Bash(git *), Bash(mkdir *), Bash(ls *), Bash(cp *), Bash(rm *), Bash(python3 *), Bash(pgrep *), Bash(zsh *), AskUserQuestion
---

# 会话总结与知识沉淀

将当前对话中产生的有价值信息，结构化地沉淀到 Obsidian 知识库和 CLAUDE.md 中，确保跨会话的知识延续。

## 脚本路径（重要）

本 skill 的脚本随插件分发，安装后在**版本钉死的插件 cache** 里，**不在** `~/.claude/skills/` 源目录（插件化后源目录已退役、只剩 `__pycache__`，旧的 `scripts/` 子路径已无脚本）。每个要跑脚本的 Bash 调用，**在同一次调用内先**用下面这行定位脚本目录（Bash 工具不跨调用保留变量，故每次都要重设；下文 ```bash 代码块已内联此行）：

```bash
SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
```

之后统一用 `python3 "$SS/<脚本名>.py"` 调用。

- **禁用 `$CLAUDE_PLUGIN_ROOT`**：它在 skill 的 Bash 上下文未注入（实测为空），不能用作前缀。
- **禁用源目录形式**：旧文档把脚本写成 `~/.claude/` 下 `skills/<本 skill>` 的 `scripts/` 子路径——插件化后那里无脚本，跑必 No such file。
- **`--plugin-dir` 本地开发模式**（不走 cache）：把 `SS` 直接指向你本地仓库的 `skills/summarize-session/scripts`。
- **注意**：`config.json` 等 **runtime 态仍在** `~/.claude/skills/summarize-session/`（稳定持久位置），下文对它的引用保持不变。

## 知识库路径解析

skill 需要知道 Obsidian Vault 的路径才能正确写入笔记。按以下优先级确定路径：

1. **命令行参数**：`/summarize-session --vault /path/to/vault`
2. **配置文件默认值**：读取 `~/.claude/skills/summarize-session/config.json` 中的 `default_vault_path`
3. **默认知识库**：如果以上都未指定，回退到 `~/.claude/knowledge-vault`（与 vault-loader 默认一致，零配置闭环不断裂；该默认路径不存在时由路径解析流程 `mkdir -p` 创建）

### 参数说明

| 参数 | 作用 | 示例 |
|:-----|:-----|:-----|
| `--vault <path>` | 本次使用的知识库路径（一次性） | `/summarize-session --vault ~/MyVault` |
| `--log-dir <path>` | 本次使用的工作日志目录（一次性） | `/summarize-session --log-dir ~/WorkLog` |
| `--set-default <path>` | 将路径保存为默认值并退出 | `/summarize-session --set-default ~/.claude/knowledge-vault` |
| `--no-log` | 仅记录笔记和 CLAUDE.md，跳过工作日志 | `/summarize-session --no-log` |
| `--quick` | 轻量模式：只做笔记 + CLAUDE.md，跳过工作日志、文档归集、Memory 沉淀 | `/summarize-session --quick` |
| `-f` / `--force` | 强制模式：跳过第三步计划确认，按默认值直接执行（计划仍打印供追溯）；不自动跑 baseline / `--fix-frontmatter` / `fix_links` 等需授权操作 | `/summarize-session -f` |
| `--catch-up [N]` | 回溯模式：扫描最近 N 天（默认 7）未总结的历史会话，补充总结 | `/summarize-session --catch-up 3` |
| `--show-config` | 显示当前配置并退出 | `/summarize-session --show-config` |
| `--backfill-archive` | 一次性扫描所有无 vault_path 的 pending-docs 条目并复制到 Vault | `/summarize-session --backfill-archive` |
| （无参数） | 使用默认配置或 `~/.claude/knowledge-vault` | `/summarize-session` |

### keywords backfill（可选，手动 opt-in）

`scripts/enrich_keywords.py` 给存量笔记一次性补 `keywords`（供 vault-loader 召回）。**调付费 `claude -p --model haiku`、每篇一次**，须手动运行、不接自动管线。

- 先 `--dry-run` 看会处理哪些篇；`--limit N` 限制本次最多发起 N 次付费调用。
- ⚠️ **成本告警**：大 vault 全量 backfill = 笔记数 × 一次 haiku 调用，先用 `--limit` 小批试。
- 跑完会改笔记 frontmatter，**需再跑 `rebuild_index.py` 刷新 cache** 召回才生效（脚本结束会提示）。
- 定位器（每个 Bash 块内联）：
  `SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)` 后 `python3 "$SS/enrich_keywords.py" --vault "$VAULT" --dry-run`。

### 配置管理

配置文件 `~/.claude/skills/summarize-session/config.json`：

```json
{
  "default_vault_path": "~/.claude/knowledge-vault",
  "work_log_dir": "~/.claude/knowledge-vault/worklog"
}
```

- `--set-default <path>`：读取配置 → 更新 `default_vault_path` → 写回 → 确认输出
- `--show-config`：读取配置并展示（文件不存在时提示未配置）

### 路径解析流程

执行总结前，按以下步骤确定路径：

**Vault 路径**（记为 `$VAULT`）：
1. 解析 `$ARGUMENTS`，检查是否包含 `--vault <path>`
2. 如果没有，读取 `~/.claude/skills/summarize-session/config.json` 获取 `default_vault_path`
3. 如果配置文件也没有，回退到 `~/.claude/knowledge-vault`（与 vault-loader 默认一致）
4. 验证 `$VAULT`：存在且是目录则继续；若 `$VAULT` 是上一步回退的默认 `~/.claude/knowledge-vault` 且尚不存在，`mkdir -p "$VAULT"` 创建后继续；若是 `--vault` 显式指定的路径却不存在，则报错（不替用户创建其显式指定的路径）

**工作日志目录**（记为 `$LOG_DIR`，`$NO_LOG=true` 时跳过）：
1. 解析 `$ARGUMENTS`，检查是否包含 `--log-dir <path>`
2. 如果没有，读取配置文件获取 `work_log_dir`
3. 如果配置文件也没有，默认使用 `$VAULT/工作日志`
4. 目录不存在时自动创建

确定路径后，知识笔记在 `$VAULT` 下操作，工作日志在 `$LOG_DIR` 下操作。

## 何时应该建议总结（主动引导规则）

以下情况 Claude **应主动提醒**用户考虑运行 `/summarize-session`（这是手动建议，而非自动触发）：

- 会话即将结束，且本次对话包含以下任一实质性工作：代码修改与提交、技术方案选型决策、问题根因定位记录、新功能或新工具的使用经验、踩坑记录或避坑建议
- 用户明确说"今天差不多了"/"先到这里"/"收工"等会话收尾信号
- 会话中产生了跨会话有参考价值的结论（选择了某方案并说明了原因、确定了某项配置规范等）

**不需要建议总结的情况**：纯问答（无写操作）、一次性调试（无沉淀价值）、用户明确说不需要记录。

这是引导语义：Claude 告知用户"此次会话值得用 `/summarize-session` 沉淀"，由用户决定是否运行；Claude 不自行触发总结。

## 核心理念

对话中产生的知识分为两类，处理方式不同：

1. **指令性内容**（偏好、规则、约束）→ 更新 CLAUDE.md
2. **知识性内容**（决策记录、技术方案、项目进展）→ 创建/更新 Obsidian 笔记

不是所有对话都值得记录。只提取**对未来工作有参考价值**的内容，忽略临时性的调试过程和一次性操作。

## Obsidian CLI 前置依赖

Vault 内资源操作优先通过 Obsidian 官方 CLI（`obsidian` 命令）完成，通过 `scripts/obsidian_cli.py` 统一封装。

**运行条件**：
- Obsidian 1.12.4+ 已安装并在 GUI 中 Register CLI（Settings → General → Command line interface 启用并注册）
- Obsidian GUI 进程运行中（由 skill 首步的 `probe()` 探测确认）

**未满足时**：自动降级到文件 I/O。降级不影响 skill 功能，仅会导致：
- Obsidian 内正在打开的笔记与文件系统短暂不一致（Obsidian 下次重扫/重启后同步）
- 查重走 `Glob + Grep` 而非 Obsidian 全文索引

**调用约定**：skill 中对 Vault 内资源（`$VAULT/` 下 `.md`）的操作统一通过：

```bash
SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
python3 "$SS/obsidian_cli.py" --vault "$VAULT" <op> [...]
```

命令映射、降级矩阵、排查请见 `references/obsidian-cli-ops.md`。

**Vault 外资源保持原状**（不经过封装层）：
- 全局 `~/.claude/CLAUDE.md`
- 项目级 CLAUDE.md
- `~/.claude/projects/*/memory/`
- `$VAULT/.meta/pending-docs.json`（JSON 状态文件，Obsidian 不感知）
- `summarized-sessions.json`、`frontmatter-cache.json`

## 并发安全

用户可能在多个窗口同时执行 `/summarize-session`，以下共享资源需要防止写冲突：

| 资源 | 保护机制 |
|------|---------|
| `summarized-sessions.json` | 脚本内置文件锁 + 原子写入，无需额外处理 |
| `frontmatter-cache.json` | 脚本内置文件锁 + 原子写入，无需额外处理 |
| `$VAULT/CLAUDE.md` 索引区 | 脚本内置文件锁 + 原子写入，无需额外处理 |
| 工作日志 `YYYY年/MM月/YYYY-MM-DD.md` | **写入前必须 Read 获取最新内容**，已有文件用 Edit 追加 |
| Obsidian 笔记（追加） | **写入前必须 Read 获取最新内容**，用 Edit 追加 |
| CLAUDE.md（偏好/规则） | **写入前必须 Read 获取最新内容**，用 Edit 追加 |
| `pending-docs.json` | 由 `sync_pending_docs.py` 维护；真死条目（无 vault_path + original_missing + path 不存在）由 sync `--apply` 自动 prune 删除（incremental 与 backfill 模式均触发；删前自动 `.bak` 备份，仅保留最近一次 prune 前状态），可重建 vault_path 的由 `reclaim_and_prune.py` 转 active；**LLM 仍禁止手动删/改 pending-docs.json**（条目状态由字段反映：`vault_path` 已归集、`original_missing` 原文件失踪、`path_invalid` / `denied_sensitive` 拒绝归集）|

**核心原则**：对已有文件的修改，始终使用 **Read → Edit** 模式而非 Read → Write 覆写。Edit 基于字符串匹配，能天然检测到文件被其他窗口修改的情况——如果 `old_string` 匹配失败，说明文件已被并发修改，此时重新 Read 后再次 Edit。

## 强制模式行为差异（`-f` / `--force`）

`/summarize-session -f` 在**手动流程**基础上跳过交互确认，按默认值直接推进到执行，适合无人值守 / 批量收尾。

行为差异（相对正常手动流程，仅去掉确认 gate）：

1. **跳过第三步计划确认**：仍把结构化计划打印到输出（供追溯），但**不弹 AskUserQuestion**，直接进入第四步执行。
2. **默认值**：工作日志照写（除非叠加 `--no-log` / `--quick`）；文档归集、Memory 沉淀照常（除非 `--quick`）；git commit 走增量。
3. **不自动跑需授权的重操作**：`--baseline` 全量提交、`--fix-frontmatter`、`fix_links.py` 在 `-f` 下**一律不自动执行**，仅在第五步输出中作为建议列出——保持对既有笔记 / 全量提交的非破坏性。
4. **正交组合**：可与 `--quick` / `--no-log` / `--vault <path>` / `--log-dir <path>` 自由组合。

其余流程（路径解析、扫描、写入、索引重建、标记已总结、git commit）与正常手动模式完全一致。

## 执行流程

### 第一步：初始化与信息采集

1. 解析 `$ARGUMENTS` 中的参数
2. 如果是 `--set-default` 或 `--show-config`，执行对应操作后结束
3. 如果是 `--catch-up [N]`，进入**回溯模式**——详见 `references/catch-up.md`，不执行下方正常流程
4. 检查模式标志：
   - `--quick` → 设置 `$QUICK=true`（隐含 `$NO_LOG=true`、`$SKIP_DOC_COLLECT=true`、`$SKIP_MEMORY=true`）
   - `--no-log` → 设置 `$NO_LOG=true`
   - `-f` / `--force` → 设置 `$FORCE=true`（强制模式，跳过第三步确认，详见前文「强制模式行为差异」）

⚡ **并行执行**（同一消息中发出多个 tool calls）：
- Read `~/.claude/skills/summarize-session/config.json`
- `Bash: date "+%Y-%m-%d %H:%M"`
- `Bash: SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1); python3 "$SS/scan_sessions.py" --timerange "$PWD"`（从当前会话 JSONL 提取工作时段的推断初值）
- `Bash: SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1); python3 "$SS/scan_sessions.py" --touched-repos "$PWD"`（扫描本次会话实际修改的 git 仓库集合，解决 cwd 与实际修改仓库错位的问题）
- `ToolSearch query="select:AskUserQuestion" max_results=1`（第三步确认环节需要；AskUserQuestion 是 deferred tool，若未预加载会导致直接调用失败。已经在直接工具列表时这步相当于 no-op）

用 config 结果确定 `$VAULT` 和 `$LOG_DIR`。

**确定工作日志的项目/分支**（记为 `$PROJECT_REPO`）：
1. 读取 `--touched-repos` 输出 `{cwd_repo, touched_repos, primary_repo, cross_repo}`
2. `primary_repo` 是本次会话实际修改最多的 git 仓库（file_count 最大；并列时优先 cwd_repo）
3. 若 `cross_repo=true`（primary ≠ cwd），在第三步计划顶部标注 `⚠️ 跨仓库修改：当前 cwd=<cwd_repo.toplevel>，但实际修改在 <primary_repo>，已自动切换`
4. 若 `touched_repos` 有多个非 cwd 仓库，在计划中列出所有 `{toplevel, branch, file_count}`，让用户确认默认选择
5. 若 `touched_repos` 为空（会话无 Edit/Write），回退到 `cwd_repo`
6. 用 `$PROJECT_REPO.toplevel` 的 basename 作为"项目"字段，用 `$PROJECT_REPO.branch` 作为"分支"字段

**获取关键产出的 git log**：
- `Bash: git -C "$PROJECT_REPO_TOPLEVEL" log --oneline -5 2>/dev/null || echo "非 git 项目"`
- 基于 `$PROJECT_REPO` 而非 cwd,避免跨仓库时关键产出字段指向错误的提交

**工作时段初值**：`--timerange` 输出 `{start, end, date, duration_hours, session_id}`，用于第三步工作日志块的默认值填充；失败时（输出含 error）回退到手填。

确定 `$VAULT` 后，**`$QUICK!=true` 时** ⚡ **再并行执行**：
- 读取 pending-docs.json：⚠️ **不要直接用 Read**（文件可能累积到 20k+ tokens 超 Read 限制）。改用脚本筛选（**$VAULT 须 forward-slash 形式；Windows 路径含 `\U/\u/\x/\N` 在内联 python 字符串里会 SyntaxError**）：
  ```bash
  python3 -c "
  import json, sys, pathlib
  p = pathlib.Path('$VAULT/.meta/pending-docs.json')
  if not p.exists():
      print(json.dumps({'count': 0, 'recent': []})); sys.exit()
  data = json.loads(p.read_text(encoding='utf-8'))
  print(json.dumps({
      'count': len(data),
      'types': {t: sum(1 for d in data if d.get('type') == t) for t in set(d.get('type','other') for d in data)},
      'recent': data[-20:],  # 仅取最近 20 条
  }, ensure_ascii=False, indent=2))
  "
  ```
- Glob `~/.claude/projects/<当前项目>/memory/*.md`

根据结果设置标志：
- pending-docs.json 为空/不存在或 count=0，且对话中无 `$VAULT` 外部 `.md` 文件生成 → `$SKIP_DOC_COLLECT=true`
- memory Glob 仅含 `MEMORY.md` 或为空 → `$SKIP_MEMORY=true`

### 第二步：扫描对话上下文

回顾当前对话，识别以下类别的信息：

| 类别 | 示例 | 目标位置 |
|:-----|:-----|:---------|
| 用户偏好/工作习惯 | "我喜欢用 xxx 方式"、"以后别做 xxx" | 全局 `~/.claude/CLAUDE.md` |
| 当前项目规则/约束 | "这个项目必须 xxx"、"不要修改 xxx" | 当前项目 CLAUDE.md（见下方说明） |
| 技术决策 | 选择了某方案及原因 | `$VAULT` 笔记（新建或追加） |
| 项目进展 | 完成了什么、下一步计划 | `$VAULT` 笔记（新建或追加） |
| 开发经验 | 踩坑记录、最佳实践 | `$VAULT` 笔记（新建或追加） |

**项目规则写入位置**：
- 在 git 仓库内 → `$(git rev-parse --show-toplevel)/CLAUDE.md`
- 不在 git 仓库 → 合并写入 `~/.claude/CLAUDE.md`

同时标记：
- **文档归集项**（`$SKIP_DOC_COLLECT!=true` 时）：详见 `references/doc-collection.md`
- **Memory 沉淀项**（`$SKIP_MEMORY!=true` 时）：详见 `references/memory-settlement.md`

### 第三步：生成更新计划

将所有待执行操作整理为结构化计划。**`$FORCE!=true`** 时用 AskUserQuestion 确认；**`$FORCE=true`**（强制模式）时把计划打印到输出后**不弹确认，直接进入第四步**（工作日志默认写入，除非 `--no-log` / `--quick`）。计划格式：

```
=== 会话总结 ===

知识库路径：$VAULT
模式：轻量模式（--quick）          ← 仅 $QUICK=true 时显示此行
模式：仅笔记（--no-log）          ← 仅 $NO_LOG=true 且 $QUICK!=true 时显示此行
工作日志目录：$LOG_DIR             ← 仅 $NO_LOG!=true 时显示以下工作日志块

⏱️ 工作日志条目：
  - 文件：$LOG_DIR/2026年/03月/2026-03-19.md
  - 时段：14:30 ~ 16:00（自动推断，可覆盖）     ← 来自 --timerange 输出;失败时显示"（请填写）"
  - 耗时：1.5h（自动计算，可覆盖）               ← 同上
  - 标题：重构日志文件识别逻辑
  - 项目：<$PROJECT_REPO 的 basename> | 分支：<$PROJECT_REPO.branch>    ← 基于 --touched-repos 的 primary_repo,跨仓库时已自动切换
  - ⚠️ 若 cross_repo=true，在此处展示："当前 cwd=<cwd_repo>，本次实际修改在 <primary_repo>（file_count=N），已自动切换"
  - ⚠️ 若 touched_repos 有多个非 cwd 仓库，列出所有 {toplevel, file_count} 让用户确认

📋 CLAUDE.md 更新（N 项）：
  - [全局] 新增偏好：xxx
  - [项目] 新增约束：xxx → /path/to/project/CLAUDE.md

📝 笔记更新（N 项）：
  - [新建] 领域/xxx.md — 说明
  - [追加] 领域/xxx.md — 说明

📦 文档归集（N 项）：               ← $SKIP_DOC_COLLECT=true 时显示"（0 项——无待归集文档）"
  - [归集][pending-docs] /path/to/doc.md → $VAULT/目标/doc.md
  - [归集][对话扫描]     /path/to/other.md → $VAULT/目标/other.md

  每条必须带来源标签（`[pending-docs]` 或 `[对话扫描]`），便于用户判断是否有遗漏
  ⚠️ 若 sync 将 prune 死条目：展示"sync 将清理 N 条死条目（已 .bak 轮转备份保留最近 5 份）"，让用户确认计划时知情（P5）

🧠 Memory 沉淀（N 项）：            ← $SKIP_MEMORY=true 时显示"（0 项）"
  - [沉淀+保留] user/偏好记录 → $VAULT/偏好与习惯/xxx.md
  - [沉淀] reference/链接 → $VAULT/参考资料/xxx.md
  - [清理] project/过期记录 — 已过期

跳过（无需记录）：
  - 临时调试过程
  - 一次性文件操作
```

`$NO_LOG!=true` 时：
- 工作时段和耗时的初值由 `scan_sessions.py --timerange` 自动推断（会话 JSONL 的首尾 timestamp），展示给用户确认并允许覆盖
- 若 `--timerange` 返回 error（首次会话、JSONL 异常等），时段字段显示"（请填写）"并回退到手填
- `options` 中**必须**包含"跳过工作日志"选项，用户选择后设置 `$NO_LOG=true`（`$FORCE=true` 时不弹选项，默认写入工作日志）

### 第四步：执行更新

⚡ **并行写入**（注意约束）：
- CLAUDE.md 更新、笔记写入、工作日志写入可**并行**执行（已有文件必须走 Read→Edit 模式，详见前文「并发安全」章节）
- **Memory 沉淀必须在笔记写入完成后执行**——沉淀内容可能需要追加到笔记中，并行会导致写冲突
- 索引重建**最后**执行（依赖所有写入结果）

#### 更新 CLAUDE.md

**全局偏好** → `~/.claude/CLAUDE.md`：
- **Read** 获取最新内容，检查是否已有类似规则（避免重复）
- 用 **Edit** 将新规则追加到合适的位置，保持现有格式风格
- Edit 失败时重新 Read 再试（其他窗口可能刚修改过）

**项目规则** → 当前项目 CLAUDE.md：
- 在 git 仓库内：**Read** `$(git rev-parse --show-toplevel)/CLAUDE.md`，用 **Edit** 在合适位置追加规则
- 不在 git 仓库：合并到 `~/.claude/CLAUDE.md` 中

#### 创建/更新 Obsidian 笔记

笔记不得放在 `$VAULT` 根目录，必须归类到子文件夹中。

**操作全部通过封装层 `scripts/obsidian_cli.py`**（详见 `references/obsidian-cli-ops.md`）：

- 查重：`python3 .../obsidian_cli.py --vault "$VAULT" search --query "<关键词>"` → 读 JSON.data.paths
- 新建：`python3 .../obsidian_cli.py --vault "$VAULT" create --path "<相对路径>" --content "<正文>"`
- 追加：`python3 .../obsidian_cli.py --vault "$VAULT" append --path "<相对路径>" --content "<正文>"`
- 读取：`python3 .../obsidian_cli.py --vault "$VAULT" read --path "<相对路径>"` → 读 JSON.data.content
- frontmatter：`property-set` / `property-read` / `properties`

> ⚠️ **参数顺序**：`--vault` 是**全局参数**，必须放在子命令（op）之前。写成 `create --vault ...` 会报 `error: the following arguments are required: --vault`。

每次调用后检查返回 JSON：
- `used: "cli"` → 已通过 Obsidian 生效
- `used: "fallback"` → 走了文件 I/O，`reason` 记录原因，累计到最终输出

笔记格式、文件夹归类策略、frontmatter 规范详见 `references/note-format.md`。

**引用 spec/plan 时的 wikilink 规范**：

- 若该 doc 已归集（pending-docs.json 条目带 `vault_path`）→ 优先用 `[[<filename-without-ext>]]` wikilink
- 若该 doc 未归集 → 用项目绝对路径 + `（未归集到 Vault）` 提示
- 同 basename 冲突（如 spec + plan 同名） → 必须带子目录消歧 `[[specs/<name>|spec]]` / `[[plans/<name>|plan]]`
- pending-docs.json 条目的 `wikilink_form` 字段（由 sync_pending_docs.py 写入）优先级最高，直接读取使用
- **引用已归集 spec/plan 时必须用 `[[wikilink]]` 而非反引号**——避免归集即孤立（P4：spec/plan 本身无出链，引用方若用反引号则它 inbound=0 成孤立节点）

**清理 unresolved 悬空链接（`fix_links.py`）**——检测到 unresolved 时可一键清理（**需用户授权，不在流程自动跑**）：

```bash
# 默认 dry-run 预览；--apply 实际改写（自动 .bak）
SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
python3 "$SS/fix_links.py" --vault "$VAULT"
python3 "$SS/fix_links.py" --vault "$VAULT" --apply
```

只把正文（非 frontmatter、非代码块/行内代码区）unresolved 的 `[[target]]` 改反引号；mask 异常整文件跳过（fail-closed）。

#### 写入工作日志

> **`$NO_LOG=true` 时跳过。**

工作日志按年/月/日三级组织：`$LOG_DIR/YYYY年/MM月/YYYY-MM-DD.md`。路径生成统一通过 `scripts/_worklog_path.py` 的 `worklog_path(log_dir, date_str)` 函数（输入校验 ValueError）。

- 写入前必须 `mkdir -p` 父目录（年/月），调用方负责创建
- `$LOG_DIR` 在 `$VAULT` 内 → 走封装层：`obsidian_cli.py --vault "$VAULT" append --path "工作日志/YYYY年/MM月/YYYY-MM-DD.md" --content "..."`（当天文件不存在时封装层内部通过 create/append 处理）
- `$LOG_DIR` 在 `$VAULT` 外 → 保持原流程：Read → Edit（已存在）/ Write（不存在）；Edit 失败重新 Read 再试
- `$LOG_DIR` 或目标父目录不存在 → 自动创建（`mkdir -p`）

**新建文件格式**：

```markdown
---
tags: [工作日志]
category: 工作日志
created: 2026-03-19
summary: "2026-03-19 工作记录"
---

# 2026-03-19 工作日志

## 14:30 ~ 16:00 | 1.5h | 重构日志文件识别逻辑

**项目**: ProjectB
**分支**: project/CHERY/BASE              ← 非 git 项目时省略此行

### 工作内容

- 主要事项 1
- 主要事项 2

### 关键产出

- 5 个文件变更，净减 72 行代码
- 提交: `046d027` [refactor|batch-analyze-bugs|脚本优化]
```

非 git 项目下，"项目"字段使用当前工作目录名（`basename $PWD`），省略"分支"和 git 相关的关键产出。

**追加条目**：仅追加 `---` 分隔线 + 新条目（无 frontmatter）。

**条目提取规则**：
- 标题：一句话概括核心工作（不超过 20 字）
- 工作内容：主要事项列表
- 关键产出：git 提交、文件变更数等可量化产出（无则省略此章节）

#### 文档归集与 Memory 沉淀执行

- **文档归集**（`$SKIP_DOC_COLLECT!=true` 时）：调脚本：

  ```bash
  SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
  python3 "$SS/sync_pending_docs.py" \
    --vault "$VAULT" --mode incremental --apply \
    --output-json /tmp/summarize_sync_result.json
  ```

  - 解析输出 JSON：`new_archived / synced / adopted / skipped_unchanged / conflict_vault_edited / conflict_both_edited / original_missing / path_invalid / denied_sensitive / errors / expired_missing / pruned / pruned_planned`
  - **conflict_vault_edited / conflict_both_edited 必须显式呈现给用户**，建议在第五步输出中加 `⚠️ 同步冲突：N 项 Vault 副本被手工编辑/双向都变，未覆盖`
  - **expired_missing 必须显式呈现给用户**：若 N>0，在第五步输出中加 `⏰ 原文件失踪超 90 天：N 项，建议人工 review 后决定是否清理 pending-docs 条目`
  - **pruned 呈现**：若 `pruned` 数 N>0，在第五步输出中加 `🧹 本次清理 N 条死条目（无 vault_path + 原文件失踪，已备份到 pending-docs.json.bak，仅保留最近一次 prune 前状态）`
  - 详细行为见 `references/doc-collection.md`
- **Memory 沉淀**（`$SKIP_MEMORY!=true` 时）：按 `references/memory-settlement.md` 执行验证与清理

#### 清理与索引重建

1. 文档归集已由 `sync_pending_docs.py` 完成；脚本通过 atomic rename 更新 pending-docs.json 字段（含 vault_path / source_*hash / vault_content_hash / source_mtime / source_size / archived_at / last_synced_at / wikilink_form / original_missing / path_invalid / denied_sensitive 等）。**LLM 不应再手动清理或修改 pending-docs.json**——有用条目保留主表，真死条目由 sync 自动 prune（删前 `.bak` 备份），状态由字段反映：
   - `vault_path` 已设 → 已归集成功
   - `original_missing=true` 且有 vault_path → 原文件已丢，Vault 副本保留
   - 无 vault_path + `original_missing=true` + path 不存在 → 真死条目，sync `--apply` 自动 prune 删除（可从 `pending-docs.json.bak` 恢复）
   - `path_invalid=true` / `denied_sensitive=true` → 永远跳过
   - **一次性深度清理**（积压死条目过多时）：`reclaim_and_prune.py` 先对死条目 basename 在 Vault 唯一命中者重建 vault_path（转 active），再清理其余真死条目：
     ```bash
     # 先 dry-run 预览 reclaimed / pruned_planned
     SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
     python3 "$SS/reclaim_and_prune.py" --vault "$VAULT"
     # 确认后 --apply（自动 .bak 备份）
     python3 "$SS/reclaim_and_prune.py" --vault "$VAULT" --apply
     ```
   - **清理已归集超龄条目**（pending-docs 只增不减时）：已归集（有 vault_path）的条目原文已安全在 Vault，其跟踪记录在归集超过 N 天（默认 30）后可手动清理；`prune_archived.py` 独立手动跑，**不接入 sync、无 config、无自动触发**：
     ```bash
     # 先 dry-run 预览 pruned_planned（--older-than N 调阈值，默认 30 天）
     SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
     python3 "$SS/prune_archived.py" --vault "$VAULT"
     # 确认后 --apply（自动 .bak 轮转备份）
     python3 "$SS/prune_archived.py" --vault "$VAULT" --apply
     ```
   - `conflict_vault_edited / conflict_both_edited` 状态由本次 sync 输出 JSON 反映（不写回 pending-docs，用户决策后改原文件再跑即可）
   - 解析 sync 输出 JSON 的 `conflict_vault_edited` 和 `conflict_both_edited` 数组，在第五步输出中提示用户
2. **触发 Obsidian 重扫**（若 CLI 可用）：
   ```bash
   SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
   python3 "$SS/obsidian_cli.py" --vault "$VAULT" reload
   ```
   `used: fallback` 时跳过（Obsidian 未运行，无需重扫）。
3. 重建索引：
   ```bash
   SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
   python3 "$SS/rebuild_index.py" --vault "$VAULT" --emit=all
   ```
   脚本输出 JSON 报告（笔记总数、本次更新数、`health_check` 字段等）。缓存规范详见 `references/cache-spec.md`。
4. **解析 `health_check` 字段**——脚本输出 JSON 含：
   - `category_with_slash`：category 含斜杠的笔记数（应为 0）
   - `project_field`：用了过时 project 字段的笔记数（应为 0）
   - `folder_subcat_missing`：子目录下 subcategory 缺失的笔记数（应为 0）
   - `no_frontmatter`：完全无 frontmatter 的笔记数（plans/specs 类临时文档可忽略）
   - `stale_indexes`：磁盘上存在但本次未写入的孤立索引文件列表（旧名 INDEX.md 或未更新的 {category} 索引.md）
   - `specplan_no_backlink`：已归集 spec/plan 无任何 `[[wikilink]]` 指向的数量（窄化检测，P4）
   - `unresolved_links`：正文 `[[target]]` 指向 Vault 内不存在目标的悬空链接数

   若 `category_with_slash + project_field + folder_subcat_missing > 0`：在第五步输出中明确告知用户，建议追加跑一次：
   ```bash
   SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
   python3 "$SS/rebuild_index.py" --vault "$VAULT" --emit=all --fix-frontmatter
   ```
   若 `stale_indexes` 非空：建议归档：
   ```bash
   SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
   python3 "$SS/rebuild_index.py" --vault "$VAULT" --emit=all --archive-stale-indexes
   ```
   归档目标 `.meta/archived-indexes/<date>/`，可回收。

   **不要**在 skill 流程内自动跑 `--fix-frontmatter`——动笔记原文件需用户明确授权。
5. 如果新建了笔记，检查是否需在 CLAUDE.md 或其他笔记中添加 `[[wikilink]]` 引用（无需文件夹路径）
6. 标记当前会话为已总结（避免 `--catch-up` 重复处理）：
   ```bash
   SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
   python3 "$SS/scan_sessions.py" --mark-current "$PWD"
   ```
7. **提交知识库改动（git commit）**——默认开启，`--no-commit` 跳过：
   ```bash
   SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
   python3 "$SS/git_commit_vault.py" --vault "$VAULT" --title "<本次会话标题>"
   ```
   - 用 `git status` 枚举知识库目录内变更/未跟踪 `.md`（笔记/工作日志/CLAUDE.md/系统索引文件）精确 add，**不全量 `-A`**；只 commit 不 push。
   - 解析输出 JSON `status`：`committed`（成功）/ `skipped`（非 git 或 --no-commit）/ `nothing`（无知识库改动）/ `failed`（占用/冲突，**不阻塞**后续）。
   - 若 `baseline_suggested=true`（历史 untracked > 20）：先跑 `--baseline-preview` 列清单，用 AskUserQuestion 让用户确认后再 `--baseline` 全量基线提交。**`$FORCE=true` 时不弹确认、也不自动 baseline**，仅在第五步输出中提示"建议另行运行 `--baseline-preview` 审阅后基线提交"（避免 `git add -A` 全量提交未脱敏文件）。
   - **commit 前确认笔记内容已按文档规范脱敏**（不含密码/token/凭据路径）。

### 第五步：输出确认

执行完毕后，简要汇报（使用实际路径）：

```
=== 已更新 ===
✅ $LOG_DIR/2026-03-19.md — 追加 1 条工作记录（14:30~16:00, 1.5h）  ← $NO_LOG=true 时无此行
✅ ~/.claude/CLAUDE.md — 新增 2 项偏好
✅ /path/to/project/CLAUDE.md — 新增 1 项规则
✅ $VAULT/CLAUDE.md — 索引已重建（N 篇笔记）
✅ [新建] $VAULT/领域/xxx.md
✅ [追加] $VAULT/领域/xxx.md — 新增"进展"章节
📦 [归集] 2 个文档已归集到 Vault
🧠 [沉淀] 3 条 Memory 已沉淀（1 条保留源文件），2 条已清理
🏷️ 当前会话已标记为已总结
✅ git commit：已提交 N 个知识库文件（<message>）        ← status=committed
⏭️ git commit：跳过（--no-commit / 非 git 仓库）          ← status=skipped
⚠️ git commit 失败：<reason>，本次未提交，可稍后手动 commit  ← status=failed（必须醒目单独成行）
🔌 Obsidian CLI：<N> 次成功 / <M> 次降级
   · <reason> × <count>（聚合 degraded_counts 输出）

⚠️ 健康检查（仅在 health_check 命中时显示）：
   - frontmatter 不规范 X 篇 → 建议跑 `--fix-frontmatter`
   - 孤立索引文件 Y 个（含旧 INDEX.md 或未更新 {category} 索引.md）→ 建议跑 `--archive-stale-indexes`
   - spec/plan 无 backlink X 篇 → 建议在相关笔记用 [[wikilink]]（而非反引号）引用
   - unresolved 悬空链接 Y 处 → 可跑 fix_links.py 清理（需授权，见下）
```

全降级时用这一行替代：

```
🔌 Obsidian CLI：全程降级（原因：<首笔 reason>）
```

**CLI 使用统计聚合**：每次 `obsidian_cli.py` 调用返回 JSON 中含 `degraded_counts`；skill 末尾将各次调用累加并展示。零降级时仍展示"N 次成功 / 0 次降级"以明确 CLI 通路健康。

## 写作规范

- 语言：中文（技术术语保留英文原文）
- 风格：简洁、结构化、面向未来的读者
- frontmatter 中的日期使用绝对日期（不用"今天"、"昨天"）
- 将用户的口语化表达转换为规范化的书面描述
- 偏好/规则类内容提炼为一句话，附上原因（如有）
- 决策记录包含：背景→方案→理由→影响

## 边界

- 不修改 `.obsidian/` 目录
- **不删除 Vault 中的已有笔记内容**（只追加或新建）
- Memory user/feedback 类型沉淀后保留源文件不删除；project/reference 类型验证通过后才删除
- 不记录包含敏感信息的内容（密码、token、凭据路径等）
- 对话中没有值得记录的内容时，直接告知用户"本次对话无需额外记录"
- 用户未确认前不执行任何写入操作（例外：`-f` / `--force` 强制模式由用户在调用时显式授权跳过确认）
- 已存在于根目录的旧笔记，追加时保持原位，不主动迁移（除非用户要求整理）
- `[[wikilink]]` 引用无需包含文件夹路径，Obsidian 会自动解析

$ARGUMENTS
