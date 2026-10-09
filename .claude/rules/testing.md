---
paths:
  - "tests/**/*"
  - "skills/*/tests/**/*"
---

# 规则正文：测试

> 本文件是 CLAUDE.md「硬约束索引 · 测试」的正文。「## 迁移原文」小节自 CLAUDE.md（基线 `3ac359f`，该提交只在开发仓库可取）迁出，除少量已失效的指代、路径与行号锚点外逐字保留；小节内「本文件」「本节」「此处」「上一版」凡指文档位置的，均指原 CLAUDE.md，出现的数字是当时的读数。此后新增内容写在「## 现行补充」里，按 CLAUDE.md「写入约定」执行。

## 现行补充

以下方法论提炼自原 CLAUDE.md 的测试计数史段落（计数本身按写入约定已删除）：

- **等价变异会按预期存活**：变异后的代码与原代码行为等价时（例如去掉 int 分支里对 `"true"` 的特判，而 `int("true")` 本来就抛 `ValueError` 并给出同样的「类型不符」），存活不说明守卫无判别力；要找真正承重的那条分支对它做变异。
- **默认命名空间陷阱**：`_state._RUNTIME` 模块级默认值是 `"legacy"`，凡不显式 `configure_context` 的用例全部跑在 legacy 分支，而真实用户绝大多数在 canonical——涉及落点的用例按 runtime 参数化。
- **撞名只在特定 cwd 出现**：「去掉 topics 路径段」只在 cwd 恰是项目根时才与注入去重文件撞名，拿子目录做断言永远看不到。
- **旧落点残留会假绿**：落点改了之后仍往旧落点写损坏数据的用例，要加前置探针区分「读到损坏数据」与「压根没读到」。
- **专为某条评审意见写的回归守卫也可能是空网**：夹具让被测分支根本走不到时（例如两个项目各自建了有效 `.git`，`_repo_root` 第一层就命中，走不到共享父目录那个无效标记），用例照样跑绿。
- **判据要能区分「正常裁剪」与「整份重置」**：只断言 `size` 与 `len(...) >= 1` 时，越限后被整份重置、文件反而变小的情形会双双通过；改成绝对下界（如 `>= 8`）才能区分。容量类守卫一律锁绝对下界，不写与其他常量的相对关系。
- **变异转红也要核对原因**：连 import 一起改掉的变异会以 `NameError` 转红，不合法；以得到预期的 `AssertionError` 为准。
- **替身要有接口覆盖守卫**：`_MetricsStub` 必须覆盖被调用的全部接口（`test_metrics_stub_covers_all_called_interfaces`，一写出来就抓到漏掉的 `nudge_due` / `mark_nudged`）。
- **手工构造被测对象的用例，都要补一条从真实产出喂入的端到端用例**（实例见 `metrics.md` 「`src` 字段实测恒为空」）。
- **测试不得继承 `GIT_*`**：四个 pytest 根的 conftest 在 `pytest_configure` 里剥离全部 `GIT_*`，匹配大小写不敏感（Windows 上环境变量名不分大小写）。实证（2026-10-08，2.0.0 验证）：`git rebase -x "<跑测试>"` 的 exec 子进程环境里有 `GIT_DIR=<worktree gitdir>`、`GIT_EDITOR`、`GIT_EXEC_PATH`、`GIT_PREFIX`、`GIT_SEQUENCE_EDITOR`；`tmp_git_repo` 等在临时目录执行的 `git init` / `git config` / `git commit` 遵从 `GIT_DIR`，结果把提交、分支、`core.bare=true`、`core.hookspath`、`user.*` 写进了共享 `.git/config`。被改写的 `core.hooksPath` 会让 pre-push 推送守卫静默失效。行为守卫是 `tests/test_git_env_isolation.py`：用子进程注入 `GIT_DIR`，断言受害仓库的 config 与 ref 不变。conftest 管不到的调用方（自写的 runner、`rebase -x` 的 exec 命令）同样要先剥离。疑似发生过时，核验命令是 `git config --local --show-origin --get-regexp "core\.(bare|hookspath)|user\."` 加上 `git worktree list`。

