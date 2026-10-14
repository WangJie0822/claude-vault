---
paths:
  - "skills/summarize-session/**/*"
---

# 规则正文：summarize-session

> 本文件是 CLAUDE.md「硬约束索引 · summarize-session」的正文。「## 迁移原文」小节自 CLAUDE.md（基线 `3ac359f`，该提交只在开发仓库可取）迁出，除少量已失效的指代、路径与行号锚点外逐字保留；小节内「本文件」「本节」「此处」「上一版」凡指文档位置的，均指原 CLAUDE.md，出现的数字是当时的读数。此后新增内容写在「## 现行补充」里，按 CLAUDE.md「写入约定」执行。

## 现行补充

### 摘要缺口的三条防线（2.3.0）

- **写期口径唯一实现**：判据与清洗在 `scripts/_summary.py`（`classify` / `sanitize_summary` / `reject_reason`），写盘只走 `_summary_store.write_with_backup`（备份 → `write_summary` → 同步 cache，三个写入点共用；基于 `_frontmatter_field`：保留原行尾、删除块标量续行、BOM / 未闭合 / 无 frontmatter 一律跳过）。不要改用 `_frontmatter.upsert_fields`：它非原子、会改写整份行尾、会给无 frontmatter 的笔记前插新块。
- **frontmatter 边界只有一套判据**：`_frontmatter_field.split_frontmatter`（按 LF 切行，首行 `strip() == "---"`，闭合行 `rstrip() == "---"`、不 lstrip），写端、`rebuild_index.parse_frontmatter`、`fm_has_summary`、`body_bytes` 共用。不要再写边界正则：2.3.0 full-review 时写端正则不认 `--- `、读端认，摘要被写进正文；读端把块标量里缩进的 `---` 当闭合、写端不认，人工摘要被判缺失后覆盖。降级解析器（无 PyYAML）须认块标量，否则 `summary: >-` 读成 `>-`、判过短后被覆盖。
- **净化删到不动点**：引号、反斜杠与结构 token 在同一循环里删，否则 `[x]"(…)` 删引号后重组出链接；入库值不做整体 NFKC，只折叠会落进结构字符集的字符。
- **cache 区分来源**：`rebuild_index._read_entry` 记 `summary_src`，`h1` 计为缺失；旧 cache 没有该键时 `classify` 读文件头 4 KB 判断。cache-spec 原先写「无 summary 则无该键」与代码相反，已订正。
- **摘要文本不进命令行**：`summary_gap.py --set-file` 读会话模型用 Write 工具写的 JSON。经 Bash 双引号传参时 shell 会先展开反引号与 `$`（评审 S08 实测）。
- **写前闸门**：`sanitize_summary` 之后还要过 `reject_reason`（敏感串 / 指令性短语、URL、路径、混合文字 / 套话 / 新值必须分档为 ok）。指令性词表只收明确短语——`assistant`、`instruction` 这类泛词会误杀名字里带 assistant 的项目名，单独的 `system prompt` 是技术名词、只拦「reveal / 输出 … system prompt」这类索取式说法；混写按连续字母分词判，µ（NFKC 后为希腊 μ）不算。依据：对作者 vault 现有 579 篇 ok 档摘要只读扫描（2026-10-13），收窄前拒 13 篇、收窄后 10 篇，剩下 9 篇含绝对路径（S11 有意拒写，摘要含路径的笔记需走 propose 或手写）、1 篇确为文件名复述。
- **缺口判定复用 `_health_check`**，`summary_gap --list` 的 `total` 与报告同源（守卫 `test_list_total_matches_rebuild_index_summary_gap`）；`--set-file` 写回同步 cache（理由同 keywords：秒级 mtime 判据）。
- **范围**：工作日志、无 frontmatter、归集文档（tags 含 `archived`）不纳入；归集文档的摘要唯一来源是 pending-docs `context`，`sync_pending_docs` 重归集会重写它。
- **付费路径**：`enrich_summary.py` 默认只列；`--apply` 只对缺口档，须 `--expect` 等于候选数；优化档只经 `--propose` → `--apply-proposals --only <id>`；每批备份可 `--restore`（只恢复当前值仍等于写入值的篇，坏行逐行跳过）；提交只含本批路径与进度文件里仍存在的遗留路径（`commit_vault(paths=, prefix=)`，路径按字面匹配）。
- **付费前后的四道比对**：列出时的值、调用前可写性（`unwritable` 不付费）、调用后重读（模型调用最长 60 s，期间的人工修改不得覆盖）、写入。计次只在 `_judge` 一处：内容拒写与写入失败计次，`model_failed` 不计；连续 `MODEL_FAIL_STREAK` 次 `model_failed` 熔断、退出码 4。
- **单实例锁按持锁进程存活判定**（`_fs._acquire_lock(timeout=0)`）：活的再旧也不抢，死的立即接管——只看 mtime 会让跑超时的批次被抢、被硬杀的锁挡住 `--restore`。`summary_gap --set-file` 遇批次锁跳过 `batch_running`，单篇缺口判定 `gap_kind` 与 `_health_check` 同口径（有对拍用例）、不跑全库。

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

> ⚠️ **`--set` 必须同步 cache，这不是优化而是正确性必需**：`rebuild_index` 的增量判据是秒级 `int(st_mtime)`（`rebuild_index.scan_vault`），而「补全 → rebuild」在流程里必然落在同一秒内 ⇒ rebuild 判定文件未变、跳过重读，cache 永远停在「无 keywords」。后果不是慢一拍而是**补了等于白补**——vault-loader 读的是 cache，召回拿不到 keywords，下次会话还会把同一篇再列一遍。实测：mtime `…860.0094`→`…860.1227`，`int()` 同为 `1786345860`。故 `set_keywords` 写完文件后直接改 cache 对应 entry（fail-open，结果在返回值 `cache_synced` 里如实报出）。同一根因也影响 `enrich_keywords.py`，只是它每篇要调一次 haiku、跨秒概率大，不易触发。
