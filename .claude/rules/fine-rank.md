---
paths:
  - "skills/vault-loader/scripts/_fine_config.py"
  - "skills/vault-loader/scripts/_fine_state.py"
  - "skills/vault-loader/scripts/_fine_input.py"
  - "skills/vault-loader/scripts/_fine_flow.py"
  - "skills/vault-loader/scripts/_inject_format.py"
  - "skills/vault-loader/scripts/_session_ops.py"
---

# 规则正文：精排（逐轮补充、fine 状态、统一注入行）

> 本文件是 CLAUDE.md「硬约束索引 · 精排（逐轮补充）」的正文（2.4.0 新建）。设计依据是作者侧的「粗排/精排分离、逐轮精排补充」spec（不随仓库分发），下文的 Qx / Dx / Fxx 是其中的决策与评审编号。精排的接线在 `prompt_submit_load.py` 与 `_topic.py`，它们已被别的 rules 覆盖，改那两处涉及精排的部分时请自行 Read 本文件。

## 现行补充

### fine 状态与热路径

- 落点：canonical `<context_home>/state/<runtime>/fine/<sha256(session_id) 前 24 位>.json`，legacy `~/.claude/vault-loader-fine/`（与 topics 的 legacy 落点同一判定）。字段只有计数、序号、时间戳、相对路径与会话设置，**不存 prompt、K 轮文本与目录**（F16）。
- 读与写：`_fine_flow.begin_turn`（来源闸门之后、指代闸门之前）是对 fine 文件的**无锁读**（原子替换保证读到完整快照）；`_fine_flow.finish_turn`（`_finish_with_metrics` 里，emit 之后、metrics annotate 与 flush 之前）是每轮唯一的一次条件写，锁取 0.25 s。来源闸门（非用户来源、slash 跳过）早退的轮次不调这两个函数、不推进 `turn_seq`；有 mod 请求时 `_report_llm` 为快照另读一次（只读，闸门轮也读）。
- **写回锁超时不等于「本轮不投递」**（对 spec §7 / §13「锁超时 → 本轮不投递、不 spawn」的已裁定偏离）：读不受锁约束，本轮照常投递与 spawn；`finish_turn` 写失败时记 `degraded=lock`、不放过期的在飞标记（下一轮再读到再记）。这一轮的 `turn_seq` 没推进、补充队列也没清，所以**下一轮会把同一份补充再投一次**；写回持续失败就持续重投，直到某一轮写回成功（2026-10-17 探针：让 `update_fine` 连续两轮返回 None，同一篇在第 2、3、4 轮都被投递，第 4 轮写成功后队列清空）。`lock` 码能进本轮记录，是因为 `_finish_fine` 排在 metrics annotate 与 flush 之前。
- 条件写只清空本轮读到的那份补充（`gen_id` 相同才清）；子进程写补充只在选中了篇、且 `src_seq` 不小于现存那份时写（F06）。判 NONE 或选中 0 篇不写（批次 3c）：空补充会被下一轮计成「产生」，还会顶掉等放宽档的非空补充；统计照常进 `stats_q`。两边都在锁内对新读到的内容合并，不拿调用前的快照整份覆盖。
- 在飞标记 `<sid_h>.inflight` 与机器槽位 `fine/slots/` 都是 `O_EXCL` 独立文件，过期判据统一为 `started + timeout + PENDING_GRACE_SEC`，`_fine_state` 与 `_topic` 两处的 `PENDING_GRACE_SEC` 由对拍用例钉住一致；过期的在飞标记由每个通过来源闸门的轮次在 `begin_turn` 读到、`finish_turn` 记一次 `lost` 失败（写回失败则不记、也不放标记，下一轮再来）。
- 导入隔离：UPS 父进程里 `_metrics`、`_session_ops`、`_fine_flow`、`_fine_state`、`_fine_input` 各自单独兜底，加载失败只丢精排或快照、粗排照常。前提是 `prompt_submit_load` 的核心导入块（含模块级传递导入）不导入它们：`_topic` 只在函数内导入 `_fine_state` / `_fine_input`；`_fine_state` 的链接判据取 `_state.is_link_like`；`_fine_input.clean_text` 不再借 `_session_ops._INVISIBLE_RE`（逐码位枚举：它的 21 个码位全在 `_output._CF_RE` 里）。守卫 `test_import_isolation.py` 逐个拦截、从真实入口跑一轮，带阳性与空白对照（2.4.0 full-review A-H1）。
- 清理：SessionStart 删超过 `state_ttl_hours` 的 fine 文件、过期槽位与在飞标记，只认本插件的文件名、不跟随链接；`analyze_metrics --purge` 覆盖 fine 目录。`source` 为 `clear` / `compact` 时清空 `injected` 与 `supp`，并把会话开始注入的项目笔记写进 `injected`；`session_start.enabled=false` 时，在 opt_out 与项目 CLAUDE.md disable 两道停用闸门之后、`claim_event` 与旧插件暂停提示之前只做 clear / compact 的重置、不写路径（旧插件仍启用或 dry_run 时不写）。