现状订正（「## 迁移原文」里以现在时写的两处已失效）：summarize-session 在 Windows 上已没有「环境性失败」，obsidian_cli 那批用例已按平台分支改写；`test_perf.py` 的判据已是 median of 7 加一次预热，不再是「3 次 spawn 取最差值」。

## 迁移原文（基线 `3ac359f`）

### HOME 隔离（原「Hook 管线」节）

- **测试必须无条件隔离 HOME，`conftest` 的 autouse fixture 负责这件事**：`state_path_for_cwd` 的两个分支（canonical `~/.context-vault/state/<runtime>/`、legacy `~/.claude/projects/<hash>/`）**都以 `Path.home()` 为根**，所以只复位 `_state._RUNTIME` 不解决问题——那只是换个子树写。实测一次全套跑的增量：只复位 `_RUNTIME` 是 canonical +0 / legacy **+36**，无条件隔离 HOME 才是两处都 +0。2026-09-10 曾因此在真实 `~/.context-vault/state/claude/` 留下 **252 个**测试文件（该目录真实数据只有 16 个），legacy 侧历史积累更达 2658 个、其中 topic 条目只有 18 条属真实会话。守卫是 `test_topic_namespace.py` 末尾**成对**的两条用例：前一条故意污染、后一条断言已复位——少了前一条，后一条在「从未有人污染过」时也恒真。

### 负载敏感用例与历史订正（原「开发与测试」节）

⚠️ **vault-loader 那个数字（原 CLAUDE.md 当时记录的通过数）是空闲态实测**：`tests/integration/test_perf.py` 两条与
`test_near_miss_nudge.py::test_concurrent_bumps_do_not_lose_updates` 都对机器负载
敏感，本机跑着二三十个并发进程时实测会红（同批 full-review 的两个 reviewer 各自
独立测得 858/1 与 857/2，均为这几条）。它们**单跑全绿**（含一次 45s 的 `test_relaxed_zero_candidates_stays_silent`，
失败形态是子进程 5s 超时而非断言不成立）；那条并发用例红的成因见 `vault-loader.md` 所记的
`lease_lock` 丢写。⚠️ 此处此前写「前两条与主题词改动**零代码路径交集**」，
被性能维核验**推翻**——`_config_loader.py` 的 `session_topic: True` 使 perf 用例
确实走该路径，只是该路径仅贡献 +0.09ms/轮（需解释量的 0.09%~1.8%），结论不变
但措辞是错的；真正的干扰源是 warmup 轮会真发 haiku。写这类数字时要带上测量条件，否则读者在
有负载的机器上复现不出来，会误判成回归。

> 上一版这三个数字（106 / 596 / 293）停在 2026-08-29，**过期了两轮**：`09ac5ed` 时已是
> 108 / 803，其后三笔各 +8 / +1 / +2。（原 CLAUDE.md 当时的）四个数字里只有 packaging 那个还成立。
> 这次不是「把红报成绿」（性质轻得多），但违反的是下面那条同一自定规则 —— 说明
> 光有规则不够，改动测试数量的提交必须顺手把这里改掉。
> ⚠️ **上一版把 summarize-session 那 10 条的根因写成「本机未装 obsidian-cli」，是错的**，
> 已于 1.1.0 查清并修复。实现本来就是跨平台的（有 tasklist / subprocess-list 分支），
> 失败的是**测试**：4 条 `test_shell_quote_*` 直接 `subprocess.run(["zsh", ...])`，Windows 无
> zsh ⇒ `FileNotFoundError`；6 条断言 POSIX 分支的返回值（`pgrep-missing`、`sh -lc`），而在
> Windows 上实现正确地走了另一支 ⇒ 恒红。**与装没装 obsidian-cli 无关**。
>
> 最值得记的是它的形态：同一个文件里就有 `test_no_hardcoded_zsh()`，守着「实现里
> 不得出现 zsh」——实现早已迁到 `sh`，而**测试自己留着 4 处硬编码 zsh**。守卫守了实现，
> 没守自己。
>
> 而上一版给出这个错误归因时，紧跟着写的正是「**判定「环境性」必须逐条拿到该条自己的
> 报错原文**——按文件名整批归类曾让一条真实缺陷被贴上环境噪声标签而无人再看」。
> 规则写对了，同一段里却按文件名整批归了类。**写下这条规则不构成遵守它。**

