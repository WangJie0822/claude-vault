# frontmatter 缓存规范

索引重建脚本（`scripts/rebuild_index.py`）使用 `$VAULT/.meta/frontmatter-cache.json` 缓存笔记的 frontmatter 元数据，避免每次全量读取所有文件。

## 缓存格式

```json
{
  "_version": 1,
  "entries": {
    "相对路径/笔记.md": {
      "mtime": 1711267200,
      "tags": ["tag1", "tag2"],
      "category": "分类名",
      "status": "active",
      "summary": "一行摘要",
      "updated": "2026-03-24",
      "created": "2026-03-19",
      "keywords": ["扩展词召回", "recall"]
    }
  }
}
```

- key 为相对于 `$VAULT` 的路径
- `mtime` 为整数秒 Unix 时间戳（通过 Python `os.path.getmtime()` 获取，无跨平台问题）
- 其余字段为 frontmatter **原始值**（不做降级推断）
- `keywords`（可选）为 frontmatter 的检索扩展词数组，纯增量字段，**不触发 `_version` 变更**；旧 cache 无此字段时读端默认空、平滑降级
- 缺失的字段不写入（如文件无 `summary`，则 entry 中无 `summary` key）

## 失效与重建

| 场景 | 处理 |
|------|------|
| 缓存文件不存在 | 全量读取，创建缓存 |
| `_version` ≠ 1 | 丢弃，全量重建 |
| JSON 解析失败 | 丢弃，全量重建 |
| 文件已删除 | 从 entries 移除 |
| 文件 mtime 变化 | 重新读取该文件 frontmatter |
| 用户手动删除缓存 | 等同首次运行 |
| 版本升级 | 一律丢弃重建，不迁移旧格式 |

## 索引排序兜底

排序时 `updated` 可能缺失，按以下优先级取值：
1. `updated`（如有）
2. `created`（如有）
3. mtime 转换为 `YYYY-MM-DD` 格式

## 索引区标记

CLAUDE.md 中的索引区由开始和结束标记界定，脚本只替换两个标记之间的内容：

```markdown
<!-- 索引区：以下内容由 /summarize-session 自动生成，请勿手动编辑 -->
（自动生成的索引表格）
<!-- /索引区 -->
```

结束标记 `<!-- /索引区 -->` 之后的内容不会被修改。如果 CLAUDE.md 中缺少结束标记，脚本会自动补上。

## 索引表格格式

```markdown
## [分类名]

| 笔记 | 摘要 | tags | status |
|------|------|------|--------|
| [[笔记名]] | summary 内容 | tag1, tag2 | active |
```

降级策略（生成时执行）：
- 缺少 `category` → 从文件路径的文件夹名推断
- 缺少 `summary` → 显示 `(待补全)`
- 缺少 `tags` → 显示 `—`

特殊处理：
- 工作日志分类只显示最近 7 天条目，超出部分汇总为一行
- `status: archived` 的笔记在分类底部用一行汇总
- 索引区控制在 200 行以内

## 脚本用法

```bash
SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
# 正常运行
python3 "$SS/rebuild_index.py" --vault "$VAULT"

# 指定缓存路径
python3 "$SS/rebuild_index.py" --vault "$VAULT" --cache /path/to/cache.json

# 预览索引内容（不写入文件）
python3 "$SS/rebuild_index.py" --vault "$VAULT" --dry-run

# 自定义参数
python3 "$SS/rebuild_index.py" --vault "$VAULT" --max-log-days 14 --max-lines 300
```

脚本输出 JSON 报告：
```json
{
  "total_notes": 42,
  "scanned": 3,
  "deleted": 0,
  "index_updated": true
}
```
