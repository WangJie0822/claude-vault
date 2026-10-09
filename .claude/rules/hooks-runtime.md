---
paths:
  - "hooks/**/*"
  - "context_vault/**/*"
  - "skills/vault-loader/scripts/_state.py"
  - "skills/vault-loader/scripts/_metrics.py"
  - "skills/vault-loader/scripts/_topic.py"
  - "skills/vault-loader/scripts/_diagnostics.py"
  - "skills/vault-loader/scripts/session_start_load.py"
  - "skills/vault-loader/scripts/prompt_submit_load.py"
  - "tests/test_hook_command_claude.py"
---

# 规则正文：Hook 管线与 runtime

> 本文件是 CLAUDE.md「硬约束索引 · Hook 管线与 runtime」的正文。「## 迁移原文」小节自 CLAUDE.md（基线 `3ac359f`，该提交只在开发仓库可取）迁出，除少量已失效的指代、路径与行号锚点外逐字保留；小节内「本文件」「本节」「此处」「上一版」凡指文档位置的，均指原 CLAUDE.md，出现的数字是当时的读数。此后新增内容写在「## 现行补充」里，按 CLAUDE.md「写入约定」执行。

## 迁移原文（基线 `3ac359f`）

### Hook 管线（`hooks/`）

- `hooks/hooks.json` 是 Claude Code/Codex 共用的薄声明，定义 SessionStart / UserPromptSubmit，两端都由 `hooks/run-hook.cmd` 路由。
- Codex 的 `commandWindows` **只做前缀归一化，不做择根**：把两个 root 环境变量里的 verbatim 前缀（`\\?\`、`\\?\UNC\`）各自归一化后写回（避免 batch 选回含该前缀的路径后与正斜杠脚本参数拼接失败），**互不覆盖**，随后按与 wrapper 相同的 `PLUGIN_ROOT` → `CLAUDE_PLUGIN_ROOT` 顺序定位 `run-hook.cmd`，把根的选择权交还给它；任何一步取不到就打印一行诊断并 `exit 0`。该字段**只被 codex.exe 消费**（实测 codex 0.153.0 命中 11 处、claude 2.1.259 命中 0 处，UTF-8 与 UTF-16LE 双编码 + 多重阳性对照），故它服务的恰是以 `PLUGIN_ROOT` 为 root 变量的那一端。
  > 2026-09-07 修掉的两个缺陷，改这里前务必先读：它曾无条件 `Get-Item Env:CLAUDE_PLUGIN_ROOT` —— 该变量缺失即抛异常、**rc=1 且 wrapper 从未被执行**，恰好在它唯一服务的 runtime 上 fail-closed，违反本文件「所有 hook fail-open」硬不变量；又用该值覆盖宿主给的 `PLUGIN_ROOT`，使 wrapper 的两个候选指向同一个根、`run-hook.cmd:12-13` 那套「非空**且**该根下脚本确实存在」的择根无从发生，于是 **rc=0 静默执行另一份插件副本**（双副本实测：`command` 路径走 A，`commandWindows` 路径走 B）。
  > Windows 守卫必须执行声明中的**完整命令**并核对 stdin 到达脚本；且必须覆盖「只有 `PLUGIN_ROOT`」「两者都无」「两者不一致」三种形态 —— 只测「两者都设且相同」会把上述缺陷全部漏掉（首版 8 组用例正是如此）。另：不要把整条声明再塞进一个 `powershell -Command` 里当作一种「形态」——它以 `powershell.exe` 开头，嵌套后外层会先把内层 `$var` 展开成空，给出与实现无关的假信号。
- plugin root 优先级固定为 `PLUGIN_ROOT`（Codex）→ `CLAUDE_PLUGIN_ROOT`（Claude Code）→ wrapper 相对路径；二者都指向插件 cache/加载根，不是用户态 skills 目录。
- runtime 适配集中在 `context_vault/`：payload 优先识别宿主（`turn_id`/`model` → Codex，`prompt_id`/`agent_type` → Claude，其后才看环境变量；两者都不给时**若 payload 带 `hook_event_name` 则判 Claude**——Codex 两个事件的 required 都含 `model`，而 Claude 的 SessionStart 实测只有 `cwd/hook_event_name/session_id/source/transcript_path`，且 `CLAUDE_PLUGIN_ROOT` **不在 hook 子进程环境里**，没有这条兜底它会落 UNKNOWN 并与同会话的 UPS 分裂到两个命名空间）。共享 config/Vault；**state 与 metrics 按 runtime 隔离，注入去重仍按 cwd**（不按 session：隔离要解决的是两个 runtime 互相踩踏，再切一层 session 会让跨会话去重失效、且 sessions 目录单调增长无清理）；事件 marker 用 `O_EXCL` 保证重复 hook 静默退出。

- **runtime 命名空间是进程级全局，凡新起的子进程都必须显式再配一次**：`_state._RUNTIME` 默认 `legacy`，detached 子进程**不继承**父进程的 `configure_context`。`spawn_topic_extraction` 因此把 `current_runtime()`——父进程**实际生效**的那个归一化值，不是按 payload/config 重新推导的——写进子进程 argv，`run_extraction_child` 用 **`adopt_runtime()`**（而非 `configure_context()`）据此配一次。**这两个函数的区别就是「单点」成不成立**：`current_runtime()` 交出的是**输出域**的值（已生效的命名空间），`configure_context()` 接受的是**输入域**的值（原始 runtime id），两个域取值字符串重叠、类型相同、语义不同，把前者喂回后者是一次**有损往返**——`claude` 是其中唯一的非不动点，而它恰好是绝大多数用户所在的那一档。父子之间 `use_canonical_namespace()` 一旦翻转（`has_legacy_data` 的三个探针里有两个是**其它组件**可能创建的路径）落点即分裂，且分裂后完全静默。`adopt_runtime()` 去掉的**只有**那次二次推导；`{claude, codex}` 白名单与 `_safe_component` 清洗必须原样保留——`_RUNTIME` 会被 `state_path_for_cwd` 拼进目录路径，放行任意字符串等于把它变成任意路径 JSON 写原语（安全评审已 PoC 复现 `..\..\..\PWNED` 写到预期范围之外）。**走 argv 不走 stdin**：后者是 best-effort 通道（写入异常被吞、且刻意不影响 spawn 的成功判定），把决定落点的参数放在那里，一次管道回收就让子进程静默回落 legacy。2026-09-09 实证漏配这一步的后果：`session_topic` 提炼 18/18 成功却全部写进 legacy，而 hook 从 canonical 读，功能默认开一周、每会话付一次 haiku，召回侧零收益且全程无声。守卫（`test_topic_namespace.py`）**必须真起子进程**——同进程用例继承父进程的 `_RUNTIME`、写读天然一致，这类分裂对它们**结构性不可见**，既有 7 条 wiring 用例全是同进程形态，缺陷因此带着全绿跑了一周。
  - ⚠️ **不止子进程——同一进程里的「另一个 hook 入口」同样要各配一次，而且 `_state` 与 `_metrics` 是两个彼此独立的全局**，配了一个不等于配了另一个。2026-09-16 实证：`session_start_load.py` 只调 `_state.configure_context`、漏调 `_metrics` 那个，`_metrics._RUNTIME` 停在默认 `legacy`，该入口 135 条 `session_start` 记录全部写进 `~/.claude/vault-loader-metrics/`，而 `analyze_metrics` 在 canonical 下读 `~/.context-vault/metrics/<runtime>/`（该 channel 计数恒为 0），canonical 侧的 `retention_days` 也永远清不到它 —— 全程零报错。识别信号有三个且互相独立：记录缺 `runtime` 字段（它只在 `_RUNTIME != "legacy"` 时写入）、两个入口的错误文案不一致（补 metrics 那次没同步过去）、canonical 侧某 channel 恒为 0。**守卫必须端到端跑真实入口**：既有 7 条 `test_session_start_metrics.py` 用例全部直接调 `build_session_start_record`/`summarize`/`render_report` 这类纯函数，对「`metrics_dir()` 用了哪个根」结构性不可见。
  - ⚠️ **补这行调用时不要把 `_metrics` 的 import 提到与 `_state.configure_context()` 并列的位置**：那是一处无条件导入，它一旦失败，同一 try 块里的 `_state` 配置会被一起跳过、把 state 也带进 legacy（已实测）。UPS 入口能把两者并列，是因为它在模块顶部有 `try: from scripts import _metrics` + `_MetricsStub` 替身兜底；SessionStart 是惰性 import、没有那层替身，位置不能照搬 —— 它的 `_m.configure_context` 应留在 metrics 自己的分支内，紧邻那处惰性 import。
  - ⚠️ **`has_legacy_data()` 会被本类缺陷的副作用自举翻转**：写 metrics 时会把 `~/.claude/vault-loader-metrics/` 创建出来，而该目录正是它的第三个探针 ⇒ `use_canonical_namespace()` 此后恒为 `False`，state/metrics/topics 一并留在 legacy。在**没有 committed 迁移标记**的机器上，任何「补配 configure_context」式的修复都会静默空转（已复现）。判断方法：`~/.context-vault/migration.json::status` 不是 `"committed"`、且那个目录存在，即处于该状态。
- **`run-hook.cmd` 是 polyglot 脚本**：同一文件既是合法的 Windows batch 又是合法的 POSIX sh（顶部 `: << 'BATCH'` heredoc 让 sh 跳过 batch 段）。单文件而非 `.cmd`+`.sh` 两份，是因为 Claude Code 在 Windows 上对含 `.sh` 的命令会前置 bash，导致双文件 wrapper 失效。改这个文件务必保持两种解释器都能正确解析，并保持 LF 行尾（`.gitattributes` 对 `*.sh`/`*.cmd` 强制 `eol=lf`，CRLF 会破坏 shebang / heredoc）。
- wrapper 按 `py` → `python3` → `python` 顺序探测解释器；找不到任何 Python 即静默 `exit 0`。
- **所有 hook fail-open**：脚本顶层 `try/except` 兜底 `exit 0`。任何 hook 都不得阻断会话。新增 hook 逻辑时保持这一不变量。
- **「宿主拒绝写入」不等于「无害」，不得永久静默**。判据单点在 `context_vault/degrade.py::is_host_write_denied`（认 `PermissionError`，以及 `OSError` 的 EACCES/EPERM/**EROFS** —— 只读*文件系统*在 POSIX 上不被映射成 `PermissionError`）。命中后登记一条 `degraded` 诊断（`CODE_HOST_WRITE_DENIED`），由 `take_user_visible` 按 code+cwd 做 TTL 冷却「说一次」，**而不是** `return` 掉：Windows 上「文件被占用」（WinError 5/32，本机 Obsidian 与安全代理属常态，`summarize-session/scripts/_fs.py` 正为此做指数退避重试）抛的同样是 `PermissionError`，与「用户目录权限真的配坏了」在异常类型上不可区分，无条件静默会把后者一并吞掉。既然无法只报真问题，就让 hint 把「哪种情况无需处理」写死 —— 这是 `_diagnostics` 模块「判据偏向沉默」那条约束下的取舍，不是对它的违反。
  该判据 2026-09-07 之前在**三处**各写一份（两个入口脚本的 `_report_nonfatal` + `_metrics.flush()` 内联），改一处漏两处、且任何一处漏改都不会让测试转红。`_metrics` 的 prune 失败**刻意不**升级为诊断（清理失败无后续影响，且该模块每次 UPS 无条件 import，不引入诊断层依赖）。守卫是 `test_readonly_sandbox.py`，它用 `ast` 钉住**接线**而非函数本体 —— 首版只调本体，把 7 个真实调用点全部还原成裸 `print` 后整套仍然全绿。

## 历史订正（2.0.0 移除 Codex 支持）

- 上文「迁移原文」里关于 Codex 的条目（`hooks.json` 两端共用、`commandWindows`、`PLUGIN_ROOT` 优先、按 payload 识别宿主）随 2.0.0 退役：`hooks.json` 只剩 Claude Code 的 `command`，wrapper 候选根为 `CLAUDE_PLUGIN_ROOT` → 自身位置，宿主恒为 claude，白名单收为 `{claude}`（`_safe_component` 与「必须保留」不变）。
- 退役的直接动因之一：1.x 的宿主识别在 Claude Code 2.1.293 上把部分 SessionStart 判成 Codex，作者本机 `metrics/codex/` 185 条记录中 183 条是 session_start，涉及 156 个会话，其中 117 个能在本机找到 Claude Code transcript；`claude -p` 下两次探针未复现，交互与后台模式的 payload 未取到，成因未坐实。
- Windows 守卫随之改写（`tests/test_hook_command_claude.py`）：按 Claude Code 的方式把插值后的完整 `command` 交给 Git Bash 执行，核对 stdin 到达脚本，覆盖「有 / 没有 `CLAUDE_PLUGIN_ROOT`」两种形态。上文「只有 `PLUGIN_ROOT` / 两者都无 / 两者不一致」三种形态里有两种依赖 `PLUGIN_ROOT`，随它一起退役。依据的实测（2026-10-08，Git for Windows 2.55.0，GNU bash 5.3.15）：Git Bash 把 `.cmd` 交给 cmd.exe 执行，polyglot 探针只输出 batch 段的标记，所以 Windows 上覆盖的是 batch 段、macOS / Linux 上覆盖的是 sh 段。守卫取 bash 必须从 git 的安装位置推出 Git Bash，不能取 PATH 上第一个 bash：Windows 常规终端里那是 System32 的 WSL 启动器（同日实测：用它执行插值后的命令串 rc=127、`No such file or directory`），整组用例会误报红。没装 Git Bash 的 Windows 上 Claude Code 改用 PowerShell 执行同一条命令串，当前命令串在那里解析失败、脚本没被执行（1.x 起既有，不是 2.0.0 引入），守卫不覆盖这一形态。
- 订正一处实测：上文「`CLAUDE_PLUGIN_ROOT` 不在 hook 子进程环境里」未标注测量版本；Claude Code 2.1.293 的 `claude -p` 探针实测 hook 子进程环境里有该变量。wrapper 仍保留「取不到就回落自身位置」。
