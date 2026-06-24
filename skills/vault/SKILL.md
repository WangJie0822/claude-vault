---
name: vault
description: 读取知识库索引，按关键词/分类/标签检索并加载相关笔记。对话涉及历史技术决策、使用偏好、踩坑记录、项目背景时，应主动用 /vault 检索相关笔记以提供有依据的回答；避免凭记忆或训练数据推测已沉淀在知识库中的项目特定知识。
argument-hint: "[关键词] [--category <分类>] [--tag <标签>] [--recent [N]]"
allowed-tools: Read, Glob, Grep, Bash(pwd), Bash(basename *)
---

# Vault 知识库检索

从知识库中检索并加载相关笔记，支持关键词搜索、分类浏览和标签过滤。

## 主动检索时机

以下场景 Claude **应主动运行 /vault** 检索，而非凭记忆或训练数据推断：

- 对话涉及**历史技术决策**（如"我们当时为什么选 X 方案"、"上次怎么解决的"）
- 对话涉及**使用偏好或约定**（如"我平时怎么处理 X"、"规范是什么"）
- 对话涉及**踩坑记录**（相关领域有无已知问题或避坑记录）
- 用户问"知识库里有没有..."或"之前记录过..."
- 新会话开始时，项目上下文（历史决策/近期进展）对当前任务有参考价值

**不需要主动检索**：纯技术问题（与个人知识库无关）、明确是临时一次性的操作。

## 配置

知识库路径按以下优先级确定（记为 `$VAULT`）：
1. 读取 `~/.claude/skills/summarize-session/config.json` 的 `default_vault_path` 字段
2. 如果配置文件不存在或字段为空，回退到 `$VAULT/`

索引文件：`$VAULT/CLAUDE.md`

## 参数解析

从 `$ARGUMENTS` 解析以下参数：

| 参数格式 | 作用 |
|:---------|:-----|
| （无参数） | 加载索引概览，展示所有分类和笔记数量 |
| `<关键词>` | 两级检索：先匹配索引摘要，再 grep frontmatter tags |
| `--category <分类>` | 加载指定分类下所有笔记的 summary 列表 |
| `--tag <标签>` | 按 tag 精确检索所有笔记 |
| `--recent [N]` | 按 updated 倒序加载最近 N 篇（默认 5） |

## 检索流程

### 无参数模式

1. 读取 `$VAULT/CLAUDE.md`
2. 统计各分类下笔记数量
3. 展示分类概览列表

### 关键词检索模式

1. 读取 `$VAULT/CLAUDE.md` 索引
2. 在索引表格中搜索 summary 和 tags 列，找到包含关键词的行
3. 如匹配不足（少于 1 条）→ 用 Grep 搜索 `$VAULT/` 下所有 `.md` 文件的 frontmatter 中 `tags:` 和 `summary:` 行
4. 汇总匹配结果，输出列表（笔记名 + summary）
5. 自动加载最相关的 1-3 篇全文

### 分类检索模式（--category）

1. 读取 `$VAULT/CLAUDE.md` 索引
2. 找到指定分类的表格区域
3. 输出该分类下所有笔记的 summary 列表
4. 如果分类不在索引中，用 Glob 搜索对应文件夹 `$VAULT/<分类>/`

### 标签检索模式（--tag）

1. 用 Grep 搜索 `$VAULT/` 下所有 `.md` 文件中 `tags:` 行包含指定标签的文件
2. 读取匹配文件的 frontmatter 提取 summary
3. 输出匹配结果列表

### 最近更新模式（--recent）

1. 用 Grep 搜索 `$VAULT/` 下所有 `.md` 文件的 `updated:` 字段
2. 按日期倒序排列
3. 加载前 N 篇（默认 5）的 frontmatter 摘要
4. 展示列表

## 上下文感知

检索时自动增强相关性：

1. 获取当前工作目录（`pwd`）和目录名（`basename`）
2. 从目录名中提取关键词（如 `projectb-assistant2.0` → `android`、`notes`、`demo`）
3. 将这些关键词作为隐式的 tag 过滤条件，在结果中优先展示匹配的笔记

关键词提取规则：
- 目录名包含 `assistant`/`android`/`iss`/`vehicle` → 关联 tags：`android`、`车载`、`语音`
- 目录名包含 `bug`/`defect`/`analyze` → 关联 tags：`缺陷全链路`
- 目录名包含 `claude`/`skill` → 关联 tags：`claude-code`、`skill`
- 其他 → 不做上下文增强

## 输出格式

检索结果统一使用以下格式展示：

```
📚 知识库检索结果

[分类名]
  - [[笔记名]] — summary 摘要 (tags: tag1, tag2)
  - [[笔记名]] — summary 摘要 (tags: tag1, tag2)

[分类名]
  - ...

已加载 N 篇全文（共匹配 M 篇）
```

如果没有匹配结果，提示用户调整关键词或使用 `--category` 浏览。

$ARGUMENTS
