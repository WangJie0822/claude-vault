# Changelog

本文件记录 claude-vault 的用户可见变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.6.0] - 2026-08-05

本轮主线是**让静默失效变成可见**：知识库不工作时，此前几乎没有任何征兆——config 损坏、索引损坏、vault 路径写错、开关没生效，表现全都是「怎么没有注入了」。这个版本给这些情形补上了诊断通道与自检入口，并修掉了若干会让 hook 直接静默死亡的缺陷。

### ⚠️ 行为变更（升级前请读）

- **`opt_out_paths` 现在会真正生效，某些目录可能突然停止注入。**

  此前它用裸字符串前缀比对，于是同一个目录的多种合理写法只有一部分有效：正斜杠写法（`"D:/work/secret"`，而 JSON 里写反斜杠需要双写，用户自然会写正斜杠）、大小写不同、末尾多一个分隔符——**全都不生效**。如果你配过 opt-out 却发现该目录仍在注入，那不是错觉。

  现在改为路径归一化（`expanduser` + `resolve` + 大小写归一）加边界锚定。副作用是：**你此前"写了但没生效"的配置，升级后会开始生效**。如果某个项目升级后不再注入，先检查 `opt_out_paths` 是不是本来就想排除它。

  同一处修复还消除了反向的误伤：配置 `.../secret` 不再把兄弟目录 `.../secret-public` 一起拦掉。

- **`~/.claude/skills/vault-loader/config.json` 首次运行不再被全量物化。**

  旧版首跑会把整份 `DEFAULT_CONFIG` 写进 config，于是此后每次默认值演进都被这份快照压制、对存量用户无效。现在只写最小占位。**存量用户仍受旧残留影响**——用 `migrate_config.py` 检测与清理，见下方「新增」与 [docs/MIGRATION.md](docs/MIGRATION.md)。

### 新增

- **失效诊断通道**：config 损坏、vault 路径不存在、索引损坏、跨 skill 路径不一致等情形，现在会经 `systemMessage` 给出用户可见提示，而不只是写一行没人看的 stderr。按诊断类型分别做 TTL 冷却，不会每次提问都刷屏。
- **`migrate_config.py --doctor`**：只读健康自检，一次性列出 config 状态、vault 路径与可达性、索引状态与条目数、跨 skill 路径一致性、以及**旧版默认值残留计数与键名**。不写盘、不 dump 用户配置原文（输出可安全贴进 issue）。
- **索引状态区分**：内部把「空索引」的五种成因（不存在 / 空 / 版本不符 / 损坏 / 超大）分开，只有真失效才告警——新装用户和 cache 版本升级期间不会收到误报。
- **注入头只展示真正命中的查询词**：此前会把全量 prompt 关键词（含未命中任何展示笔记的碎片）塞进注入头。

### 修复

- **索引读取的五类畸形输入**（任一都可能让整份索引作废，其中两类会让 hook 静默死亡、stdout 全空、连诊断都不触发）：
  - JSON 根不是对象（`[]` / `null` / 字符串都是合法 JSON）
  - 深嵌套 JSON 触发 `RecursionError`（约 80KB 即可触发，远在 10MB 上限之内）
  - `summary` / `path` 无长度上限，单字段可逼近 10MB 进入模型上下文
  - `tags` 写成标量字符串时被逐字符迭代成假 tag，污染 tag-IDF 统计
  - 一条笔记的 `mtime` 非法会让**整份索引**判损坏——3 篇里坏 1 篇，存活 0 条
- **笔记标题可伪造终端界面**：摘要清单里的标题直接取自索引的 path 键（外部可控，Vault 可能是 clone 来的），未剥控制字符也未折叠换行，可在 `systemMessage` 里伪造出多行内容冒充 Claude Code 的告警并诱导执行命令。现已净化并截断。
- **全文注入的框架分隔符改用随机 nonce**：此前用字面量 `---` 框住不可信正文，笔记正文写 `---` 即可伪造「引用结束」。
- **笔记路径解析收敛到带容器校验的单点**，堵住索引中被篡改的越界 path 读到 Vault 外文件。
- `ensure_vault` 收敛，恢复 vault-loader 对 Vault 只读的不变量。
- 两个 hook 的输出收敛到唯一出口，杜绝一次执行写出两段 JSON 文档（那会让 Claude Code 解析失败并把原始 stdout 整个推进模型上下文）。
- 补上 hook 顶层 import 的 fail-open 缺口——此前导入期异常会直接 exit 1，兜底覆盖不到。

### 文档

- 订正多处失实表述，其中影响最大的一条：**改 Vault 路径必须改两处**。`/summarize-session --set-default` 只写 summarize-session 侧，vault-loader 读的是自己 config 里的 `vault_path`，不会跟着走——此前三份文档都写成「读端从写端取路径」，照做会让自动注入在一个空目录上静默工作。
- README 补充定向回退与收敛脚本之间的冲突提示（`scoring.prompt_keyword_hit: 3` 恰好等于历史默认，会被 `--apply` 当作残留清理）。

### 内部

- 测试从 366 增至 438（vault-loader），新增畸形输入、路径归一、标题伪造、诊断通道等守卫；性能守卫判据从「3 次取最差」改为「7 样本取中位数 + 预热」，消除尖峰主导导致的误判。

## [0.5.0] - 2026-07-22

- tag 命中改按 IDF 加权，`keywords` 命中权重从 3 提到 5。
- 写端补齐 keywords 覆盖，`rebuild_index` 统计覆盖率并在过低时告警。
- 安全：笔记路径解析收敛到带容器校验的单点。
- 存量用户升级须知与 `migrate_config.py` 收敛脚本，见 [docs/MIGRATION.md](docs/MIGRATION.md)。

## [0.4.0] - 2026-07-02

- 中文查询改用 CJK bigram 分词（无词典滑窗 + 函数词停用表），中文问句与短词的召回明显改善。
- 召回池默认排除 `tags` 含 `archived` 的笔记（`/vault` 手动检索不受影响）。
- 安全：清单模式补隔离声明，注入文本净化控制字符（含 Unicode 行分隔符折叠）。

## [0.3.0] - 2026-07-01

- 移除 auto-mode 整套，插件瘦身。
- 新增归档笔记清理（`prune_archived`）。

## [0.2.0] - 2026-06-27

- 笔记 frontmatter 支持 `keywords` 字段作为检索扩展词，参与读端打分。

## [0.1.1] - 2026-06-24

- 修复 SKILL.md 中指向已退役源目录的脚本路径，改用插件 cache 定位器。

## [0.1.0] - 2026-06-23

- 首个插件版本：summarize-session / vault-loader / vault 三个 skill 与 SessionStart、UserPromptSubmit 两个 hook 打包分发。
