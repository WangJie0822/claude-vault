这是兼容旧 Claude slash command 的薄入口。实际规则以 `skills/vault/SKILL.md` 为准：路径优先读取 `~/.context-vault/config.json::vault_path`，旧 `~/.claude` 配置仅作兼容回退。Codex 直接调用 `vault` skill。

规则：
1. 先读取 <vault>/CLAUDE.md 了解所有笔记的结构（<vault> 为配置的知识库路径）
2. 根据 $ARGUMENTS 或当前对话上下文，判断需要加载哪些笔记
3. 仅加载相关笔记，不要全量读取
4. 如果 $ARGUMENTS 为空，列出所有可用笔记供用户选择
5. 加载后简要总结关键信息