修法不是让那些用例跳过，而是让每条显式声明测哪个平台分支（monkeypatch `sys.platform`），
并补上此前一条都没有的 Windows 分支覆盖；shell 注入那 4 条改用 `sh` + 无 POSIX sh 时 skip
（Windows 上 `_cli` 走 list 形式根本不经 shell，那条路径上不存在 shell 注入面）。

> ⚠️ **上一版这行数字是不实的**：它写着三个套件全绿（`tests/` 71 passed、vault-loader 583 passed、
> summarize-session 290 passed），而当时实测是 3 failed / 6 failed / 11 failed —— 三组全部把红报成了绿。
> 那不是笔误，是「交付文档按 plan 预期书写完成态」：读者据此**跳过自己的验证**。
> 凡在本文件写下测试数字，必须回到实际执行记录，不得照 plan 顺写。

### 变异不转红的五种成因（出自 0.9.x 的一轮 full-review 整改）

**19 条核心守卫逐个变异验证全部转红**，其中 5 条第一次不转红、成因各不相同且**都不是「守卫无判别力」**——这五种成因值得单独记住，它们是「变异不转红」的完整故障谱：
1. **变异脚本的结果解析失效**：正则 `^FAILED` 撞上 pytest 的 ANSI 颜色码（行首是 `\x1b[31m`），恒解析不到，6 个变异全被读成「没转红」。差一点据此判定所有守卫无判别力并做一轮完全不必要的返工。修法：`--color=no` + 剥 ANSI + **用 returncode 与解析结果交叉校验**（rc≠0 却解析不出用例名 ⇒ 是解析器坏了）。
2. **用例绕过了被测的那一层**：用 `write_record` 手写记录验证「`build_record` 是否落盘某字段」，删掉落盘也不红。改从 `build_record` 真实产出喂入。
3. **对照组用了错误的历史形态**：验证「对照榜是否收敛到新记录」时用了 `_seed`（更早的形态，`near_miss` 连 `dedup` 键都没有），走 `"dedup" not in nm` 分支计入「未知(旧记录)」，**根本到不了被测的那一行**。换成 0.9.0 形态后转红。
4. **变异锚点写法不匹配**（预检拦下，报「命中 0 次」而不是静默跳过）。
5. **参数化用例名带 `[param]` 后缀**，精确匹配期望名认不出来。

> 前三种在结果上与「守卫无判别力」完全一样，唯一的区别是**你有没有做阳性对照**。判据：手工构造一个「确定该转红」的变异，若它也不红 ⇒ 是方法坏了，不是守卫坏了。

### 「环境性失败」归因与性能判据（历史订正，现状见「现行补充」）

注意（原 CLAUDE.md 当时记录的）是**可收集数、不是通过数**。本机（Windows）全量跑：`tests/` 与 vault-loader **全绿**；summarize-session 有 11 例环境性失败，**不要与回归混为一谈**：

