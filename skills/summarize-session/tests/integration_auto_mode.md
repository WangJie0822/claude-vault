# 自动模式集成验证清单

## 前置准备

- [ ] config.json 中 `auto.enabled=true, auto.dry_run=false`
- [ ] launchd plist 已安装(`launchctl list | grep summarize-session-auto`)
- [ ] 选一个已知有沉淀价值的会话 ID(有 Edit/Write 操作、`total_messages > 10`、`size_kb > 50`)
- [ ] 从 `~/.claude/skills/summarize-session/summarized-sessions.json` 中临时移除该会话(若存在)

## 触发方式

```bash
TEST_SESSION="<填入>"
# 直接 headless 调用(不通过 cron)
claude -p --model claude-sonnet-4-6 \
  "/summarize-session --auto --session $TEST_SESSION" \
  > /tmp/auto-test.log 2>&1
echo "exit=$?"
```

或通过 launchd 触发:

```bash
# 先用 enqueue 脚本入队(模拟 SessionEnd hook)
SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
python3 "$SS/enqueue_auto_summary.py" \
  --session "$TEST_SESSION" --cwd "$(pwd)"

# 立即触发 cron
launchctl kickstart -k "gui/$UID/com.user.summarize-session-auto"
sleep 60  # Sonnet 一个会话约 30-60s
RUN_LOG=$(ls -t ~/.claude/skills/summarize-session/auto-runs/run-*.log | head -1)
cat "$RUN_LOG"
```

## 验证项

### V1: LLM 二次价值过滤输出

- [ ] stdout 第一行匹配正则 `^LLM_VALUE_VERDICT=(continue|skip)`
- [ ] 若 `skip`,后续 stdout 含 `reason=...` 短语
- [ ] 若 `continue`,跳到 V2-V4

### V2: 草稿区写入正确

仅当 V1 = continue:

- [ ] `~/.claude/skills/summarize-session/auto-drafts/$(date +%F)/` 目录存在
- [ ] 至少一个 `*.draft.md` 文件首行为 `---`
- [ ] frontmatter 含 `target_path`、`source_session`、`source_date`、`type` 四个字段
- [ ] `target_path` 指向真实存在的目标文件(`~/.claude/CLAUDE.md` 或项目 CLAUDE.md 或 memory/*.md)

### V3: 笔记/工作日志直写 Vault

仅当 V1 = continue:

- [ ] `~/Vault/` 下今天有新增/追加的笔记(`find ~/Vault -newer /tmp/auto-test.log -type f -name "*.md"`)
- [ ] `~/Vault/工作日志/$(date +%F).md` 存在并已追加条目

### V4: 标记已总结

- [ ] `~/.claude/skills/summarize-session/summarized-sessions.json` 含 `$TEST_SESSION`

### V5: 不交互/不阻塞

- [ ] 整个调用 stdout/stderr 没有出现"等待用户输入"提示(无 AskUserQuestion 调用残留)
- [ ] 进程退出码为 0
- [ ] 日志中无 stack trace 异常(除非 V1=skip 触发的预期跳过)

## 跨链路验证(SessionStart 提示)

- [ ] 新开 Claude Code 会话,看会话首条消息是否注入 `📝 自动总结草稿待审:N 项,运行 /summarize-session --review-drafts 查看`(N=V2 中草稿数)

## 草稿审阅+合入工作流

- [ ] `/summarize-session --review-drafts` 输出按 type 分组的 unified diff,可读
- [ ] 编辑/删除部分草稿(`~/.claude/skills/summarize-session/auto-drafts/$(date +%F)/*.draft.md`)
- [ ] `/summarize-session --apply-drafts` 输出 `✅ 合入 N 项,❌ 失败 0 项`
- [ ] 目标文件已追加草稿正文,草稿目录已清空

## 失败处理验证(可选)

- [ ] 把 `auto.session_timeout_sec` 改为 5(秒),触发 cron 后日志含 `STATUS=timeout`
- [ ] `failure_count` 累加到 3 后,该会话从队列出队并标记 `permanent_skip`
- [ ] `auto-runs/run-*.log` 保留 30 天后被 `find -mtime +30 -delete` 清理
