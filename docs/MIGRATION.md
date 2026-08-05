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

## ⚠️ 从旧版升级到「召回质量修复版」的须知（面向所有升级用户）

### 症状：盘上旧值压制新默认，修复只生效一半

本版本把 `scoring.prompt_keyword_hit` 默认值从 3 提到 5（让精确 keywords 命中能胜过泛 tag），并新增 tag-IDF 加权（`relevance.use_tag_idf`）。

**已运行过旧版的存量用户须注意**：旧版首跑会把当时的全量默认值**物化**写进 `~/.claude/skills/vault-loader/config.json`，其中就包含 `prompt_keyword_hit: 3`。升级后 deep-merge 把盘上这个 3 当成「用户显式覆盖」，优先于新默认 5；而同批新增的 `use_tag_idf` 因为是**新键**、盘上没有，正常取到默认 `true`。净结果是「tag-IDF 已收窄候选集、但 keywords 权重仍停在 3」的**半套组合**，召回可能**不如修复前**。

`/plugin update` 不会修复这一点——它不触碰用户态 `config.json`。

> 同一机制影响**所有**被物化过的调参键，不止 `prompt_keyword_hit`：任何键只要盘上值等于旧默认，该键未来的默认值演进对你都静默失效。全新安装用户不受影响——本版本起首跑只写最小占位（`_config_version` + 一行说明），不再物化全量默认。

### 推荐做法：用收敛脚本 `migrate_config.py`

插件自带的 `skills/vault-loader/scripts/migrate_config.py` 正是为此场景而生。它只删除「盘上值恰好等于该键**历史上任一版本**默认值」的数值调参键，删除后 deep-merge 自动回落到当前版本的最新默认；`vault_path`、`keyword_to_tags`、`opt_out_paths`、`display`、`enabled` / `dry_run` 等一律不动，你显式调过的非默认值也不动。

**第 1 步 · dry-run 预览**（默认行为，只读扫描，不改动任何文件）：

```bash
VL=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/vault-loader/scripts 2>/dev/null | sort -V | tail -1)
python3 "$VL/migrate_config.py"
```

> 若报 `No such file or directory`，说明你装的插件版本早于本脚本引入的版本——先 `/plugin update` 再重跑（`ls -d` 的 glob 取的是 cache 里版本号最大的那份）。

输出逐条列出将被删除的键与当前值，形如：

```
[migrate_config] dry-run：<...>/config.json 发现 9 个物化残留键（加 --apply 执行清理，不加不会改动任何文件）：
  将删除 scoring.prompt_keyword_hit=3
  将删除 relevance.min_topical_score=4
  ...
```

**逐条核对这份清单**（为什么必须核对见下方「使用限制」第 1 条），确认没有你刻意设成该值的键。

**第 2 步 · 确认无误后 apply**（先备份，再原子写回清理后的 config）：

```bash
VL=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/vault-loader/scripts 2>/dev/null | sort -V | tail -1)
python3 "$VL/migrate_config.py" --apply
```

脚本会先把改动前的 config 完整备份出去并打印备份路径，然后打印实际删除的键，末行给出撤销命令。

**第 3 步 · 如需撤销**（把第 2 步打印的备份路径填进去）：

```bash
VL=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/vault-loader/scripts 2>/dev/null | sort -V | tail -1)
python3 "$VL/migrate_config.py" --restore ~/.claude/projects/vault-loader-backups/config-<时间戳>.json
```

`--restore` 覆盖前同样会先把**当前**内容备份出去，所以 apply 之后手工做的调参也有恢复路径。`--restore` 只接受顶层键属于 vault-loader schema 的备份文件（防止把任意 JSON 还原成 config）。`--apply` 与 `--restore` 互斥，同时传会直接报 usage 错误。

指定非默认 config 路径用 `--path <路径>`。若 config 路径经符号链接 / NTFS junction 重定向，脚本为防越权写入会整体放弃；确属你自己的 dotfiles 软链布局时可加 `--force`（`-y`）放行。

### 使用限制（三条，用前必读）

1. **判据是「值等于历史默认」，不是「用户没改过」。** 脚本无法区分「旧版物化残留的 3」和「你手动设成 3」——两者盘上完全一样。若你曾刻意把某个键设成恰好等于某个历史默认值，它会被当成残留删掉。**这就是第 1 步 dry-run 必须逐条核对的原因**；真被误删也可用 `--restore` 整份回滚。
2. **只处理数值键（int/float）。** 布尔开关（`use_tag_idf`、`use_keywords`、`split_cjk_bigram` 等）、列表（`exclude_note_tags`）、字符串一律不参与判定——开关值的语义是「你要不要这个行为」，等值判定法对它不适用。好处是你显式关过的止血开关绝不会被误清；代价是旧版物化的布尔残留也不会被清理，如需回到默认请手工删除该键。
3. **备份目录不受 `--path` 影响。** 备份恒落真实 HOME 下的 `~/.claude/projects/vault-loader-backups/`，即使你用 `--path` 指定了别处的 config。该目录与 `config.json` 物理隔离，也不落在插件仓库或任何项目工作树内。

   > ⚠️ **不保证「在任何 git 仓之外」**（此前本文档如此断言，现撤回）：把 `~/.claude` 整体纳入版本管理是常见做法，那样备份目录就落在该仓库内，是否被跟踪完全取决于它自己的 `.gitignore`。备份内容是 config 全文（含 `vault_path` 等本机绝对路径），推送到公开仓库前请自行确认。

### ❌ 不要删除 `config.json`

本文档此前建议「删除 `config.json` 让其按新默认重建」，**该建议已撤回**。删除会连同 `vault_path` 一起丢失：重建后 `vault_path` 回落到默认的 `~/.claude/knowledge-vault`，`keyword_to_tags` / `opt_out_paths` / `display` 等自定义配置一并清零，而且**全程不报错**——你的知识库从此静默不再被注入，唯一征兆是"怎么没有注入了"。

手动把 `scoring.prompt_keyword_hit` 改成 `5` 仍然可行，但只治这一个键；上面的收敛脚本是完整解法。

上一条「config 无需搬动」是就**路径延续**而言，不含本次的默认值变更。

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