- **环境相关的既有失败（改动前后一致，非回归）**：summarize-session `tests/test_obsidian_cli.py` **11 例**，根因是本机未安装 obsidian-cli（`FileNotFoundError` / CLI 不可用致返回错误 dict）。`tests/test_wrapper.py` 全量跑时偶发 `WinError 6/50` 句柄失效。
  > 此前本节记的是「obsidian_cli 9 例」，后改 10，现实测 11；判定「与本次改动无关」的硬证据是 `git status -- skills/summarize-session/` 为空（该目录零改动），而不是数字对得上。
  > ⚠️ **本节此前把 `test_archive_doc.py` 那 1 例一并归为「未安装 obsidian-cli」，是错的**。实测其根因是用例把写测试当天的日期钉死（断言 `vault_archived_at == '2026-05-28'`，而实现取 `datetime.date.today()`），自那天起每天必红，与 obsidian-cli 毫无关系。已在 `c6ef0a3` 修复（夹住调用前后当天日期 + 形态断言），summarize-session 当时为 11 failed / 269 passed / 2 skipped；2026-08-10 keywords 三防线后为 **11 failed / 287 passed / 2 skipped**（failed 数不变，全部集中在 `test_obsidian_cli.py`，逐条报错为 `FileNotFoundError: [WinError 2]` 或 Windows 路径分隔符断言）。
  > 教训与 perf 那条同构且更严重：**归因写错比数字记错有害得多**——数字对不上会被追查，而一条真实缺陷被贴上「环境噪声」标签后，就再没人看它了。凡把失败归为「环境性」，必须逐条拿到该条自己的报错原文，不能按文件名整批归类。
  > ⚠️ `test_wrapper.py` 此前还有一条被**误归为「环境性」**的常驻失败（「Git Bash `sh` 下 wrapper 探测到 Microsoft Store python stub」）。机制描述是对的，但归类误导——它是真实产品缺陷（OBS-7）在本机的显形：`run-hook.cmd` 的 sh 段探测顺序是 `python3 → python`，二者在本机都是 Store stub（`command -v` 成功但 `-c pass` 退出码 49），而唯一可用的 `py` **只在 batch 段探测、不在 sh 段**。补上 `py` 后该用例转绿。教训与 perf 那条同构：「环境性失败」这类排除性结论必须做根因分析才能下。
- **性能守卫 `tests/integration/test_perf.py::test_prompt_submit_under_300ms` 属另一类，历史上曾被误记为「既有失败」**：2026-08-04 基线对照实测（同 fixture、同 tmp HOME、交替 8 轮）`d9c964b` median p95 = 0.179s vs 决策面抽取后 0.207s，**median 与 min 同步位移 ⇒ 确定性回归而非噪声**，根因是渲染层重复计算 `_hit_keywords`（决策层结果被丢弃）。该回归已在 `63e784d` 修复——渲染层重复调用实测由 203 次降为 **0 次**。但本用例判据是「3 次 spawn 取最差值」，最差值由偶发解释器启动/调度尖峰主导，**本机仍会偶发越界**（尤其并发跑测试时）。故：**它偶发红不等于没回归，也不等于有回归——要判定性能问题必须做基线对照，不能靠单次观测下「既有失败」的结论。**

> ⚠️ 2026-08-05 补一条更细的：**反方向也会错**。P3 接线后连续 3 轮 pytest A/B 显示「NEW 全红、OLD 全绿」，据此下了「确定性回归」的结论——但那仍是**单轮采样**（每轮只跑一次，而该用例内部又只取 3 次的最差值）。改用不依赖该判据的独立测量（复刻其 tmp_home + 500 篇 fixture，median of 9，两轮交替）后结论反转：UPS 两边交错无系统性差异，SessionStart 侧 NEW 反而明显更快；单独跑 OLD 的 pytest 也复现了越界。**方差大的指标，「有回归」和「没回归」两个方向都需要 median of N，不能靠 pass/fail 计数。**

**判据已在后续一轮整改中改掉（L-10）**：上面两段描述的「3 次 spawn 取最差值」不再是当前实现——`test_perf.py` 现为 `SAMPLES = 7` 取 **median** + `WARMUP = 1` 次预热（fixture 刚写完 500 个 `.md`，紧接着的第一次调用读的是冷缓存，实测比稳定值高一倍以上，正是它把最差值顶过阈值的）。阈值 0.5s / 0.3s 未放松。两段历史教训仍然成立并保留：它们记录的是**方法论**，而那两次误判恰恰源自这个已被替换的判据。第三次实证在同一方向上——那一轮 opt_out 修复后 pytest 连续 5 轮 4 红，独立测量（median of 9、A/B 交替）却显示 NEW 比 OLD 更快，微基准进一步定量为 +0.98ms/次，不可能翻转 0.5s 阈值。

测试用 `monkeypatch` 隔离 HOME；Windows 上 `Path.home()` 取 `USERPROFILE` 而非 `HOME`，conftest 两者都 set（只 set HOME 在 Windows 无效）。
