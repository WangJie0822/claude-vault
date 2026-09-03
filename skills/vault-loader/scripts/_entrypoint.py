"""本插件只服务交互式人类会话；其余场景 hook 直接返回、不注入。

判据是环境变量 `CLAUDE_CODE_ENTRYPOINT`。2026-09-01 实测（CC 2.1.251，探针装在
生效的 plugin cache 上，各有同批阳性对照）：

| 场景                    | 是否触发 hook | CLAUDE_CODE_ENTRYPOINT |
|-------------------------|---------------|------------------------|
| 交互式终端会话          | 是            | `cli`                  |
| `claude -p`（含 clp 等）| 是            | `sdk-cli`              |
| Agent 工具 subagent     | **否**        | —                      |
| agent team teammate     | **否**        | —                      |

后两者根本不触发 UserPromptSubmit / SessionStart，本来就没有注入可禁；判据只需
拦住 `claude -p` 这一类。为什么值得拦：实测 sdk-cli 占打分轮次的 35.3%，每轮注入
中位 4238 字符（人类会话 971），吃掉约一半注入预算，而它带着完整任务 prompt 而来，
本机历史笔记不构成其上下文。

**为什么用环境变量而不是 transcript 里的 entrypoint**：transcript 每条 user
message 都带 `entrypoint`，看着正合适，实测却不可用 —— `claude -p` 那轮 user
message 的 timestamp 是 14:39:22.600、hook 落盘 14:39:23.428，而 transcript 文件的
**mtime 是 14:39:36.978**，文件在会话结束时才写。hook 触发时那条记录不在盘上，而
`claude -p` 又是单轮会话、没有前一轮可读，判据对最需要它的场景 100% 失效（端到端
实测：装上后 metrics 照旧新增）。环境变量零 IO、零时序依赖。

⚠️ 用「两个逻辑时间戳的先后」论证「文件当时已可读」是无效的，我正是这么错了一轮。

**变量缺失时按支持处理**：白名单本身是严格的（非 `cli` 一律不注入），但变量不存在
时不能跟着关掉——那会在 harness 改名或降级时静默停掉整个插件，而「静默失效」正是
本仓库反复吃过亏的失败模式。
"""

import os

ENTRYPOINT_ENV = "CLAUDE_CODE_ENTRYPOINT"

# 只有交互式终端会话享受注入。新增交互式形态（IDE 集成等）时在此登记。
INTERACTIVE_ENTRYPOINTS = ("cli",)


def is_supported_session() -> bool:
    """本次会话是否该被注入。"""
    ep = os.environ.get(ENTRYPOINT_ENV) or "cli"   # 缺失按交互式处理，见模块说明
    return ep in INTERACTIVE_ENTRYPOINTS