### 子进程统计并账

- 外发字节只由父进程并账：子进程把每次调用（含失败的）的统计追加到 fine 状态的 `stats_q`（至多 5 条，不累加 `bytes_sent`），`finish_turn` 在锁内按 `gen_id` 出队并把其中的 `bytes` 并进 `bytes_sent`；`spawn_blocked` 把尚未并账的队列字节算进外发上限。
- `finish_turn(consume_stats=...)` 的取值是「metrics 未开，或本轮过闸」（`prompt_submit_load._Turn.past_gate`：只由 `mark_scored()` 在 `decide_injection` 之后置位，`_maybe_spawn` 在它为假时拒绝发起）。本轮状态都在每次 `main()` 新建的 `_Turn` 上，不用模块级全局（2.4.0 full-review Q-H1）。metrics 开着时，闸门轮的记录放不下统计，统计留队到下一个过闸轮；过闸轮一律消费，记录构造失败也一样（外发记账优先于 metrics 完整）。写回失败的那一轮统计没出队，也不计，留到下一轮。精排只在过闸轮发起，所以两次过闸之间 `stats_q` 至多 +1，留队触不到 5 条的截断，这个上界不依赖 metrics 是否健康。

### spawn 门禁与失败停用

- 门禁不读 topics entry 的 `state` / `fail`，也不再写 `pending` 占位，改读在飞标记、槽位与 fine 状态（F60）；`has_recent_topic_attempt` 退出门禁。
- 首轮成功判据单点 `_topic.first_round_done`（UPS 的发起模式与 `describe_session_topic` 的 `mode` 共用）：entry 在 TTL 内且 `state == "ok"`，或旧 entry（无 `state` 字段）有词。**不看有没有词或点名**：子进程在排除后无候选时不调用模型、写 `[]/[]/ok` 占住首轮结论，按此判也算首轮已完成，否则每轮重发直到调用上限（F35）。首轮没成功，下一轮继续走首轮点名模式。
- subagent 判据取 `agent_type` 的**真值**：Claude Code 2.1.296 的 UPS payload 恒带 `agent_type` / `agent_id` 键，主会话里值为 null（`claude -p` 探针实测）；子 agent 的提问不触发 UPS，后台子 agent 结束时的 `<task-notification>` 会触发，已由非用户来源闸门早退。
- 失败计数：`timeout` / `exit_nonzero` / `lost` / `error` 计入、成功清零，连续 2 次停用精排（`streak`）；`spawn_blocked` 不分首轮点名与逐轮补充，停用后两者都不再发起，30 分钟后或在面板里把精排 / 逐轮补充从关重新打开时恢复。`no_claude` 直接停用，`state_ttl_hours` 后重新探测一次；`call_cap`、`bytes_cap` 只在状态文件存续期内不恢复（TTL 清理或超过 256 KB 时计数归零），也不是硬上限：写回失败那次不计数，单次 prompt 不按余额截断。
- 交接：`spawn_fine` 把 payload 写进 `tempfile.TemporaryFile()`、回到开头，作子进程的 stdin，父进程在 `Popen` 返回后关掉自己的句柄；子进程照旧读到 EOF。不用「管道加同步写」：Windows 匿名管道缓冲 4096 B，越过它的每个发起轮 UPS 要在 emit 之前等子进程启动并开始读（实测约 92 ms）。写临时文件失败按 `spawn_error`，由 `_maybe_spawn` 回滚在飞标记与槽位。守卫：`test_session_topic.py` 的替身子进程用例（判别式）与 `test_perf.py` 的大 payload 稳态（预算）（2.4.0 full-review A-H2 / P-H1）。
- 子进程 argv 带 `--setting-sources ""`、`--strict-mcp-config`、`--disable-slash-commands`（D8；三种启动方式的实测记在给 `call_claude` 加这三个参数的那笔提交里，`git log --grep="三项隔离参数"`）。超时杀整棵进程树：Windows 用 Job Object，POSIX 用 `os.killpg`——经 npm 的 `.CMD` 垫片时真正干活的是孙进程（F69）。

