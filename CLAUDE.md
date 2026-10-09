# CLAUDE.md

This file is the resident project guidance for Claude Code; detailed rule bodies live in `.claude/rules/` (see 写入约定).

## 写入约定（改本文件前先读）

本文件是 Claude Code 的常驻指令源，只写：架构与数据契约、每条硬约束的一行祈使句（带正文指针）、测试命令、本约定。

- **规则正文写入仓库根下 `.claude/rules/` 的对应文件**（见下表）。本文件只在「硬约束索引」对应分组（分发与发布类在「分发边界（重要）」节）加一行：`- <祈使句>。改 <源文件> 前先 Read .claude/rules/<rules 文件>.md`，同组末行已有统一指针时可省略本行指针。正文写理由、反例、实证经过（写明测量条件与版本）、历史订正；否定性决策（「刻意不做」「勿…」「不要…」）的结论句留在本文件。
- 子系统 → 正文文件（与 `.claude/rules/` 目录一一对应，由 `tests/test_claude_md_layout.py` 守卫）：

| 子系统 | 正文文件 |
|---|---|
| Hook 管线与 runtime | `.claude/rules/hooks-runtime.md` |
| vault-loader 注入、打分、主题词与配置表 | `.claude/rules/vault-loader.md` |
| metrics | `.claude/rules/metrics.md` |
| summarize-session | `.claude/rules/summarize-session.md` |
| 分发边界与发布 | `.claude/rules/distribution.md` |
| 测试方法 | `.claude/rules/testing.md` |

- **订正或退役**一条规则：同步改写或删除它的索引行，正文追加「历史订正」，不删原始实证。
- **新建 rules 文件**：`.claude/rules/` 只放一层小写 `.md`，frontmatter 只用下面这一种写法（YAML 写错会被 Claude Code 当成无 `paths`，每次会话无条件常驻）：

  ```
  ---
  paths:
    - "<glob>"
  ---
  ```

  glob 只用三种写法：目录 `<dir>/**/*`；嵌套单文件写完整相对路径；根级单文件 `/<文件名>`。禁止以 `/**` 结尾（单段 `X/**` 会被 Claude Code 2.1.283 剥成不锚定的 `X`），禁止单独的 `**`。`paths` 按「规则约束的符号定义在哪个文件」反推；每个被跟踪文件最多被 2 份 rules 覆盖；单份正文 ≤ 11,000、单次 Read 带回合计 ≤ 18,000 UTF-16 单元。文件头写明它是本文件哪一节的正文，在「硬约束索引」加 `### <分组>`，并在上表加一行。
- **新增源文件**：检查它是否应加进对应 rules 的 `paths`；不加就靠常驻索引行兜底。
- 以下**不属于项目规则**，不写入本文件与 `.claude/rules/`：① 测试套件的通过/失败/跳过计数与耗时这类随下次提交过期的读数——写进 commit message，附文件清单与可复跑命令；② 只与作者本机有关的操作步骤（`packaging/` 工具用法、推送与重做发布的命令）——写 Vault 笔记，作者另经 `/memory` 维护。例外：「分发边界（重要）」保留分支拓扑、推送策略、发布流程与门禁各一句（违反后果不可逆）；rules 里的「## 迁移原文」小节是基线的历史原文，不受这两条约束。
- 规则正文与索引行不得含本机绝对路径、主机名或远端 URL、会话 ID、IP、用户名或邮箱（随公开发布分发）；不得写出能生效的 vault-loader 注释（停用、标签、额外路径三种），举例时在关键字中插入零宽空格。
- 本文件体积上限由 `tests/test_claude_md_layout.py` 守卫，写入后跑 `python -m pytest tests/test_claude_md_layout.py -q`。超限时合并同组索引行，或把非索引内容挪进 rules；调整上限本身需用户确认。

## 项目概述

`context-vault`（仓库目录历史名 `claude-vault`）是一个跨平台（macOS / Linux / Windows）、面向 Claude Code 的插件（2.0.0 起不再支持 Codex），把四个 skill 与一组 hook 打包成「知识库沉淀—召回」闭环（其中三个构成闭环本体，第四个 `config` 是运维入口）。面向中文笔记工作流调优。

## 架构大图

三个 skill 协同形成闭环（必须放在一起理解，单读任一个看不出全貌）：

- **summarize-session**（写端，唯一写入方）——会话结束时把对话沉淀为 Obsidian 笔记 / 工作日志 / CLAUDE.md 更新，并维护 `<vault>/.meta/frontmatter-cache.json`。
- **vault-loader**（读端，只读）——通过 hook 把相关笔记自动注入会话上下文。
- **vault**（手动检索）——会话中按关键词 / 分类 / 标签调取笔记。

