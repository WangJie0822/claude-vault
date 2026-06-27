# 笔记格式与归类规则

## 文件夹归类策略

按以下优先级确定笔记的存放文件夹：

1. **已有笔记归属**：追加到已有笔记时，保持原有路径不变
2. **动态匹配现有文件夹**：Glob `$VAULT/*/` 获取现有文件夹列表，根据笔记内容语义匹配最接近的文件夹
3. **创建新文件夹**：实在无法匹配时，使用简短的中文名称创建新文件夹

**匹配参考**（**以下仅为示例，真实匹配以动态 Glob `$VAULT/*/` 的结果为准**，随 Vault 实际目录演变）：

| 内容领域 | 典型文件夹 |
|:---------|:----------|
| 缺陷分析工具链相关 | `缺陷全链路/` |
| PR Review / 代码审查 | `代码审查/` |
| 服务端部署 / Web 应用 | `服务端/` |
| Claude Code / Skill 开发 | `Claude Code/` |
| Android / 车载项目 | `车载开发/` |
| 跨领域设计文档 | `设计文档/` |
| 用户偏好 / 行为指引 | `偏好与习惯/` |
| 外部资源 / 参考链接 | `参考资料/` |
| 其他技术内容 | `技术笔记/` |
| 项目管理 / 流程 | `项目管理/` |

文件夹名应简洁、稳定，避免过于细粒度。按需创建，不预创建空目录。

## 文件命名

使用中文描述性名称，与现有笔记风格一致。
示例：`缺陷全链路/batch-analyze-bugs 技术决策.md`、`技术笔记/Kotlin 测试模式.md`

## 新建笔记 frontmatter

必填字段：

```yaml
---
tags: [相关标签]
category: 分类名
subcategory: 子分类名     # 仅当笔记位于 <category>/<sub>/ 子目录下时必填
created: YYYY-MM-DD
summary: "一行摘要"
---
```

选填字段（仅在有实际值时添加）：

| 字段 | 何时添加 |
|------|---------|
| `updated` | 修改已有笔记时，设为当前日期 |
| `keywords` | 几乎总是添加：3-8 个检索扩展词（同义词/别名/跨中英文术语），供 vault-loader 召回。与 tags 区分：tags 给人导航，keywords 给机器召回 |
| `related` | 有明确关联笔记时（纯文本名称列表） |
| `source` | 文档归集时记录原始路径 |
| `status` | 非 active 状态时（默认 active 无需显式写入） |

### keywords 生成规则（vault-loader 召回用）

写/更新笔记时为 `keywords` 产出 3-8 个**检索扩展词**：
- 收录：核心概念的同义词/近义词、英文↔中文对译、常见别名与缩写、用户可能换的说法。
- 质量约束（与 `enrich_keywords.py::sanitize_keywords` 同口径）：CJK 词 ≥2 字、ASCII 词 ≥3 字；禁单字与超宽通用词（"性能/优化/设计"——子串匹配会刷命中无关提问）；禁含换行与 YAML 元字符；每篇 ≤8 条。
  > 注：上述 CJK≥2/ASCII≥3 的完整口径由**写期** `enrich_keywords.py::sanitize_keywords` 强制；**读端** `_frontmatter_reader.load_cache` 只做更粗的「≥2 字」防御网（剔单字），不强制 ASCII≥3——主写路径（手写 frontmatter）的质量仍依赖本规则。
- 内联数组写法：`keywords: [扩展词召回, 相关性打分, recall]`。

### frontmatter 强约束（rebuild_index.py 强制）

| 约束 | 错误示例 | 正确示例 |
|------|---------|---------|
| `category` **不得含斜杠** | `category: 项目笔记/ProjectB` | `category: 项目笔记` + `subcategory: ProjectB` |
| **禁用** `project` 字段（脚本不识别） | `project: ProjectB` | `subcategory: ProjectB` |
| 子目录下笔记**必须**有 `subcategory` | `项目笔记/ProjectA/x.md` 缺 `subcategory` | `subcategory: ProjectA` |
| `subcategory` 应与所在目录名一致 | 笔记在 `ProjectA/` 但 `subcategory: ProjectA-ohos` | `subcategory: ProjectA` |

**自动诊断与修复**：

```bash
SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
# 仅诊断
python3 "$SS/rebuild_index.py" \
  --vault $VAULT --health-check-only

# 自动修复 frontmatter（拆斜杠 category / 删 project / 补 subcategory）
python3 "$SS/rebuild_index.py" \
  --vault $VAULT --emit=all --fix-frontmatter

# 归档孤立 INDEX 到 .meta/archived-indexes/<date>/
python3 "$SS/rebuild_index.py" \
  --vault $VAULT --emit=all --archive-stale-indexes
```

`rebuild_index.py` 默认输出 `health_check` 字段，含上述四类问题计数 + `stale_indexes` 列表，便于 skill 流程发现并修复。

### 无 frontmatter 的笔记如何归组

`plans/` 和 `specs/` 子目录下的临时实施计划/设计文档常无 frontmatter。`rebuild_index.py` 通过路径推断兜底：

- `<cat>/file.md` → 归到 `<cat>` 的"其他"
- `<cat>/<sub>/file.md` → 归到 `<cat>` 的 `<sub>` 分组
- `<cat>/<sub>/<x>/file.md` → 同上（深层目录仍归二级）

仍建议补 frontmatter 以保证 INDEX 的 summary/tags 字段完整。

## 笔记正文结构

```markdown
# 笔记标题

## 概述

（简要描述）

## 详细内容

（结构化的技术内容）

## 相关笔记

- [[相关笔记1]]
- [[相关笔记2]]
```

正文中使用 `[[wikilink]]` 语法引用其他笔记（无需包含文件夹路径，Obsidian 自动解析）。

## 存量笔记 frontmatter 兼容

更新已有笔记时，**仅追加缺失字段**，不修改或删除已有字段：

| 场景 | 处理 |
|------|------|
| 旧字段 `date` 存在，无 `created` | 新增 `created`（值同 `date`），保留 `date` 不动 |
| 缺少 `category` | 从文件所在文件夹名推断并新增 |
| 缺少 `summary` | 从笔记标题或首段文本提取并新增 |
| 已有自定义字段（`parent` 等） | 保留不动 |

## 追加到已有笔记

- 查重优先使用 `$VAULT/.meta/frontmatter-cache.json`（缓存中 key 为相对路径，包含文件名），直接在缓存中搜索目标笔记名。缓存不存在或未命中时，回退到 Glob `$VAULT/**/<笔记名>.md` 搜索
- 读取已有内容，找到合适的插入位置
- 追加新章节或在现有章节下补充
- 更新 frontmatter 的 `updated` 字段为当前日期
- 不破坏已有结构
