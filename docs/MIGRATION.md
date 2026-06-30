# claude-vault 迁移指南（仅原作者）

> ⚠️ 本文档**仅供原作者**——此前在 `~/.claude` 里直接维护这套 vault-loader / summarize-session / vault skill 与 hook 的人。
> **全新安装的用户请忽略本文档**：你直接 `/plugin install claude-vault` 即可零配置使用，无需任何迁移。

## 背景：为什么需要迁移

原作者此前通过 `~/.claude/settings.json` 的**绝对路径**注册了 4 个 hook：

| 事件 | 脚本 |
|---|---|
| SessionStart | `skills/vault-loader/scripts/session_start_load.py` |
| SessionStart | `hooks/session_start_auto_notify.py` |
| UserPromptSubmit | `skills/vault-loader/scripts/prompt_submit_load.py` |
| SessionEnd | `hooks/session_end_enqueue.py` |

安装 claude-vault 插件后，插件自带的 `hooks/hooks.json` 会注册**同名事件**的 hook。Claude Code 对 `settings.json` hooks 与插件 hooks **取并集执行、不去重**。

**若启用插件却不删除 `settings.json` 旧注册，会双触发**：
- SessionStart 知识库上下文注入两遍（双倍 token）
- UserPromptSubmit 同理重复注入
- 旧的 auto-mode 注册（`session_start_auto_notify.py` / `session_end_enqueue.py`）现已无对应脚本，会静默 no-op，但仍应一并清理

## 迁移步骤（必须原子完成）

> **启用插件与删除旧注册必须在同一步完成**，否则中间态会双触发。

1. 安装插件：`/plugin marketplace add <你的-repo>` 然后 `/plugin install claude-vault`
2. **同时**从 `~/.claude/settings.json` 删除上述 4 条 hook 注册（SessionStart 2 条 + UserPromptSubmit 1 条 + SessionEnd 1 条）
3. 新开一个会话，验证**单次触发**：
   - SessionStart：知识库上下文只注入一次（不重复出现）
   - UserPromptSubmit：prompt 相关注入只出现一次

## 配置与数据的延续

- **config**：插件复用用户态固定路径 `~/.claude/skills/vault-loader/config.json` 与 `~/.claude/skills/summarize-session/config.json`——这正是你现有的 config，无需搬动。
- **现有 vault**：插件默认 `vault_path` 为 `~/.claude/knowledge-vault`。若你要继续使用现有的 vault 目录，在 `~/.claude/skills/vault-loader/config.json` 把 `vault_path` 设为你的现有 vault 路径，并在 summarize-session config 把 `default_vault_path` 设为同一路径（两者需一致，启动时会自检告警）。
- **frontmatter-cache**：现有的 `<vault>/.meta/frontmatter-cache.json` 若版本为 `_version: 1` 可直接复用；否则下次 `/summarize-session` 会重建。

## 单源工作流（--plugin-dir）

> **适用场景**：你希望直接从本地插件仓库加载插件，改动即生效，无需手动同步到 `~/.claude/skills/`。

### 未验证点说明

以下标注 ⚠️ 的行为**本会话未端到端验证**，仅依据 Claude Code 文档与设计推断：

- ⚠️ `--plugin-dir` 持久化后重启，hook 是否确实从插件目录触发（而非旧 settings.json 注册）
- ⚠️ 插件 skill 与 `~/.claude/skills/` 源 skill 同名时的加载优先级与去重行为
- ⚠️ SKILL.md 文本改动是否真正"自动检测"无需重启（取决于 Claude Code 版本）

---

### 1. 机制

```
claude --plugin-dir "<插件目录绝对路径>"
```

- Claude Code 从指定目录直接加载插件，无需安装到 `~/.claude/skills/`
- **SKILL.md 文本改动**：⚠️【未验证】可能自动检测，也可能需要重启
- **hooks / agents / MCP 改动**：需重启会话（或运行 `/reload-plugins` 若当前版本支持）

---

### 2. 持久化启动（PowerShell）

在 `$PROFILE`（`~\Documents\PowerShell\Microsoft.PowerShell_profile.ps1`）中添加 wrapper 函数，使每次启动 Claude Code 都自动携带 `--plugin-dir`：

```powershell
function claude { & claude.exe --plugin-dir "<插件目录绝对路径>" @args }
```

> 需你将 `<插件目录绝对路径>` 替换为实际路径后新开 shell 生效。

---

### 3. 迁移旧 hook 注册

若你此前已通过 `~/.claude/settings.json` 注册了本插件的 4 个 hook，启用 `--plugin-dir` 后必须删除旧注册，否则同一会话内 hook 双触发（双倍 token、重复入队）。

**步骤：**

1. **Dry-run 预览**（不修改文件）：

   ```bash
   python3 scripts/migrate_settings.py
   ```

   输出会列出将被删除的条目（4 条：`session_start_load.py`、`session_start_auto_notify.py`、`prompt_submit_load.py`、`session_end_enqueue.py`）。

2. **确认无误后 Apply**（备份原文件后写入）：

   ```bash
   python3 scripts/migrate_settings.py --apply
   ```

   脚本会将原 `settings.json` 备份为 `settings.json.bak-<YYYYMMDD-HHMMSS>`，再写入删除目标条目后的版本。

3. 指定非默认路径（可选）：

   ```bash
   python3 scripts/migrate_settings.py --settings /path/to/settings.json
   ```

> 若文件不存在或无匹配条目，脚本以 exit 0 退出并打印 `nothing to migrate`，可安全重复运行。

---

### 4. 源 skill 重名处理（⚠️ 未验证点）

`~/.claude/skills/` 下可能存在与插件同名的 skill 目录（`vault-loader`、`summarize-session`、`vault`），其加载优先级与插件版本的关系**本会话未验证**。

建议处理流程：

1. 以 `--plugin-dir` 启动新会话后，检查 skill 列表（输入 `/` 查看可用 skill）
2. 若出现重复 skill（同名两份），考虑将 `~/.claude/skills/{vault-loader,summarize-session,vault}` 重命名为 `.bak` 后缀
3. **不要在验证前删除**源 skill 目录——若插件加载失败，源 skill 仍可作为回退

---

### 5. 重启验证

新开会话后验证单次触发：

- **SessionStart**：知识库上下文注入只出现一次（搜索输出中无重复）
- **Skill 列表**：无同名重复条目

---

### 6. 回退

如需回退到原 settings.json 注册方式：

1. 恢复备份：`cp settings.json.bak-<时间戳> settings.json`
2. 删除 `$PROFILE` 中的 `function claude {...}` wrapper（或重命名使其不生效）
3. 若已将源 skill 重命名为 `.bak`，将其改回原名
4. 新开会话验证 hook 单次触发

---

## 运行测试（开发者）

各 skill 独立跑（推荐，避免多 skill 同名 conftest 冲突）：

```bash
cd skills/vault-loader && python3 -m pytest -q
cd skills/summarize-session && python3 -m pytest -q
python3 -m pytest tests packaging -q   # 仓库根：插件级 hook/wrapper/打包测试
```

打包脱敏闸门（发布前必跑，须 `secret scan clean` / exit 0）：

```bash
python3 packaging/build_plugin.py
```
