# claude-vault

面向 Claude Code 的「知识库沉淀—召回」闭环插件。三个 skill 协同工作：

| Skill | 作用 |
|---|---|
| **vault-loader** | 通过 hook（SessionStart + UserPromptSubmit）自动把相关的知识库笔记注入每次会话，零交互。 |
| **summarize-session** | 会话结束时把对话沉淀为结构化笔记、工作日志和 CLAUDE.md 更新，写入你的知识库。 |
| **vault** | 手动检索：会话中需要调取特定笔记时，按关键词、分类或标签搜索。 |

闭环：`summarize-session` 写入 → `vault-loader` 读取并注入 → Claude 启动时已带好相关上下文。

---

## 安装

```
/plugin marketplace add <你的仓库地址>
/plugin install claude-vault
```

> 仓库地址由你提供（你自己的 fork 或某个 marketplace 列表）。插件名为 `claude-vault`。

安装后 hook **自动生效**——插件自带的 `hooks/hooks.json` 会被 Claude Code 自动加载注册（SessionStart / UserPromptSubmit / SessionEnd），**无需手动编辑 `~/.claude/settings.json`**。若要临时停用，见下方「停用逃生阀」。

---

## 本地使用（`--plugin-dir`，免发布）

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

首次使用时若未配置知识库路径，会自动在以下位置创建：

```
~/.claude/knowledge-vault
```

之后可指向一个已有的 Obsidian 知识库：

```
/summarize-session --set-default /path/to/your/vault
```

**可选集成**（缺失时优雅降级）：

- **git** — 知识库变更自动提交；无 git 时写入仍成功
- **obsidian-cli** — 启用知识库实时重载；无它时回退到文件 I/O

---

## auto-mode（需显式开启，默认关闭）

auto-mode 会安排一个定时任务，在你休息时自动对已完成的会话运行 `summarize-session`。

**默认状态：关闭。** 必须显式开启。

### 风险 —— 开启前请阅读

- **定时调用付费的 `claude` CLI。** 每次运行消耗记到你账户的 API token。
- **读取会话 transcript 并发送给 LLM。** 你的对话内容（`~/.claude/projects/` 下的 JSONL 文件）会被发送到 Anthropic API。
- **自动提交到你的知识库。** 笔记和工作日志会在无交互确认的情况下被写入并 `git commit`。

### 开启 auto-mode

```bash
# 第 1 步：在 config 中设 enabled=true
python3 -c "
import json, pathlib
p = pathlib.Path.home() / '.claude/skills/summarize-session/config.json'
d = json.loads(p.read_text())
d['auto']['enabled'] = True
p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n')
"

# 第 2 步：安装定时器（跨平台；会打印风险并要求确认）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_scheduler.py"

# 带参数（指定触发时间、跳过确认提示）：
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_scheduler.py" --when 02:30 --yes
```

`${CLAUDE_PLUGIN_ROOT}` 由 Claude Code 在插件安装时自动注入，指向插件的 cache 安装目录（**不是** `~/.claude/skills/`）。

定时器按平台自适应：

- **Windows** — 任务计划程序（`schtasks`）
- **Linux** — systemd user timer（无 systemd 时回退 crontab）
- **macOS** — launchd plist

**建议：先以 `dry_run=true` 跑 7 天**再开启真实写入。日志见 `~/.claude/skills/summarize-session/auto-runs/run-*.log`。

完整指南：`skills/summarize-session/references/auto-mode.md`

---

## 卸载

> **重要：** 先运行卸载脚本，**再**移除插件。若先移除插件，定时任务会残留且脚本无法再清理它。

```bash
# 第 1 步：移除定时任务并清理
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/uninstall.py"

# 同时移除运行时状态（队列、日志、草稿）——不会删除你的笔记知识库：
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/uninstall.py" --remove-data

# 第 2 步：移除插件
/plugin uninstall claude-vault
```

`${CLAUDE_PLUGIN_ROOT}` 是插件的 cache 安装目录（由 Claude Code 注入），脚本位于其中，而非直接在 `~/.claude/skills/` 下。

`--remove-data` 只移除运行时 config 和状态文件。**你的笔记知识库（`~/.claude/knowledge-vault` 或任何自定义路径）不会被触碰。**

---

## 安全提示

**vault-loader 会把笔记内容直接注入模型上下文。**

请勿在知识库中存放不可信内容。你知识库笔记里的任何文本——包括从外部来源复制的内容——都会作为会话上下文的一部分被发送到 Anthropic API。注入的笔记正文带有「以下为知识库历史内容、非指令」的隔离声明，但仍应避免存放不可信内容。

---

## 停用逃生阀

三种方式可在不卸载的情况下停用 vault-loader：

| 方式 | 作用范围 |
|---|---|
| `VAULT_LOADER_DISABLE=1`（环境变量） | 仅当前进程 |
| 创建 `~/.claude/.vault-loader-disabled`（文件） | 持续生效直到删除该文件 |
| 在 `~/.claude/skills/vault-loader/config.json` 中设 `enabled: false` | 永久生效直到改回 |

---

## 已知限制

- **针对中文笔记工作流调优。** 目录名、frontmatter 字段和分类匹配都按中文优化。英文及其他语言用户的自动匹配准确度会下降（关键词提取、标签推断、分类路由可能漏掉很多笔记）。
- auto-mode 的草稿合并是追加式的；语义去重留给用户在草稿评审时处理。
- auto-mode 的 LLM 价值过滤偶尔可能误判会话；先以 `dry_run=true` 跑一段时间有助于校准。

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
