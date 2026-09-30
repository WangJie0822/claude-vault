---
paths:
  - "skills/summarize-session/**/*"
---

# 规则正文：summarize-session

> 本文件是 CLAUDE.md「硬约束索引 · summarize-session」的正文。「## 迁移原文」小节自 CLAUDE.md（基线 `3ac359f`，该提交只在开发仓库可取）迁出，除少量已失效的指代、路径与行号锚点外逐字保留；小节内「本文件」「本节」「此处」「上一版」凡指文档位置的，均指原 CLAUDE.md，出现的数字是当时的读数。此后新增内容写在「## 现行补充」里，按 CLAUDE.md「写入约定」执行。

## 迁移原文（基线 `3ac359f`）

### summarize-session

skill 驱动（`SKILL.md` 即编排逻辑），辅以 `scripts/` 下脚本。模式：正常 / `-f`（强制，跳确认）/ `--catch-up` / `--quick`。Vault 内资源优先经 `scripts/obsidian_cli.py` 封装，Obsidian CLI 不可用时降级文件 I/O。

#### `git_commit_vault` 的纳入判据是**排除法**，且只看顶层段

`_is_knowledge_md` 决定哪些 `.md` 会被自动 commit 进用户的 Vault。2026-09-03 由「顶层目录白名单」反转为「除顶层段以 `.` 开头者外一律纳入」——白名单每新增一个顶层目录就要改代码，忘了改则**静默漏提交**（实测某 Vault 有 6 篇长期滞留、其中 4 篇从未入 git，而使用者的 CLAUDE.local.md 直接引用它们）。

两条必须记住的边界：

- **判据只看顶层段**，因此嵌套在笔记目录下的工具目录（`技术笔记/.trash/`、`工作日志/.obsidian/`、`笔记/node_modules/`）**仍会被提交**，而索引器 `scan_vault` 按**任意深度**排除且集合还不同（含 `node_modules`）。两边不一致是**既有**行为——旧白名单判据同样只看顶层段，凡顶层段在白名单内其下任意深度都放行（已用旧实现逐条对跑确认）。判定「某个暴露是不是本次引入」时务必先跑这个对照，否则会把既有行为误记成新缺陷。
- **点目录排除必须先于 `is_system_index`**。后者的规则是「文件名前缀 == 父目录名」，工具目录下同样能构造出该形态（`.obsidian/.obsidian 索引.md`），放在它后面会被抢先放行。2026-09-07 已把该分支整体移除（排除法下它已无独立作用，只剩这个副作用）。

工具只 commit 不 push；正常路径 `git add -- <changes>` 精确 add，`--baseline` 的全量 `-A` 由 skill 经 preview 确认后才放行。

#### keywords 缺口的三条防线

`keywords` 是 vault-loader 的精确召回通路（权重 5，最强信号），缺失即该篇在提问时召不回来。写期口径的**唯一实现**在 `scripts/_keywords.py::sanitize_keywords`（CJK ≥2 / ASCII ≥3、去重、上限 8、剔 YAML 元字符），三条写入路径共用——口径分裂过一次就再也对不齐：

1. **源头**：`archive_doc.py` 归集 spec/plan 时从 pending-docs 条目透传 `keywords`，走与 `summary`←`context` 完全相同的通路，**零额外模型调用**（写 `context` 时本来就在写）。生产方契约见 `references/doc-collection.md`。
2. **兜底**：`keywords_gap.py --list/--set`，接在 `SKILL.md` 第四步 `rebuild_index` 之后，由会话里的 LLM 读笔记自己生成再写回，同样零调用。缺口判定**复用 `rebuild_index._health_check`**，不另写判据（否则会出现「报告说缺 5 篇、补全只看到 3 篇」这种查不出来的漂移，`test_list_total_matches_rebuild_index_coverage_numerator` 是这条契约的守卫）。
3. **清存量**：`enrich_keywords.py`，**付费**（每篇 spawn 一次 `claude -p --model haiku`）、手动 opt-in、不接自动管线。

> **为什么需要三条**：归集是自动的、补齐原先只有付费手动一条，两者速率不匹配 ⇒ 缺口必然随时间线性增长。作者本机实测：最后一次 backfill（2026-07-27）之后 12 天累积 37 篇，覆盖率 99%→96%，且 37/37 全部是归集进来的 spec/plan。2026-08-10 补齐后为 **100%**（applicable 973）。

> ⚠️ **`--set` 必须同步 cache，这不是优化而是正确性必需**：`rebuild_index` 的增量判据是秒级 `int(st_mtime)`（`rebuild_index.py:163`），而「补全 → rebuild」在流程里必然落在同一秒内 ⇒ rebuild 判定文件未变、跳过重读，cache 永远停在「无 keywords」。后果不是慢一拍而是**补了等于白补**——vault-loader 读的是 cache，召回拿不到 keywords，下次会话还会把同一篇再列一遍。实测：mtime `…860.0094`→`…860.1227`，`int()` 同为 `1786345860`。故 `set_keywords` 写完文件后直接改 cache 对应 entry（fail-open，结果在返回值 `cache_synced` 里如实报出）。同一根因也影响 `enrich_keywords.py`，只是它每篇要调一次 haiku、跨秒概率大，不易触发。