### 候选、投递与复核

- 候选在子进程里构造（`_fine_input`）：排除集同时作用于摘要层与目录层；目录层超预算时先留宽召回 topical>0 的条目，其余按 mtime 丢旧（F63）；进 prompt 的文本单行净化、剥不可见字符、以 «» 包裹。
- 回显校验：编号后必须紧跟 «文件名» 回显，归一（NFKC + casefold，去 `.md`）后回显须是候选文件名的前缀（含整名，至少 4 个字符或整名），回显比文件名更长不算；缺回显也按不一致丢弃，计 `n_echo_mismatch`。
- 时效（D1 / D10）：补充只投递给 `turn_seq == src_seq + 1` 的那一轮；子进程写回晚于第 `src_seq + 1` 轮的开始时刻时放宽到 `src_seq + 2`；`turn_started` 缺该项时不放宽。投递轮另需本轮提问关键词（不含主题词）命中该篇 tag / 摘要 / keywords / 文件名之一。指代型与关键词不足的闸门轮不认领、不投递（`_fine_flow.release_supp_claim`）：补充留给下一个过闸轮照上述时效判，写回早于闸门轮开始的在那一轮按时效丢弃并计数，晚于的在第 `src_seq + 2` 轮走放宽档。
- 承接性提问（相对「用户最近几轮的提问」没有新话题）判 NONE 的规则只在补充模板 `_topic._SUPP_TPL` 里（批次 3c）：首轮点名模板与两者共用的 `_SEL_RULES` 不加（用户决定；评审 R1：首轮点名判 NONE 会让整个会话没有点名）；模型看不到助手的回答，判据只用本轮与最近几轮提问两栏。不加启发式的承接词表闸门（用户决定）。守卫在 `test_fine_select.py`。
- 补充只投递一次。主题词：首轮点名模式整体覆盖（词、点名与 `state=ok` 一起写）；补充模式成功调用刷新主题词，但解析出 0 个词时保留上一份，也不改首轮的 `state` / `fail`（F01）。

### 配置与会话设置

- 新旧键兼容的读取单点在 `_fine_config`：`fine_rank_on` 取「`fine_rank` 与 `session_topic` 任一为 false 即关」，`pin_max_raw` 取「新键、旧键里不等于默认值的那个，新键优先」；`load_config_ex` 合并后经 `resolve_superseded`（定义在 `_config_loader`，`_fine_config` 重导出）把有效值同时写回新旧两键。生产代码不得再直接读 `session_topic` / `session_pin_max`。
- 会话设置（面板的 8 个键，与 mod 的 `SETTING_ROWS` 逐一相同、同序，由对拍用例钉住）经 `apply_session_settings` 叠加在有效 config 上，之后整轮只读那份 config；config 里为 false 的 `fine_rank` / `fine_supplement` 在会话内不能打开（锁定）；「设为默认」只允许收窄（`check_persist`），放宽方向一律拒绝并提示经 `/config` 修改。
- 请求里有 `settings` **对象**（含 `{}`）才以它为准，`{}` 即清空本会话已存的设置；不带该键或值不是对象，沿用已存（`SessionOps.has_settings`；整份作废的请求也算「没带」）。

### mod 与 Python 之间的设置、「设为默认」与 Read 观测

- 「设为默认」（请求 `persist[]`）由 `prompt_submit_load._apply_persist` 经 `check_persist` 判后用 config skill 的 `set_key` 写回。mod 等回执才出队：快照的 `persisted`（面板键名的字符串列表）或 `rejected{what:"persist", key}`。配置读坏回落默认的那一轮、dry_run 轮，以及在精排接线之前就早退的轮次（停用闸门、来源闸门）既不执行也不拒收，mod 每轮继续重发（`set_key` 幂等）。
- 拒收值域：`rejected` 里 `what` 为 `settings` 的 reason ∈ `bad_item` / `over_cap`（结构）与 `unknown` / `bad_value` / `locked`（`apply_session_settings`）；`persist` 的 ∈ `bad_item` / `over_cap`（结构）、`bad_key` / `bad_value` / `widen`（`check_persist`）与 `unavailable` / `write_failed`（`_apply_persist`）。mod 按 `key` 把该键从本会话设置或「设为默认」队列删掉，不再重发；`over_cap` 的 `key` 为空。
- 主模型读过的 vault 笔记（请求 `reads[]`）：mod 送出时不清空，等到摘要与送出请求一致的 `scored` 快照才只移除送出的那几条；没有回执时跨轮累积，超过 50 条丢最旧。Python 只在完整记录（metrics 开）上落 `vault_reads`；过闸但记录构造失败的那一轮，那批读取会丢。
- mod 的 Read 观测：`tool.call{tool:'Read'}` 原样返回 `next(e)`，观测段包在 try/catch 里；**刻意不加 `.catch`**——`claude plugin validate` 会给「gating hook without .catch」的 advisory，但 d.ts 原文是 engine 事件上失败的 hook 被跳过、最后一次 `next` 结果成立，加返回 `{deny}` 的 `.catch` 反而有害。子 agent 的 Read（带 `agentId`）、被拒绝或出错的 Read（`isError` / `deny`）不计；是否在 vault 内靠快照的 `vault_root` 判（vault 可配到任意位置，按 home 拼不出来），所以第一份带 `vault_root` 的 UPS 快照之前的 Read 不计。

