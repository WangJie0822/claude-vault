# Codex 调用与会话归属

从当前已加载的 `SKILL.md` 的绝对路径定位同级 `scripts/`，记为 SS。PowerShell 直接使用该绝对路径；不要执行 Bash 定位器。路径拿不到时仅在当前 `CODEX_HOME`（未设置则 `~/.codex`）的 `plugins/cache/` 内查找本插件，核对版本与 `context-vault` / 旧 `claude-vault` 名称；不要借用另一个宿主的缓存。

## 当前会话与历史补录

从可信当前线程上下文取得线程 ID，并与 rollout 首条 metadata 核对。`CODEX_THREAD_ID` 只作待核验的输入；缺失、身份不一致或 incomplete 时停下，不按 cwd 的最新 mtime 猜测。

以下为 Bash 示例；SS 与线程 ID 都须先取得。PowerShell 用等价参数列表传值。

```bash
python3 "$SS/scan_sessions.py" --runtime codex --thread-id "$CODEX_THREAD_ID" --timerange "$PWD"
python3 "$SS/scan_sessions.py" --runtime codex --thread-id "$CODEX_THREAD_ID" --touched-repos "$PWD"
python3 "$SS/scan_sessions.py" --runtime codex --session "$CODEX_THREAD_ID" --parse --max-chars 30000
```

正文来源只取解析器的规范消息；不把工具回显、developer 指令、reasoning 或加密块当成用户内容。修改工具无法识别时，按 `touched_files_status=unknown` 表述，结合实际 git diff 核对，不声称零修改。解析文本截断后应继续读取该精确会话的相关部分，不凭标题补全事实。

正常总结沿用 SKILL 的笔记格式、工作日志、文档归集与索引流程，由当前会话模型写作，不调用补词/补摘要付费后端。规则写入目标是项目 `AGENTS.md`：先读其写入约定，遵守规则正文落点；Vault 索引名 `CLAUDE.md` 是共享数据契约，保持原名。

完成笔记写入、索引重建并回读成功后，才标记精确 ID：

`rebuild_index.py` 只更新已有 `CLAUDE.md` 的索引区；检查报告的 `index_updated` 与 `cache_written`，并读取根索引、分类索引和 cache。缺少索引文件/标记时按 Vault 的初始化约定处理，保留已有正文；退出码为 0 本身不代表索引已更新。使用通用流程的 `--emit all` 同步分类索引。

```bash
python3 "$SS/scan_sessions.py" --runtime codex --mark "$CODEX_THREAD_ID"
```

标记命令再次验证来源完整性；失败不得报告已总结成功。不要用按目录选最新的 Claude `--mark-current` 示例替代。历史补录先运行 `--runtime codex --days <N>` 列表，让用户选择，再对选定的 `--session <id...> --parse` 逐个总结；只有成功者进入 `--mark`，保留其他宿主的 manifest 条目。`skipped_sources` 要告知，不能解释为不存在会话。

## 模型与 Memory 边界

付费批处理须用户明确启用：补词先 `--dry-run`；补摘要默认只列，确认范围后才 `--apply --expect <N>` 或 `--propose`。加 `--backend codex`，可选 `--model <精确ID>`。优先级为本批参数、宿主配置、旧全局显式模型、Codex 原生配置；profile/provider 随宿主配置保留。Codex 固定 token 开销未测量时显示未知，不套用 Claude 的估值。

本宿主设 `$SKIP_MEMORY=true`，跳过 Claude Memory 的归集/删除步骤。不得虚构与 `~/.claude/projects/*/memory/` 同构的 Codex 目录；本技能不修改 Codex memory。长期知识写 Vault。未验证真实模型、原生 UI 或平台行为时，交付说明明确保留未验证状态。
