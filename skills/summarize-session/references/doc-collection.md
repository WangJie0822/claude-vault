# 文档归集规则

## 入口

`scripts/sync_pending_docs.py` 是唯一归集入口。SKILL.md 第四步调用：

```bash
SS=$(ls -d ~/.claude/plugins/cache/*/claude-vault/*/skills/summarize-session/scripts 2>/dev/null | sort -V | tail -1)
python3 "$SS/sync_pending_docs.py" \
  --vault "$VAULT" \
  --mode incremental \
  --apply \
  --output-json /tmp/summarize_sync_result.json
```

`/summarize-session --backfill-archive` 走：

```bash
python3 .../sync_pending_docs.py --vault "$VAULT" --mode backfill --output-json ...
# 输出 dry-run 报告，等用户加 --apply 才写
```

## 条目字段契约（谁填什么）

`pending-docs.json` 是一个 JSON 数组。**生成文档的一方**（写 spec/plan 的那次会话）只填下表前几项，其余一律由 `sync_pending_docs.py` 自动维护，手填会被覆盖或导致状态错乱。

| 字段 | 谁填 | 说明 |
|---|---|---|
| `path` | 生产方 | 文档的**绝对路径**。相对路径会被判 `path_invalid` 永久跳过、不归集 |
| `type` | 生产方 | `spec` / `plan` / `memory` / `note` / `other`，决定 Vault 内的落点子目录 |
| `context` | 生产方 | 一行描述。归集时截 200 字写进 frontmatter 的 `summary` |
| `keywords` | 生产方（可选，强烈建议填） | 字符串数组，3-8 个检索扩展词。归集时经 sanitize 写进 frontmatter |
| `created` | 生产方 | ISO 时间 |
| 其余全部 | `sync_pending_docs.py` | `vault_path` / `*_hash` / `*_mtime` / `archived_at` / `wikilink_form` / `path_invalid` / `denied_sensitive` … **禁止手填** |

**`keywords` 为什么必须在这里填**：spec/plan 源文档本身没有 frontmatter，Vault 副本的 frontmatter 全部由 `archive_doc` 生成。它此前不写 `keywords`，于是每归集一篇就留一个精确召回空洞——那篇笔记在提问时召不回来——只能靠事后手动跑付费的 `enrich_keywords.py` 补。而归集是自动的、补齐是手动的，两者速率不匹配，缺口必然随时间线性增长（作者本机实测：一次 backfill 之后 12 天累积 37 篇，覆盖率 99% → 96%）。

在条目里一并填上就没有这个问题，且**零额外模型调用**——写 `context` 时本来就在写。

词的质量约束见 `note-format.md`；非法词（YAML 元字符、单字、非字符串）由 `archive_doc` 静默剔除，不会破坏 frontmatter。全部非法或没填时**不写该键**，而不是写空数组——写空数组会让覆盖率统计把它算作「已有」，把缺口藏起来。

漏填也不会永久丢：`/summarize-session` 收尾时用 `keywords_gap.py` 检测缺口并补上（见 SKILL.md 第四步第 5 项）。

## 模式

| 模式 | 处理范围 | 默认 apply |
|---|---|---|
| `--mode incremental` | 全部条目（含已 vault_path 的同步检测） | `--apply` 默认开启（由 SKILL.md 第四步显式带）|
| `--mode backfill` | 仅无 vault_path 的条目 | 默认 dry-run；用户加 `--apply` 才写入 |

## 数据结构

详见 spec `docs/superpowers/specs/2026-05-28-summarize-session-doc-archive-design.md` 的「数据结构」节。

## 决策表（速查）

### 前置校验（任一命中即跳过归集）

- `path` 非绝对 → 标 `path_invalid=true`
- 命中敏感文件 deny-list → 标 `denied_sensitive=true`
- `path` 在 Vault 内 → short-circuit，vault_path 直接等于 path
- 原文件不存在 → 标 `original_missing=true` + `original_missing_since`

### 同名冲突 5 分支

1. 目标路径无文件 → 直接写
2. 目标存在 + `vault_source_repo/path` 与 entry 匹配 → 走"副本正文手工编辑检测"
3. 目标存在 + 无 `vault_source_*` + basename 匹配 → **adopt**（upsert frontmatter，正文不动）
4. 目标存在 + 有 `vault_source_*` 但不匹配 + `--rename-on-conflict` → 加 timestamp 后缀
5. 上述都不命中 → fail-fast

### 副本正文手工编辑检测

**注意**：`vault_content_hash` 是**正文部分（剥离 frontmatter 后）的 sha256**，不是整文件 hash。这样 frontmatter 改动（脚本权威字段如 vault_source_hash）不会影响 hash 比对，确保 stored hash 仅在用户手工改正文时变化。

| Vault body hash | 源 hash | 行为 |
|---|---|---|
| 同 | 同 | skipped_unchanged |
| 同 | 异 | 正常 synced（覆盖 Vault 副本）|
| 异 | 同 | `conflict_vault_edited`，**不覆盖** |
| 异 | 异 | `conflict_both_edited`，**不覆盖** |

## 错误处理

详见 spec「错误处理与边界」表。脚本内并发安全靠 `scripts/_fs.py:_acquire_lock`（LOCK_TIMEOUT=300s + mtime refresh + PID 探活）。