第四个 skill **config** 不在这个闭环里，是**运维入口**：把全部可配项连同「当前值 / 默认值 / 是否被显式设置 / 这一项干什么」列出来，并在确认后安全改写 `config.json`。读端**只读**（`load_config()` 在文件缺失时会写占位，读端因此只 `json.loads` 原始文件）；写端的安全边界**继承自 `migrate_config.py`**（重定向守卫、独立目录备份、原子写、restore），不重新发明。首版三处只读：`work_log_dir` / `obsidian_cli.*`（不在 `DEFAULT_CONFIG`、无类型判据，且后者只认 canonical 会与路径解析分叉）、三处逃生阀（用户开它们时往往正因插件出了问题，让插件自己去改失败方向不对称）。三个 `session_topic_*` 是**隐私旋钮**（控制每会话外发多少篇给外部模型），类型校验对它们全放行，故改动需显式确认。

**核心数据契约**：`<vault>/.meta/frontmatter-cache.json` 是写端（summarize-session 的 `rebuild_index.py`）与读端（vault-loader 的 `load_cache`）之间的唯一接口。读端 `load_cache` 校验 `_version`，不匹配返回空索引——改一端的 cache schema 必须同步另一端。笔记 frontmatter 的 `keywords`（检索扩展词）经此 cache 流到读端 scorer，是**可选增量字段**——新增它**不**触发 `_version` 变更（读端缺失默认空、双向兼容）。

**Vault 路径解析（1.0）**：canonical 单一来源是 `~/.context-vault/config.json::vault_path`，全新安装默认 `~/.context-vault/knowledge-vault`。canonical 不存在时继续兼容 0.9.x 的两份 `~/.claude/skills/.../config.json`；旧两值冲突时诊断/迁移必须 fail-closed，不猜哪一份正确。显式迁移只复制，不删除 legacy 数据。

## 硬约束索引

Read 到匹配文件时 rules 会自动注入；上下文里已出现该 rules 的内容就不要再 Read，否则同一正文进两次。用 shell 查看文件、新建文件、看 `git diff` 不会触发，这些情况下按每组末行的指针主动 Read（Claude Code 2.1.283 实测，判据见 `tests/test_claude_md_layout.py` 文件头）。

### Hook 管线与 runtime

- `hooks/hooks.json` 是 Claude Code 的薄声明，由 `hooks/run-hook.cmd` 路由；候选根固定为 `CLAUDE_PLUGIN_ROOT` → wrapper 相对路径，`PLUGIN_ROOT` 不是候选（2.0.0 起）。
- 守卫必须按 Claude Code 的方式执行声明中的完整 `command`（插值 `${CLAUDE_PLUGIN_ROOT}` 后交给 bash；Windows 上必须是 Git Bash，不能用 PATH 上的 WSL 启动器），核对 stdin 到达脚本，并覆盖「有该变量」「没有该变量」两种形态。没装 Git Bash 的 Windows 改由 PowerShell 执行，当前命令串在那里解析失败，守卫不覆盖。
- **所有 hook fail-open**：顶层 `try/except` 兜底 `exit 0`，任何 hook 都不得阻断会话。
- `run-hook.cmd` 是 polyglot（同一文件既是 batch 又是 POSIX sh），两种解释器都要能解析，保持 LF 行尾；wrapper 按 `py` → `python3` → `python` 探测，找不到即静默 `exit 0`。
- 宿主恒为 Claude（2.0.0 起没有宿主识别）；入口只对带 `hook_event_name` 的 payload 做事件去重，事件 marker 用 `O_EXCL`。state 与 metrics 按命名空间（legacy / claude）隔离，注入去重按 cwd，不按 session 切。
- runtime 命名空间是进程级全局：新起的子进程、同一进程里的另一个 hook 入口，都必须各自配置；`_state` 与 `_metrics` 是两个独立全局。子进程用 `current_runtime()` 交出、`adopt_runtime()` 接收，走 argv 不走 stdin；`{claude}` 白名单与 `_safe_component` 清洗必须原样保留（只剩一个取值也不得当死代码删掉）。SessionStart 入口里不要把 `_metrics` 的 import 提到与 `_state.configure_context()` 并列（UPS 入口有 `_MetricsStub` 兜底，可以并列）。
- `has_legacy_data()` 会被本类缺陷的副作用自举翻转：没有 committed 迁移标记的机器上，补配 `configure_context` 式的修复会静默空转。
- 「宿主拒绝写入」不得永久静默：判据单点 `context_vault/degrade.py::is_host_write_denied`，命中即登记 `degraded` 诊断、由 `take_user_visible` 冷却后说一次；`_metrics` 的 prune 失败刻意不升级为诊断。
- 改 `hooks/`、`context_vault/`、`_state.py`、`_metrics.py`、`_topic.py`、`_diagnostics.py` 或两个入口脚本前先 Read `.claude/rules/hooks-runtime.md`。

