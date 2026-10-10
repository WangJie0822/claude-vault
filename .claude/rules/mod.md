---
paths:
  - "hooks/vault-mod.js"
  - "hooks/vault-mod.d.ts"
  - "skills/vault-loader/scripts/_session_ops.py"
  - "skills/vault-loader/scripts/_annotations.py"
  - "tests/mod/vault-mod.test.ts"
---

# 规则正文：mod（注入可见与可控）

> 本文件是 CLAUDE.md「硬约束索引 · mod（注入可见与可控）」的正文。

## 现行补充

- 通道：mod 在 `classic.UserPromptSubmit` 的 `next()` 之前写按轮请求文件 `state/live/<sid_h>-<prompt_id>.req.json`，事件 `e` 原样传递；Python 只凭 stdin 的 `session_id` / `prompt_id` 算路径，读完即删。**不要改回「往 e 里加字段」**：settings hook 收到的 stdin（含 HTTP、prompt、agent 类）会一并收到这些笔记路径与标注，且「新增字段进 stdin」只有实测、文档未写。
- 快照只在本轮请求存在过时写，所有出口经 `TurnReport.finalize` 统一写：停用类 `disabled`、闸门经 `_stage_gate_record` 记 `gate:<reason>`、召回路径 `scored` / `dry_run`。重复事件既不写快照也不删请求：请求属于抢到事件的那个进程，删掉会让它读空。新增出口不用逐个改，但新增闸门必须走 `_stage_gate_record`。
- `state/live/` 与命名空间无关，**不要**挪进 `state/claude/`：未迁移机器上那里不得有文件（`test_claude_only_namespace.py`）。
- 快照显示字段必须先折单行再 `sanitize_for_display`，并剥 bidi / 零宽字符；`path` 保留原始 cache 键（回传时要与 `active_entries` 精确匹配）。mod 读快照的字段由 `tests/test_mod_static.py` 钉到 `snapshot_v1.json` 的键集合。
- 排除在决策层**最先**判（早于两个去重分支），`dedup="user_excluded"` 不是「曾注入过」：报表按「曾注入过」分桶的地方必须单列它。
- 手动钉住与 LLM 点名分开渲染、分开计 metrics；`PIN_HEADER` 一字不改；合计上限只走 `_topic.split_pin_budget`。
- 标注：mod 投递的标注一律按服务端 metrics 记录核对：两个 `admitted_*` 按 `prompt_h` 找那一轮、再看 `shown` / `ft`；`llm_pinned` 只核对 path 是否在该会话任一记录的 `llm_pinned` 里，它的 `context_ids` 里的 `prompt_h` 取自请求体、没有核对（mod 不产出这类标注）。每个会话只扫一次 metrics 文件；整批只取一次 `lease_lock`（timeout 0.25 s、`hard_stale_after` 600 s，常量在 `_annotations`）、锁内读一次已有 id、一次追加——写入位于 emit 之前，逐条取默认 2 s 锁时 N=50 实测阻塞 100.4 s（节点 2 P-H1）。kind 必须与快照条目一致：全文注入的那篇是 `admitted_fulltext`，按 `admitted_list` 投递会被核对拒收、mod 随即删掉它。
- 标注拒收是终局（mod 收到即删、不再重投），只用于确知不该写的情形；判不了的既不 ack 也不拒收。新增一种 reason 时必须先决定它是不是终局。现状见下文「契约 v1」。
- mod 的「本会话」按会话 id 归属：`classic.SessionStart` 与 `classic.UserPromptSubmit` 里会话 id 变化时重置 OPS / WARN / VIEW。**不依赖** `$.state` 是否随 `/clear`、`/resume` 重置——类型文档与 spec 的记载不一致，未实测；两种语义下这段都正确。
- 两个 `classic.*` hook **刻意不加 `.catch`**：`claude plugin validate` 会报「gating hook without .catch」，那只是陈述；kit 探针（节点 2 4A，含阳性对照）证实失败的 hook 被跳过、下层照常答复，默认就是 fail-open。照 guard 范式加返回 `{ block }` 的 `.catch` 反而会在 mod 出错时拦截提问。
- 停用逃生阀开着时，有 mod 请求的那一轮仍写 `status=disabled` 的快照，SessionStart 仍清理 `live/`（spec §5.4 的设计）；要让 `live/` 完全不写入需同时关 `mod_enabled`。
- mod 的测试不走 pytest：仓库根 `claude plugin test`（`tests/mod/*.test.ts`）。根 `tests/test_mod_static.py` 以临时 HOME 起它与 `claude plugin validate`（两者都会往 HOME 写 `.claude.json` 等文件），PATH 上没有 `claude` 时这两条 skip。
- mod 不起子进程、不联网：`tests/test_mod_static.py` 的 `calls` 白名单钉住；要扩白名单先评估当前目录劫持与外发面。
- home 规则：mod 侧 `OS=Windows_NT` 取 `USERPROFILE`，否则 `HOME`；与 Python `Path.home()` 一致。两侧不一致时只会「请求读不到」，不会写错地方。
- 门禁实测（2026-10-10，Windows 11）：
  - 旧版本对 manifest 新字段：Claude Code 2.1.286、2.1.250、2.1.220 都不因 `hooks.json` 的 `modules` 或 `plugin.json` 的 `types` / `userConfig` 拒绝插件，命令 hook 照常执行；mod 只在 2.1.286 上加载，2.1.250 与 2.1.220 上没有 band 与 `/vault-injected`，召回不受影响。
  - 2.1.293：`$.env.get('OS')` 为 `Windows_NT`；`$.fs.write` 自动创建多级目录；`claude -p` 下 `$.session.surfaces()` 为 `[]`（所以 `-p` 不写请求）。
  - 2.1.293 的 `claude plugin test`：不能 import JSON（只认 `.ts` `.tsx` `.jsx` `.js` `.mjs` `.cjs` `.mts` `.cts`）；`fs.*` 事件到达测试的 hook 之前路径已解析成宿主 OS 的绝对路径，stub 不能按 mod 拼出的原样字符串比对。
  - 2.1.293 的 `claude plugin validate`：对经由辅助函数的调用写作 `$.env.get (via homeDir)`，括号里可能含逗号；静态校验拒绝把 `$` 的成员当值读（`typeof $.store.keys` 会让模块不加载）；仓库根有 `CLAUDE.md`、`commands/vault.md` 没有 frontmatter，两条 warning 使 `--strict` 失败。
  - `$.store` 落点：`<配置目录>/plugins/store/<插件名>_<来源>-<哈希>.json`（只在 `--plugin-dir` 加载、来源为 `inline` 时测过）。

