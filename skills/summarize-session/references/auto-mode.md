# 自动总结模式使用指南

`/summarize-session --auto` 是配套定时调度的 headless 入口，本文档说明如何启用、调优和回滚。

## 总览

```
SessionEnd hook → enqueue_auto_summary.py(硬规则筛选) → auto-queue.jsonl
                                                            ↓
                                       调度器 02:30 触发 → scripts/run_auto_summary.py
                                                                ↓
                          串行 spawn `claude -p --auto --session <uuid>`
                                                                ↓
       LLM 二次价值过滤 → 笔记/工作日志直写 Vault + CLAUDE.md/Memory 走 auto-drafts/
                                                                ↓
                                          SessionStart hook 提示未审草稿
```

## 风险说明（opt-in，请先阅读）

自动模式涉及以下风险，安装调度器前请知情同意：

- **消耗 API 费用**：每次 cron 触发会调用 Claude API（`claude -p`），按实际 token 计费
- **会话记录发送给 LLM**：会话 JSONL（含对话内容）会作为 prompt 输入，发送到 Anthropic API
- **自动 git commit**：笔记/工作日志变更会自动提交到知识库 git 仓库

如不接受上述风险，请仅使用手动 `/summarize-session`（无上述副作用）。

## 安装（跨平台）

```bash
# 1. 启用配置（出厂默认 enabled=false, dry_run=true）
python3 -c "
import json,pathlib
p=pathlib.Path.home()/'.claude/skills/summarize-session/config.json'
d=json.loads(p.read_text())
d['auto']['enabled']=True
p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
"

# 2. 安装定时任务（跨平台：Windows/Linux/macOS；会打印风险说明并要求确认）
# ${CLAUDE_PLUGIN_ROOT} 由 Claude Code 在插件安装时自动注入（指向插件 cache 目录）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_scheduler.py"

# 带参数示例（指定触发时间，跳过确认提示）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/install_scheduler.py" --when 02:30 --yes
```

`install_scheduler.py` 根据当前平台自动选择调度机制：
- **Windows**：Windows 任务计划程序（Task Scheduler / schtasks）
- **Linux**：systemd user timer（回退 crontab）
- **macOS**：launchd plist

安装前会打印风险说明（API 费用 / 会话内容上传 / 自动 git commit），**需要确认后才注册**（`--yes` 跳过）。

## 上线建议：先 dry_run 7 天

首次启用建议保持 `dry_run=true` 跑 7 天：

- 每天看 `~/.claude/skills/summarize-session/auto-runs/run-*.log`
- 观察硬规则误杀率（被 `skipped: hard_rule ...` 的会话是否合理）
- 观察 LLM 二次过滤准确度（`LLM_VALUE_VERDICT=skip` 的会话是否真的没价值）
- 不满意则调阈值（见下文「调优」）

7 天后切真写入：

```bash
python3 -c "
import json,pathlib
p=pathlib.Path.home()/'.claude/skills/summarize-session/config.json'
d=json.loads(p.read_text())
d['auto']['dry_run']=False
p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
"
```

## 暂停/恢复

**临时暂停**（出差、换机时）：

```bash
touch ~/.claude/skills/summarize-session/.auto-paused
```

**恢复**：

```bash
rm ~/.claude/skills/summarize-session/.auto-paused
```

**永久关闭**：

```bash
python3 -c "
import json,pathlib
p=pathlib.Path.home()/'.claude/skills/summarize-session/config.json'
d=json.loads(p.read_text())
d['auto']['enabled']=False
p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
"
```

## 调优

`~/.claude/skills/summarize-session/config.json` 的 `auto` 段：