### vault-loader 注入、打分、主题词与配置表

- 注入正文恒带 `INJECTION_NOTICE` 隔离声明。SessionStart 注入确定性的项目固定上下文（不打分）；UserPromptSubmit 按 prompt 关键词打分取 Top N。信号 D 已无生产调用方，勿误以为生效。ASCII 关键词走词边界匹配、含 CJK 的走子串匹配；改 `scoring` 权重须同步调阈值。
- 会话主题词按项目根隔离（`_repo_root` 纯 stat 向上找，判据是「像不像 git 仓库」，不解析 `.git` 文件里的路径），与注入去重分属两个文件；`state_path_for_cwd` 勿顺手对齐成项目根。大小保证在 `TOPIC_TRIM_BYTES` 字节裁剪，守卫必须用宽字符，`_encoded_size` 连行尾一起对齐写端。不做全局单文件或按 session_id 存；不做「读不到就回退旧位置」。
- `lease_lock` 在 Windows 上遇 delete-pending 抛的 `PermissionError` 要重试，POSIX 上原样抛出；2s 租约在极端争用下仍可能 `TimeoutError`（未处理）。
- session pin：点名上限写读两侧共用单点 `_topic.effective_pin_max`；对账标记只在 metrics flush 成功后、且只在 admitted 非空的轮次翻；`_KIND_HIT` / `_KIND_BUCKET` 必须全覆盖 `KINDS`，别写 `else` 或 `not in (...)` 排除法；索引排除判据单点 `is_system_index_name`。
- slash 跳过闸门必须在信号 J 之前；命令名解析与剥离共用「到第一个空白为止」的切分口径；`<command-name>` 包裹形态用 `match` 锚定开头，不用 `search`；名单的类型归一不能省（配成字符串会逐字符迭代）；只对 UPS 收到 `/cmd` 原文的命令生效。
- 停用逃生阀有四种：环境变量、文件、config、项目 CLAUDE.md 注释。
- 配置表是机器契约：`skills/vault-loader/SKILL.md` 锚点内，每个 `DEFAULT_CONFIG` 叶子键必须有一行表格；锚点缺失时守卫抛错，不回落全文；废弃键走显式豁免清单 `_EXEMPT`。
- 改 `_signal_collect.py`、`_scorer.py`、`_decision.py`、`_state.py`、`_topic.py`、`_candidate_pool.py`、`context_vault/atomic.py`、配置项或配置表前先 Read `.claude/rules/vault-loader.md`；pin 与 slash 的接线在 `prompt_submit_load.py`，读它时自动加载的是 hooks-runtime 与 metrics，改那部分也要先 Read 本组正文。

### metrics

- metrics opt-in 默认关；每会话独立 `.jsonl`（Windows 多进程追加同一文件会丢记录）；prompt 关键词只存加盐 hash，不落 prompt 原文，`transcript_path` 刻意不落盘；`annotate` 在 `_PENDING is None` 时 no-op（opt-in 零足迹）。
- `analyze_metrics` 不读 config，生效配置随记录自描述落盘；顶层数值字段的坏值清洗只有一处（`_NUMERIC_TOP_FIELDS`），不要在消费侧就地写 `int(v or 0)`；读端用 `"inj_chars" in r` 判存在，不用 `.get(...,0)`；`n_topic_words` 刻意不记逐条 hit；gate 记录恰好 5 个键。
- near-miss 判据在生成侧、截断之前施加（`near_miss_scorelow`），三个消费者共用 `scorelow_entries()`；旧记录（无该键）一律不计，不做兼容过滤；提示冷却是全局的，不复用 per-cwd 诊断冷却。
- 两个榜单共用 `_shown_path` 一个路径展示出口，新增榜单不得直接拼 path；`retention_days` 只约束月份事件目录；`--purge` 无确认、立即不可逆。
- 标注按 `prompt_h` 精确匹配定位提问，不用时间戳就近匹配；标注落 `context_ids`（只落标识符）；web 入口的标识符只从服务端 items 查，请求体里带的一概忽略；精度标注的来源闸门是黑名单（`src` 实测恒为空），不是白名单。
- 改 `_metrics.py`、`analyze_metrics.py` 或入口脚本里的 metrics 接线前先 Read `.claude/rules/metrics.md`。

