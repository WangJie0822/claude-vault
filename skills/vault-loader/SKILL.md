---
name: vault-loader
description: 自动从知识库按相关性注入 summary 清单到会话上下文（零配置，安装即生效）。SessionStart 注入项目相关笔记 + 近期工作日志；UserPromptSubmit 按用户问题动态深入。禁用逃生阀：VAULT_LOADER_DISABLE=1（单次进程）/ ~/.claude/.vault-loader-disabled 文件（持续）/ config enabled:false（永久）。
argument-hint: "[--diagnose] [--show-config] [--reset-state]"
allowed-tools: Read, Bash(python3 *)
---

# vault-loader：自动加载知识库

vault-loader 通过两个 hook 把 Obsidian Vault 的相关笔记自动注入到 Claude Code 会话上下文，让 Claude 启动时即可看到项目历史决策、近期工作进展。

## 触发机制

| Hook | 时机 | 输出 |
|---|---|---|
| SessionStart | 每次会话启动 | 输出 JSON，含两个字段：`additionalContext`（注入正文，逐字喂模型，含完整 wikilink 清单）+ `systemMessage`（用户可见摘要，已清洗终端转义控制字符）；确定性项目固定上下文：项目相关笔记（项目目录∪标签匹配，mtime 倒序）+ 近 7 天工作日志 + 近期 git 提交（无打分排序）|
| UserPromptSubmit | 每次提交 prompt | 输出同结构 JSON（`additionalContext` + `systemMessage`）；prompt 强相关笔记 Top 3 清单；Top 1 score ≥ 10 时升级为全文 |

失败/无候选时**静默退出**（除非 `verbose_on_skip: true`）。

## 安装（零配置）

作为 `claude-vault` 插件安装即生效：插件自带的 `hooks/hooks.json` 由 Claude Code 自动加载并注册 SessionStart / UserPromptSubmit hook，**无需手动编辑 `~/.claude/settings.json`**。hook 经插件的 polyglot wrapper 运行，脚本路径相对 `${CLAUDE_PLUGIN_ROOT}`（插件 cache 安装目录）解析。

vault-loader 从 `~/.claude/skills/summarize-session/config.json` 读取 `default_vault_path`（由 `/summarize-session --set-default <路径>` 写入）；未配置时默认 `~/.claude/knowledge-vault`，用户可配置为任意 Obsidian Vault 路径。

1. 确认 `<vault>/.meta/frontmatter-cache.json` 存在（由 `/summarize-session` 首次运行后自动生成）。
2. 首次启动新会话即生效。

> 旧的「手动在 `~/.claude/settings.json` 注册 hook」装法已废弃；若你之前手动注册过同名 hook，需删除旧注册以免与插件双触发——见 `docs/MIGRATION.md`。

## 配置

`~/.claude/skills/vault-loader/config.json`（缺失自动生成默认）：

- `enabled`：总开关
- `dry_run`：true 时**不真实注入**（`hookSpecificOutput` 字段缺失，不喂模型），仅输出 `systemMessage` 标 `[DRY-RUN]`，用于灰度验证会注入什么
- `vault_path`：Vault 路径
- `display.user_visible`：true（默认）时生成用户可见的 `systemMessage` 摘要清单；false 时关闭用户侧提示（仅静默注入 `additionalContext`）
- `display.verbosity`：`"normal"`（默认）/ `"compact"` / `"full"`，控制 `systemMessage` 详细程度
- `display.show_size`：true 时在 `systemMessage` 显示注入体积估算
- `session_start.{max_notes, max_recent_worklogs, recent_worklog_days, max_commits, include_tag_matched_notes}`（`min_score` 已废弃：startup 不再打分，保留仅为旧配置兼容）
- `user_prompt_submit.{max_notes, min_score, fulltext_threshold, fulltext_max_bytes, min_keyword_count, state_ttl_hours}`
- `scoring.*`：评分权重表
- `keyword_to_tags`：cwd 关键词 → tag 映射
- `opt_out_paths`：路径前缀黑名单
- `verbose_on_skip`：跳过时输出短提示

详见 spec §9。

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

- **没看到注入**：检查 `<vault>/.meta/frontmatter-cache.json` 是否存在（`<vault>` 为配置的知识库路径）；运行一次 `/summarize-session` 重建索引
- **注入了不相关笔记**：调整 `keyword_to_tags` 映射；或在项目 CLAUDE.md 加 `<!-- vault-loader: tags=[...] -->` 精准声明
- **想看跳过原因**：临时改 `config.json.verbose_on_skip: true`
- **想看会注入什么不实际注入**：改 `config.json.dry_run: true`；`systemMessage` 会标 `[DRY-RUN]` 且不含 `additionalContext`（不喂模型）
- **不想看用户侧提示（仍正常注入模型）**：改 `config.display.user_visible: false`；hook 将只输出 `additionalContext` 无 `systemMessage`

## 与其他系统的关系

- **/summarize-session**：写入 Vault 笔记并维护 frontmatter-cache.json（数据源）；vault-loader 只读
- **/vault skill**：手动深加载通道；vault-loader 注入清单后引导用户/Claude 调 `/vault` 加载全文

## 验收

```bash
cd ~/.claude/skills/vault-loader && python3 -m pytest -v
```

应所有用例通过，性能满足 SessionStart < 500 ms / UserPromptSubmit < 300 ms。
