# Memory 沉淀规则

## 扫描范围

仅扫描当前项目对应的 `~/.claude/projects/<当前项目>/memory/` 目录。跳过 `MEMORY.md` 索引文件。

如果 Glob 结果仅包含 `MEMORY.md` 或为空，跳过整个 Memory 沉淀环节。

## 按类型处理

读取每个 memory 文件的 frontmatter（`name`、`description`、`type`），按 type 分类：

| type | 沉淀目标 | 沉淀后处理 | 原因 |
|------|---------|-----------|------|
| `user` | `$VAULT/偏好与习惯/` | **保留源文件不删除** | 体积小，Claude Code 高频使用，删除会导致知识断裂 |
| `feedback` | `$VAULT/偏好与习惯/` | **保留源文件不删除** | 直接影响 Claude Code 行为，删除后未来会话无法自动遵循 |
| `project` | 对应领域文件夹 | 有效的沉淀后删除；过期的直接删除 | 时效性强，过期后无用 |
| `reference` | `$VAULT/参考资料/` | 沉淀后删除 | 可从 Vault 重新检索 |
| 未知/缺失 | 按 name 和 description 推断 | 无法判断的跳过 | 安全优先 |

## 沉淀方式

按主题聚合到对应领域笔记中（不是一个 memory 一个笔记），创建或追加。笔记格式遵循 `note-format.md`。

## 验证与清理

1. 对每个标记为"沉淀"的 memory，确认目标笔记已成功写入（Read 验证文件存在且内容包含沉淀内容）
2. 仅对**验证通过**且**类型为 project 或 reference** 的 memory 执行 `rm` 删除，同步更新对应项目的 `MEMORY.md` 索引
3. user/feedback 类型始终保留源文件，即使沉淀成功
4. 验证未通过的 memory 文件保留不动，在输出中标注"⚠️ 沉淀失败，源文件已保留"