### summarize-session

skill 驱动（`SKILL.md` 即编排逻辑），辅以 `scripts/` 下脚本。模式：正常 / `-f`（强制，跳确认）/ `--catch-up` / `--quick`。Vault 内资源优先经 `scripts/obsidian_cli.py` 封装，Obsidian CLI 不可用时降级文件 I/O。

- 勿恢复 `git_commit_vault` 里的 `is_system_index` 放行分支（2026-09-07 已移除；它会在点目录排除之前抢先放行工具目录里的同名文件）；纳入判据是排除法、只看顶层段；只 commit 不 push。
- keywords 写期口径的唯一实现是 `_keywords.sanitize_keywords`；`keywords_gap.py --set` 必须同步 cache（秒级 mtime 判据会让 rebuild 跳过重读）；缺口判定复用 `rebuild_index._health_check`，不另写判据；`enrich_keywords.py` 付费、手动 opt-in，不接自动管线。
- 改 `skills/summarize-session/` 前先 Read `.claude/rules/summarize-session.md`。

### 测试

- 测试不得继承 `GIT_*`：四个 pytest 根的 conftest 在 `pytest_configure` 剥离（大小写不敏感），由 git 派生的子进程（如 `rebase -x`）里跑测试也要先剥离。
- 测试不得写真实 HOME：vault-loader 与 config 两个根由 conftest 的 autouse fixture 无条件隔离；`tests/` 与 summarize-session 根没有该 fixture，需在用例内 monkeypatch HOME 与 `USERPROFILE`（Windows 上 `Path.home()` 取后者）。
- 判定性能回归或「有无回归」须 median-of-N 加 A/B 交替，不靠单次 pass/fail；把失败归为「环境性」必须逐条拿到该条自己的报错原文；修法不是让用例跳过，而是显式声明测哪个平台分支。
- 变异不转红先查方法（结果解析、用例是否绕过被测层、对照组形态），再做阳性对照；变异转红也要核对原因（不合法的变异会以 `NameError` 假转红）；手工构造被测对象的用例，要补一条从真实产出喂入的端到端用例；容量类守卫要锁绝对下界。
- 写测试或排查失败前先 Read `.claude/rules/testing.md`。

## 分发边界（重要）

并非所有目录都随插件分发。**git 跟踪 = 分发源**：`.claude-plugin/`、`context_vault/`、`hooks/`、`skills/`、`commands/`、`scripts/`、`tests/`、`images/`、`docs/MIGRATION.md`、README、CLAUDE.md、`.claude/rules/*.md`（规则正文，仅一层）、CHANGELOG.md、VERSION。

**本地开发工具 / 设计文档，被 `.gitignore` 排除、不分发**：
- `packaging/` —— 作者私有发布工具：`build_plugin.py`（脱敏闸门，见 `.claude/rules/distribution.md`）、`import_assets.py`（从 `~/.claude` 源 allowlist 同步资产到插件目录）。
- `docs/superpowers/` —— spec / plan 设计文档（含私人引用，不能随 clone 泄露）。
- `.superpowers/` —— subagent-driven 开发的 task 简报。
- `.claude/`（`.claude/rules/*.md` 除外）—— git worktree 的物理目录（`EnterWorktree` 默认落这里）。里面是**另一份完整工作树**，误 `git add .claude/` 会把整个仓库副本连同其 `.full-review/`、`.superpowers/` 一起提交进来。
- 运行时产物：`config.json`、`*.jsonl`、`summarized-sessions.json`、`*.log`。

- **发布前脱敏闸门**：`python packaging/build_plugin.py`（仅作者主检出）按 `git ls-files` 扫描私人内容，按内容探测跳过二进制（不用后缀白名单）；新增分发文件前过一遍。它只扫工作树，不扫 git 历史与 commit message（开发谱系的历史 message 不改写）。改分发清单或发布相关文件前先 Read `.claude/rules/distribution.md`。
- **分发边界守卫**：`tests/test_no_corpus_in_repo.py` 用 allowlist 校验跟踪清单；改动分发边界时须同步它。`.claude/` 下只放行 `.claude/rules/*.md`。
- **守卫存在性门禁**：`tests/test_push_guard_installed.py` 在开发谱系检出中要求 pre-push 守卫已安装、形态正确（源一致性只在作者主检出核验）；守卫源码不能进跟踪树。

