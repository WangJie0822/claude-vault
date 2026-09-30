---
paths:
  - ".claude-plugin/**/*"
  - ".codex-plugin/**/*"
  - "scripts/**/*"
  - "/.gitignore"
  - "/.gitattributes"
  - "/VERSION"
  - "/CHANGELOG.md"
  - "/AGENTS.md"
  - "tests/test_no_corpus_in_repo.py"
  - "tests/test_push_guard_installed.py"
  - "tests/test_plugin_version_sync.py"
  - "tests/test_build_codex_artifact.py"
---

# 规则正文：分发边界（重要）

> 本文件是 CLAUDE.md「分发边界（重要）」的正文。「## 迁移原文」小节自 CLAUDE.md（基线 `3ac359f`，该提交只在开发仓库可取）迁出，除少量已失效的指代、路径与行号锚点外逐字保留；小节内「本文件」「本节」「此处」「上一版」凡指文档位置的，均指原 CLAUDE.md，出现的数字是当时的读数。此后新增内容写在「## 现行补充」里，按 CLAUDE.md「写入约定」执行。

## 现行补充

- 发布流程的理由（原 CLAUDE.md「发布流程」；发布形态已于 2026-09-29 改为每版一笔 squash）：clone 后可经 `git show <旧commit>:<file>` 取回历史内容，脱敏闸门拦不住，只有推送策略能；四处版本号同步是为了让 `/plugin update` 按版本识别更新，1.2.1 发布时曾漏改 `.codex-plugin/plugin.json`，由 `tests/test_plugin_version_sync.py::test_all_manifests_follow_root_version` 拦下。多笔提交形态下中间提交的树不经过脱敏闸门，这是改为每版一笔 squash 的理由之一。

## 迁移原文（基线 `3ac359f`）

### 分发边界的实证与原理（原「分发边界（重要）」节）

**发布前脱敏闸门**：`python packaging/build_plugin.py` 扫描私人内容正则（作者标识、私有 IP、真实路径、session UUID 等），命中即 `exit 1`。**遍历源是 `git ls-files -z`（不是文件系统 `rglob`）**——与「git 跟踪 = 分发」判据同源；`-z` 是必需的，`core.quotepath` 默认 `true` 会把非 ASCII 路径转义成八进制、令匹配整类失效。`SKIP_PREFIXES = ("packaging/", "docs/superpowers/")` 按**路径前缀**排除自指误报（早期按「任意路径段」匹配 `docs`，导致唯一分发的 `docs/MIGRATION.md` 从未被扫过）；二进制按内容探测跳过，不用后缀白名单（否则新增文件类型 = 新增盲区）。新增分发文件前过一遍这个扫描。

> **闸门自身的失效模式（DO-C1，已修）**：扫描根取自 `Path(__file__).parent.parent`，**与调用时 cwd 无关**。按绝对路径调用另一棵树里的本脚本，扫的是脚本那棵树；若那棵树不含待发布内容，闸门照样打印 `secret scan clean` + `exit 0`——**与真正通过完全无法区分**。实测修复前：cwd 在另一棵仓库时 `exit=0 / secret scan clean`。现在闸门会 ① 无条件自述「扫描根 / 论域 / 候选与实读文件数」；② cwd 所属工作树 ≠ 扫描根时 `exit 2` 硬失败（逃生阀 `--root <path>`）；③ 实读 0 个文件时 `exit 2`——空树上的 clean 是最典型的假通过。

**分发边界守卫**：`tests/test_no_corpus_in_repo.py` 用 allowlist 校验——断言 `git ls-files` 每一项都落在 CLAUDE.md「分发边界（重要）」首段的分发清单内，任何新目录误入即红（早期是只查 `docs/superpowers/` 前缀与 `.jsonl` 后缀的 denylist，覆盖不到 `packaging/`、`.superpowers/`、运行时产物等）。改动分发边界时须同步该守卫的 allowlist。

> **推送守卫（CLAUDE.md「分发边界（重要）」推送策略所述的 pre-push）的存在性也有门禁了（DO-H1，已修）**：git hook **从不经 git 协议传输**，源副本又不分发，所以「这次 clone 从来没装过守卫」此前是**完全静默**的状态。现分两层：
> - **机制**（不分发，随发布工具留本机）：`python packaging/install_hooks.py` 安装/更新，`--check` 只校验。二进制写入——文本模式在 Windows 会把 `#!/bin/sh` 的 `\n` 变成 `\r\n`，shebang 尾带 `\r` 令 POSIX shell 解析失败，即「装了但跑不起来」。POSIX 上自动补执行位（缺执行位时 git **静默跳过** hook）。目标位置已有来源不明的同名 hook 时拒绝覆盖，需 `--force`。
> - **门禁**（随仓库分发）：`tests/test_push_guard_installed.py`。仅当本地存在与 `origin/master` 无共同祖先的分支（即开发谱系）时才要求守卫——发布 clone 只有 master 谱系 ⇒ 自动跳过，不误伤安装者。除存在性外还钉**形态**（内容须含 `origin/master` 与 `merge-base` 两个判据关键字，空壳/占位/被别的 pre-push 覆盖都拦得住）与**源一致性**（作者检出里已装副本须与源逐字节相等）。
>
> 守卫源码本身**不能进跟踪树**：它按 remote URL 判定放行，正文里写着内网远端域名与私有自建域名，而内网域名恰是 `SECRET_PATTERNS` 的拦截项之一——一旦跟踪，脱敏闸门立刻红。安装脚本因依赖该源副本同理留在 `packaging/`。所以落地的是「没装就红」，不是「自动装」。
>
> ⚠️ 由此牵出一条闸门盲区：脱敏闸门只扫工作树文件，**不扫 commit message**。写 message 时若引用了私人标识（比如解释「守卫为什么不能跟踪」时把那个域名原样抄进去），闸门不会有任何反应，而发布提交会把 message 一并带上公开远端。实测本仓库开发谱系里确有多笔 message 命中 `SECRET_PATTERNS`。
>
> 补丁是 `packaging/scan_commit_messages.py`（不分发，已接进 `run_gates.py`）。默认范围 `origin/master..HEAD`：在发布分支上这正是本次要发布的全部提交；在开发谱系上与 `origin/master` 无共同祖先、没有发布语义，**自动跳过并说明**——若在日常分支恒红，这条检查很快会被无视，等于没有。**开发谱系的历史 message 不改写**：那些提交本就不推公开远端（pre-push 守卫保证），为此重写历史的风险大于收益。
