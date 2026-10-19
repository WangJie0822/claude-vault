# frontmatter 缓存规范

索引重建脚本（`scripts/rebuild_index.py`）使用 `$VAULT/.meta/frontmatter-cache.json` 缓存笔记的 frontmatter 元数据，避免每次全量读取所有文件。

## 缓存格式

```json
{
  "_version": 1,
  "_parser": 3,
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

- `_parser`（可选）为写端解析器修订号（`rebuild_index.PARSER_REV`），只有 `rebuild_index` 写它；读端（vault-loader）不读，**不触发 `_version` 变更**。解析口径改变、同一篇笔记会解析出不同结果时 +1（2 = 未装 PyYAML 时的降级解析器认 YAML 块序列；3 = frontmatter 读到闭合行为止，见下方「读取窗口」）。`keywords_gap --set` 与摘要同步两个出口只改单条 entry、整份写回，原样保留该键，不补写
- **读取窗口**：每篇先读前 2000 个字符；首行是 `---` 而这段里没有闭合行时继续往下读，直到读到闭合行、文件结束或 65536 个字符（`rebuild_index.FM_SCAN_CHARS`）。读到文件结束仍没有闭合行的，按没有 frontmatter 处理（与 `_parser` 3 之前相同）；读满 65536 个字符仍没有闭合行的，entry 不记 tags 等 frontmatter 字段，另记 `"fm_truncated": true`。`_parser` 3 之前只读前 2000 个字符：frontmatter 的闭合行落在这之外时整段读不到，tags 为空
- `fm_truncated`（可选）只在上述「读满仍未闭合」时写入、值恒为 `true`，纯增量字段，**不触发 `_version` 变更**。读端（vault-loader）见到它就把这一篇的 tag 判为读不全：排除 tag 非空时不送给精排；粗排不受影响。旧 cache 没有它，读端当作没有
- key 为相对于 `$VAULT` 的路径
- `mtime` 为整数秒 Unix 时间戳（通过 Python `os.path.getmtime()` 获取，无跨平台问题）
- 其余字段为 frontmatter **原始值**（不做降级推断）
- `keywords`（可选）为 frontmatter 的检索扩展词数组，纯增量字段，**不触发 `_version` 变更**；旧 cache 无此字段时读端默认空、平滑降级
- 缺失的字段不写入（不在 frontmatter 里的字段、`fm_truncated` 为假时都不写）；例外：文件无 `summary` 时以正文首个 `# ` 标题兜底写入 `summary`，并记 `summary_src: "h1"`（frontmatter 自带的记 `"fm"`）。`summary_src` 是可选增量键，不触发 `_version` 变更

## 失效与重建

| 场景 | 处理 |
|------|------|
| 缓存文件不存在 | 全量读取，创建缓存 |
| `_version` ≠ 1 | 丢弃，全量重建 |
| `_parser` 缺失、类型不对或小于当前修订号 | 保留 entries，忽略 mtime 全量重解析一次，写回当前修订号 |
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
SS=$(ls -d ~/.claude/plugins/cache/*/*vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
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