> **分支拓扑**：`dev` 是**实际工作分支**——其 git 历史含早期未脱敏内容（实测 178 行 / 22 路径），且**不只在 `docs/superpowers/`**，若干分发文件的历史版本里也有；工作树已清理，但 git 历史永久保留。`origin/master` 是不含开发历史的干净首版发布提交（孤儿提交），与 `dev` 谱系**无共同祖先**。本地 `master` 实测干净，与 `origin/master` 同源。

**推送策略（强制）**：私有开发远端承载完整开发历史、接受全部 ref；**公开仓库与内网镜像只接受发布分支 `master` 与 tag**。由 `.git/hooks/pre-push` 守卫，两道判据——ref 名 + 谱系（`git merge-base origin/master <sha>` 失败即属 `dev` 谱系）。后者是实质防线：把 `dev` 改名成 `master` 推送同样拦得住。守卫 **fail-closed**（与 hook 的 fail-open 相反）：`origin/master` 缺失即拒绝，绕过需 `--no-verify`。源副本在 `packaging/hooks/pre-push`（不分发，换机需重装）。

**发布流程（重要）**：对外发布为每版一笔 squash 提交——master 是独立谱系，该提交的树等于待发布那一版 dev 提交的树（重做已打 tag 的版本时钉死该版原有的树，不取当前 dev），父提交是原 master tip，另打注解标签 `v<版本>`。禁止把 `dev` 或其任何衍生分支推向公开或内网远端：脱敏闸门只扫工作树、不扫 git 历史。发布时同步 bump 三处版本号：`VERSION`、`.claude-plugin/plugin.json`、`.claude-plugin/marketplace.json` 的自身条目（守卫 `tests/test_plugin_version_sync.py`）；别靠记忆数这几个文件，跑门禁（`packaging/run_gates.py`，仅作者主检出）。

## 开发与测试

**五个 pytest 根（四个随仓库分发、一个作者专用），导入约定各不相同——从错误的 cwd 跑会因 import 失败（这是最容易踩的坑，它们不能用单一 rootdir 一起收集）**：

```bash
# 1. 插件打包 / hook / scripts 测试（仓库根，测试用绝对 ROOT 路径定位）
python -m pytest tests/

# 2. vault-loader（有自己的 pytest.ini；测试 import 形如 `from scripts._x`）
cd skills/vault-loader && python -m pytest
# 单测试： python -m pytest tests/test_scorer.py::<func>

# 3. summarize-session（无 pytest.ini；conftest.py 把 scripts/ 加进 sys.path；测试 import 裸 `from _x`）
cd skills/summarize-session && python -m pytest tests/

# 4. config skill（与 summarize-session 同款：conftest 把 scripts/ 加进 sys.path，
#    刻意不建 scripts/__init__.py —— 见该文件 docstring 里的顶层包名争用说明）
cd skills/config && python -m pytest tests/

# 5. 发布工具测试：脱敏闸门 + 推送守卫安装脚本（仅作者主 checkout 可用——
#    packaging/ 被 gitignore，不在 clone / worktree 中）
python -m pytest packaging/
```

测试方法（perf 判定、变异验证、「环境性失败」归因）见 `.claude/rules/testing.md`。本节不记录测试计数，见「写入约定」。

## 约束与约定

- 任何改动必须保持 **macOS / Linux / Windows 三平台兼容**（polyglot wrapper 适配三平台 shell）。
- hook 必须 fail-open，永不阻断会话。
- vault-loader 对 Vault 只读；summarize-session 是唯一写入方（且只追加/新建，不删除已有笔记）。
- git 跟踪文件中不写 Obsidian `[[...]]` wikilink（vault 是个人知识库层、不随仓库分发）。
- 文档以中文为主；但分发内容必须通过 `build_plugin.py` 脱敏扫描（零私人标识）。
- 分发的 SKILL.md / references 必须优先按当前加载的 `SKILL.md` 绝对路径定位同级 scripts；仅 Claude Bash 无法取得该路径时允许搜索 `~/.claude/plugins/cache/`，同时识别 `context-vault` 与旧 `claude-vault`。不得跨到 `~/.codex` 取脚本（那里只可能是停止更新的 1.x 副本），也不得使用 `~/.claude/skills/.../scripts/` 源目录形式。skill shell 不得假设注入 `CLAUDE_PLUGIN_ROOT`；PowerShell 必须使用当前 skill 的绝对 scripts 路径。