### 注入行

- 各处注入（SessionStart 项目笔记、粗排清单、首轮点名、手动钉住、精排补充）共用 `_inject_format.note_line`：`- 「标题」附注 — 摘要 ‖ 路径：<绝对路径>`。路径字段由插件生成、固定在行尾；标题与摘要里的 `‖` 一律替换为 `|`，所以一行里只有行尾那一个路径字段；路径解析失败或不安全时整段省略。`PIN_HEADER` 一字不改。记录与快照带 `fmt=2`，`inj_chars` 可按格式断代。

### metrics 记录口径

- gate 记录只有 5 个键、放不下补充计数，所以指代型与关键词不足的闸门轮不认领补充，由下一个过闸轮的完整记录计 `n_supp_gen` 与时效丢弃；索引为空的闸门轮仍会消耗到期补充、记不下，报表写明「时效丢弃不含索引为空的闸门轮」。2.4.0 开发期闸门轮投递补充的旧记录（`supp_only` 形态）由 `analyze_metrics` 按 `"fine_on" not in r` 认读，生产者已删除。
- `n_fine_done` 是并账的调用数、含失败的，失败数单独落 `n_fine_fail`；报表写「已并账调用 N 次（其中失败 M 次）」，NONE 率的分母是成功调用。
- 凡本轮没展示粗排清单（粗排关、`max_notes` 为 0、admitted 为空）都落 `shown=[]`；`sample_admitted` 有 `shown` 时按它抽样，旧记录（无该键）照旧，其中 `max_notes == 0` 不抽清单侧。

### 工作日志

- 工作日志不进粗排与精排候选（W1）：判据 `is_worklog`（路径首段为 `工作日志` 或 category 含它）在 vault-loader（`_candidate_pool`）与 summarize-session（`_summary`）各一份，由仓库根 `tests/test_worklog_parity.py` 对拍钉住一致（先例 `is_system_index_name`）。SessionStart 的近期日志清单走信号 F 直接扫目录，不受影响。

## 历史订正

- 「每个 TTL 窗口最多提炼一次」的承诺退役（F53）：它写在 `vault-loader.md` 迁移原文（`has_recent_topic_attempt` 的 docstring 承诺）与 vault-loader 的 SKILL.md 里。2.4.0 起首轮失败后下一轮继续走首轮模式，代价改由失败停用与每会话调用上限收敛。
- `vault-loader.md` 迁移原文会话 pin 一条里的 `_PENDING_PIN_ACCOUNT` 已收进 `prompt_submit_load._Turn.pending_pin`（Q-H1），延迟到 flush 成功后才翻的语义不变。
- 「失败计数」一条原写「`call_cap`、`bytes_cap` 本会话内不恢复」，2.4.0 full-review D-M4 订正。
- `mod.md` 现行补充里 topics `state` 一条原写「由占位与子进程显式写入」「spawn 门禁只看主题词与 `has_recent_topic_attempt`」，2.4.0 起改写；原文用 `git log -S"has_recent_topic_attempt" -- .claude/rules/mod.md` 取回。
- 「时效」「子进程统计并账」「metrics 记录口径」三条原写指代型与关键词不足的闸门轮只看时效投递补充、记录与快照为 `supp_only`（spec §4 ④ / Q30 / F22），2.4.0 批次 3c 改为闸门轮不认领、不投递；改动它们的那笔提交是 `git log -S"SUPP_ONLY_KEYS" -- .claude/rules/metrics.md` 列出的最新一笔，原写法在其父提交里。
