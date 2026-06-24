# --catch-up 回溯总结流程

当用户执行 `/summarize-session --catch-up [N]` 时，扫描最近 N 天（默认 7）的历史会话，找出未总结的会话并按需补充总结。

## 执行流程

### 第一步：扫描未总结会话

运行扫描脚本获取会话列表：

```bash
python3 ~/.claude/skills/summarize-session/scripts/scan_sessions.py --days $DAYS
```

脚本输出 JSON，包含：
- `scan_range_days`：扫描范围
- `total_sessions_in_range`：范围内总会话数
- `already_summarized`：已总结数
- `unsummarized_count`：未总结数
- `sessions`：未总结会话列表（含 session_id、project、date、size_kb、first_intent、total_messages）

### 第二步：展示列表并让用户选择

将扫描结果整理为可读列表，用 AskUserQuestion 让用户选择：

```
=== 未总结会话（最近 N 天） ===

已总结：X 个 | 未总结：Y 个 | 总计：Z 个

序号 | 日期              | 项目              | 主题                          | 消息数 | 大小
1    | 2026-03-26 14:30  | assistantskills   | 批量分析缺陷脚本优化           | 42     | 156KB
2    | 2026-03-25 10:15  | ProjectA          | 实现记账记录导入功能           | 28     | 89KB
3    | 2026-03-24 16:00  | VpaManager        | 修复语音助手授权问题           | 15     | 34KB
...

请选择要总结的会话（输入序号，如 "1,3" 或 "all" 或 "none"）：
```

**过滤规则**：
- 跳过文件 < 5KB 的会话（通常是初始化或极短对话）
- 按时间倒序排列（最新在前）
- 如果 `--project` 参数指定了项目，只显示该项目的会话

### 第三步：逐个解析并生成总结

对用户选择的每个会话：

1. 调用脚本解析完整对话：
   ```bash
   python3 ~/.claude/skills/summarize-session/scripts/scan_sessions.py --parse --session <uuid>
   ```

2. 分析解析出的对话内容，识别：
   - 技术决策和原因
   - 项目进展和关键产出
   - 踩坑记录和最佳实践
   - 用户偏好和规则（如有）

3. **重要约束**：历史会话的上下文是截断的，信息可能不完整。遇到不确定的内容：
   - 标注"（信息不完整，仅供参考）"
   - 不要编造或推测缺失的细节
   - 优先提取明确可靠的信息

### 第四步：合并生成更新计划

将所有选中会话的总结合并为一个更新计划，格式与正常总结相同：

```
=== 回溯总结（N 个历史会话） ===

知识库路径：$VAULT

📝 笔记更新（X 项）：
  - [新建] 领域/xxx.md — 来自 2026-03-26 会话
  - [追加] 领域/xxx.md — 来自 2026-03-25 会话

📋 CLAUDE.md 更新（X 项）：
  - [全局] 新增偏好：xxx — 来自 2026-03-24 会话

⏱️ 工作日志（N 条）：
  - $LOG_DIR/2026年/03月/2026-03-26.md — 14:30~16:00，1.5h，主题:批量分析缺陷脚本优化
  - $LOG_DIR/2026年/03月/2026-03-25.md — 10:15~11:20，1.1h，主题:实现记账记录导入功能
  每条工作日志的时段由对应会话 JSONL 的首尾 timestamp 自动推断，追加到会话日期当天的文件，不是今天。

跳过（无需记录）：
  - session abc123 — 仅初始化/短对话
  - session def456 — 临时调试，无持久价值
```

**回溯模式特殊规则**：
- **工作日志默认启用**——时段由会话 JSONL 的首尾 timestamp 自动推断（调用 `parse_session` 返回的 `timerange` 字段），每个会话生成一条条目，追加到**会话日期当天**的 `$LOG_DIR/YYYY年/MM月/YYYY-MM-DD.md` 文件而非今天。用户可通过 `/summarize-session --catch-up --no-log` 禁用
- **同步跑 `sync_pending_docs.py --mode incremental --apply`**——hash 比对幂等，不会重复归集已归集条目；可补漏（如原始会话被遗漏的 doc）。但跳过"对话扫描第二层"（散落 .md 兜底），因为历史会话上下文压缩后无法可靠扫描
- **不做 Memory 沉淀**——Memory 是当前会话的缓存，历史会话的 Memory 可能已过期
- 笔记中标注来源会话日期，便于追溯

### 第五步：执行并标记

用户确认后：

1. 执行笔记写入和 CLAUDE.md 更新（与正常流程相同）
2. 标记已处理的会话为"已总结"：
   ```bash
   python3 ~/.claude/skills/summarize-session/scripts/scan_sessions.py --mark <uuid1> <uuid2> ...
   ```
3. 重建索引
4. 输出确认报告

## 参数组合

| 命令 | 行为 |
|:-----|:-----|
| `--catch-up` | 扫描最近 7 天，列出未总结会话 |
| `--catch-up 3` | 扫描最近 3 天 |
| `--catch-up 30` | 扫描最近 30 天 |
| `--catch-up --project assistantskills` | 只扫描指定项目 |

## 已总结会话清单

清单文件：`~/.claude/skills/summarize-session/summarized-sessions.json`

```json
{
  "sessions": ["uuid-1", "uuid-2", ...],
  "updated": "2026-03-27T10:00:00"
}
```

正常总结流程（非 catch-up）结束时，也会将当前会话 ID 写入此清单，避免重复总结。