## 契约 v1（mod ↔ Python）

两侧都按 `v === 1` / `v == 1` 判等；改任何一条都要两侧同改，并同步 `tests/fixtures/mod_contract/` 与 `tests/mod/vault-mod.test.ts`。

- **请求**（mod 写，Python 读完即删）：`live/<sid_h>-<prompt_id>.req.json`，`sid_h = sha256(session_id)[:24]`，`{v, exclude[], pin[], unpin_llm[], annotations[]}`。SessionStart 的是 `<sid_h>-ss.req.json`，内容恒为 `{}`，只作「有 mod 在」的标记，Python 不读其内容（排除因此不作用于会话开始注入）。
- **上限**两侧镜像（JS `LIMITS`、Python `MAX_*`）：exclude 50、pin 8、unpin_llm 8、annotations 50，单条字符串 ≤ 512 字符，请求 ≤ 1,000,000 字节。mod 在按钮处到上限即拒绝并 toast；Python 截断并报 `over_cap`。
- **摘要**：`sha256(JSON([v, exclude, pin, unpin_llm, 标注 id 列表]))[:32]`，取校验之前的原样列表；Python 用 `ensure_ascii=False, separators=(",", ":")`。唯一用途：mod 判断快照回应的是不是自己发的那份请求，不一致即告警。固定值守卫：`request_v1.json` 的摘要是 `caef2f245a8dd2710078881708d351e5`（两侧用例都钉着）。
- **快照**（Python 原子写，mod 只读）：`live/<sid_h>.json`，顶层键由 `snapshot_v1.json` 钉住。`status`：
  - `disabled`（停用类出口）、`gate:<reason>`（闸门）、`scored` / `dry_run`（召回路径）；
  - `pending`：已过事件去重、却没落到上面任何一个出口（SessionStart 的 vault 不可达出口就是这样；UPS 异常退出时也会）；
  - `duplicate` 不写快照。
  - mod 只在 `scored` / `dry_run` 更新注入列表、钉住、排除、`prompt_h` 与 `metrics_on`；其余状态保留上一次的展示。
- `metrics_on` 反映本轮读到的配置（闸门轮也如实报）；`prompt_h` 只在 metrics 开启的打分轮出现，是标注的目标。`hits` 只在 metrics 开启时写。
- **`rejected[]`**：`{what, key, reason}`，`what` ∈ `exclude` / `pin` / `unpin_llm` / `annotation` / `request`。`reason` 与 mod 的处理：
  - `metrics_off`（配置正常读出且 metrics 关闭）、`no_record`（会话 metrics 文件读得到、但没有能核对上的记录）、`bad_item`：annotation 的这几种都是终局，mod 按 `key` 从队列删除；`over_cap` 的 `key` 为空，不对应具体条目（mod 每份请求本就只带 50 条）；
  - `not_in_index`：mod 把这条路径从 OPS 删掉；`key` 截到 64 字符，路径更长时 mod 删不掉它（Q-L3，已知）；
  - `bad_request`：整份请求作废。
  - 列表上限 120（= 一份合法请求最多可能产生的条数），不得截得更短：截掉的 `not_in_index` 让 mod 永远收不到「已失效」。
- **既不 ack 也不拒收**（留在 mod 队列，7 天 TTL）：config 读坏回落默认值的那一轮、dry_run、会话 metrics 文件一个都读不到（`--purge` 之后与命名空间分裂分不开）、取锁超时。
- **`acked[]`**：已写入、或已由别的会话写入的标注 id（同 id 只写一次），mod 从队列删除。
- **清理责任**：请求由 Python 读完即删（重复事件除外，留给抢到事件的进程）；快照超过 24 h、请求与写快照的临时文件超过 1 h 由 SessionStart 的 `cleanup_live` 删，只认这三种文件名，不跟随链接与 reparse point；`--purge` 删 `live/` 下的这三种文件；store 队列由 mod 读队列时按 TTL 清。Python 不运行（未装、或 home 两侧不一致）时请求文件无人清理（已知）。