| 字段 | 默认值 | 含义 |
|---|---|---|
| `enabled` | false | 总开关 |
| `dry_run` | true | 干跑模式（不调 claude，只打印命令） |
| `model` | claude-sonnet-4-6 | headless 调用的模型 |
| `max_per_run` | 8 | 单次 cron 处理上限 |
| `session_timeout_sec` | 480 | 单会话超时（秒） |
| `run_timeout_sec` | 3600 | 整次 cron 超时（秒） |
| `log_retention_days` | 30 | 日志保留天数 |
| `max_failure_count` | 3 | 失败 N 次后永久跳过 |
| `hard_rules.min_messages` | 5 | 硬规则：最小消息数 |
| `hard_rules.min_size_kb` | 20 | 硬规则：最小会话文件大小 |
| `hard_rules.min_duration_min` | 3 | 硬规则：最小会话时长（分钟） |
| `hard_rules.require_edit_or_write` | true | 硬规则：必须有 Edit/Write 工具调用 |

跨平台 timeout 实现：由于 macOS 没有 GNU `timeout` 命令，所有超时控制走 `~/.claude/skills/summarize-session/scripts/_timeout.py`（返码 124 与 GNU 一致）。

## 草稿审阅工作流

cron 跑完后，自动模式产生的 CLAUDE.md/Memory 改动**不直接生效**，而是写入 `~/.claude/skills/summarize-session/auto-drafts/<日期>/`。

**每天早上**（SessionStart hook 会提示）：

```
/summarize-session --review-drafts    # 看 diff
# 直接编辑或删除 ~/.claude/skills/summarize-session/auto-drafts/<日期>/*.draft.md
/summarize-session --apply-drafts     # 一键合入
```

笔记和工作日志在自动模式下**直写 Vault**，不进草稿区。

## 故障排查

**SessionEnd 未入队**：

```bash
tail -50 ~/.claude/skills/summarize-session/auto-runs/enqueue.log
```

查看是否有 `skipped: ...` 提示；若是 `hard_rule` 命中，可调阈值。

**调度器没触发**：

检查对应平台的调度器状态：
- **macOS**：`launchctl print "gui/$UID/com.claude-vault.auto"`（查 next run 字段）；若休眠跨过触发时间，launchd 会在唤醒后补跑
- **Linux**：`systemctl --user status claude-vault-auto.timer`
- **Windows**：任务计划程序 → 查 `claude-vault-auto` 任务状态

**会话总结失败**：

```bash
ls -t ~/.claude/skills/summarize-session/auto-runs/session-*.log | head -3 | xargs tail -20
```

查 stderr。常见原因：知识库路径不可达、claude CLI API key 失效、超时。

**清空队列**（慎用，丢失待总结）：

```bash
> ~/.claude/skills/summarize-session/auto-queue.jsonl
```

## 已知限制

- **草稿合入是追加**到目标文件末尾，不做语义合并（由人工在审阅阶段判断"已有同类规则就删草稿"）
- **LLM 二次价值判断有可能误判**；dry_run 7 天观察是缓解措施
- 自动模式产生的笔记/CLAUDE.md 草稿，由跑 `--auto` 时的 LLM 判断质量决定；模型从 Sonnet 升级到 Opus 可改 `auto.model`

## 卸载/回滚

```bash
# 卸载定时任务（跨平台）
# ${CLAUDE_PLUGIN_ROOT} 由 Claude Code 在插件安装时自动注入（指向插件 cache 目录）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/uninstall.py"

# 同时清除运行时数据（auto-queue、日志、草稿等）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/uninstall.py" --remove-data

# 关 enabled
python3 -c "
import json,pathlib
p=pathlib.Path.home()/'.claude/skills/summarize-session/config.json'
d=json.loads(p.read_text())
d['auto']['enabled']=False
p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
"

# 删 SessionEnd 注册（可选）
# 编辑 ~/.claude/settings.json，从 hooks.SessionEnd 移除 enqueue_auto_summary.py 的条目

# 删 SessionStart 注册（可选）
# 编辑 ~/.claude/settings.json，从 hooks.SessionStart 移除 session-start-auto-notify.sh 的条目

# 清运行时数据（如未用 --remove-data）
rm -rf ~/.claude/skills/summarize-session/{auto-queue.jsonl,auto-runs,auto-drafts,.auto-paused}
```
