---
description: 补全或优化知识库笔记的摘要（summary）
argument-hint: "[gaps|rewrite] [路径...] [--include-archived] [--no-commit]"
---

这是 `/summarize-session --summaries` 的薄入口：补全或优化知识库笔记 frontmatter 的 `summary`。实际流程以 summarize-session skill 的 `SKILL.md`「摘要补全模式（`--summaries`）」一节为准。

规则：
1. 以 `--summaries $ARGUMENTS` 作为参数，按 summarize-session 的「摘要补全模式」执行
2. 付费调用前必须先只列候选、展示篇数与估算成本，并征得用户确认；`-f` 不豁免这一步
3. 优化已有摘要只能经 propose 生成对照、由用户按 id 选定后写入
