读取知识库索引（路径由 ~/.claude/skills/summarize-session/config.json 的 default_vault_path 决定，默认 ~/.claude/knowledge-vault），然后根据用户的问题或当前上下文，按需加载最相关的笔记文件。

规则：
1. 先读取 <vault>/CLAUDE.md 了解所有笔记的结构（<vault> 为配置的知识库路径）
2. 根据 $ARGUMENTS 或当前对话上下文，判断需要加载哪些笔记
3. 仅加载相关笔记，不要全量读取
4. 如果 $ARGUMENTS 为空，列出所有可用笔记供用户选择
5. 加载后简要总结关键信息
