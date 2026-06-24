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
