"""诊断缓冲：把「用户必须知道的失效」从 stderr 升级为 systemMessage。

**为什么 stderr 不算「有输出」**：hook 的 stdout 进 transcript（可被 `/resume` 与事后
审计看到），stderr 不进。在 16 份真实顶层会话上验证过：注入正文（stdout）持久化命中
10+ 份、每份 2 处；而 `[vault-loader]` 的 stderr 前缀命中仅 2 处，逐条核验后均为 Read
工具回显的源码正文，**非 hook stderr**；`system` 记录子类型里也没有 UPS/SessionStart
的 hook summary 可承载它（`stop_hook_summary` 有 `hookErrors[]`，这两个事件无对应物）。
即：现有全部 `[vault-loader] …` 诊断写的都是没有读者的通道。

**三条硬约束**：

1. **顶层零 I/O、零副作用**。本模块被两个 hook 顶层 import，而顶层 import 在
   `if __name__ == "__main__"` 的兜底之外——导入期任何异常都会波及 fail-open。
   见 `tests/test_fail_open.py`。
2. **`notify()` 绝不写 stdout**。hook 的 stdout 契约是「一次进程执行产出**一个** JSON
   文档」；写两次会让 Claude Code 的 `JSON.parse` 失败、把整个原始 stdout 当 plainText
   推进模型上下文（连带 systemMessage 里未经注入侧净化的 vault 派生文本）。诊断只追加
   进缓冲，由 hook 出口处唯一一次 `emit` 带出。参见 `_output.emit` 的单次写出守卫。
3. **判据一律偏向沉默**。误报与漏报在此**不对称**：漏报的代价是维持现状；误报的代价是
   用户去排查一个不存在的问题 → 不信任插件 → 设 `VAULT_LOADER_DISABLE=1` 或写
   `~/.claude/.vault-loader-disabled`（**持久文件**）。而插件经 marketplace 分发、
   无遥测无反馈通道，作者收不到这个信号。所以拿不准时不要报。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import NamedTuple

from scripts._state import diag_cooldown_expired, save_diag_ts

# 失效等级。fatal＝召回这一轮完全没发生；degraded＝仍在工作但用户的配置/数据有问题。
LEVEL_FATAL = "fatal"
LEVEL_DEGRADED = "degraded"

# 稳定的诊断标识。用于按条冷却，**不要随文案改动**（改了等于重置所有用户的冷却）。
CODE_CONFIG_CORRUPT = "config_corrupt"
CODE_VAULT_UNREACHABLE = "vault_unreachable"
CODE_CACHE_BROKEN = "cache_broken"
CODE_VAULT_PATH_MISMATCH = "vault_path_mismatch"

# 外部可控文本的长度上限。诊断进 systemMessage，过长会淹没终端。
_MAX_FIELD = 200

# CR / LF / Unicode 行分隔符：三者语义都是换行。必须折叠——`sanitize_for_display`
# 按设计**保留** `\t` `\n` `\r`（见 `_output.py` 的 `_CTRL_RE` 注释），指望它净化是错的。
# 不折叠的话，`vault_path` 里塞几行就能伪造出多行终端 UI 冒充 Claude Code 自身的提示。
_NEWLINE_RE = re.compile(r"[\r\n\x85  ]+")


class Diagnosis(NamedTuple):
    """一条待告知用户的失效。

    `message` 说「发生了什么」，`hint` 说「怎么办」——两者都必须是用户**能据以行动**的。
    拿不准用户能做什么时，先别加这条诊断（见模块 docstring 第 3 条）。
    """
    code: str
    level: str
    message: str
    hint: str


# 进程级缓冲。hook 是一次性短进程，无并发写入。
_PENDING: list[Diagnosis] = []


def reset() -> None:
    """清空缓冲。生产上一个 hook 进程只跑一次，无需调用；供测试隔离用。"""
    _PENDING.clear()


def notify(diag: Diagnosis) -> None:
    """登记一条诊断。**只追加，零 I/O、不写 stdout**（见模块 docstring 第 2 条）。"""
    _PENDING.append(diag)


def pending() -> list[Diagnosis]:
    """当前缓冲（副本，供测试与调试查看）。"""
    return list(_PENDING)


def fold_home(value: object) -> str:
    """把本机 home 前缀折叠成 `~`。

    诊断进 systemMessage 就意味着进 transcript，而 transcript 会被分享、被审计。
    本仓另有一套发布前脱敏闸门专防私人路径外泄，运行时输出不该反向操作。
    """
    text = str(value)
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):
        return text
    if not home:
        return text
    if os.path.normcase(text).startswith(os.path.normcase(home)):
        return "~" + text[len(home):]
    return text


def safe_field(value: object, limit: int = _MAX_FIELD) -> str:
    """净化任何要拼进诊断文案的**外部可控**内容（路径、异常文本等）。

    顺序固定：折叠 home → 折叠换行 → 截断。换行折叠必须在截断之前，否则截断点
    之后的换行虽被丢弃，截断点之前的仍会漏网。
    """
    text = _NEWLINE_RE.sub(" ", fold_home(value)).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _render(diag: Diagnosis) -> str:
    icon = "⚠️" if diag.level == LEVEL_FATAL else "ℹ️"
    parts = [f"{icon} vault-loader：{diag.message}"]
    if diag.hint:
        parts.append(diag.hint)
    return "　".join(parts)


def take_user_visible(config: dict, cwd: Path) -> str:
    """取出缓冲里该让用户看见的诊断，渲染成文本；返回 "" 表示本轮不输出。

    **清空缓冲是无条件的**——即便因门禁或冷却而不输出，也不能留到下一次调用，
    否则会在意想不到的出口漏出来。

    门禁：
      - `display.user_visible: false` → 不输出。这是 SKILL.md 已发布的契约
        （「hook 将只输出 additionalContext 无 systemMessage」），不能因诊断而破例。
      - **不**受 `display.verbosity` 约束——那个控制的是注入摘要的详略，
        不是「要不要告诉你坏了」的开关。
      - 按 code 的 TTL 冷却：同一 cwd 同一 code 在窗口内最多出现一次。
        没有冷却的话 UserPromptSubmit 每条 prompt 都会重发。
    """
    diags = list(_PENDING)
    _PENDING.clear()
    if not diags:
        return ""

    display = config.get("display", {})
    if not isinstance(display, dict) or not display.get("user_visible", True):
        return ""

    ups = config.get("user_prompt_submit", {})
    ttl = ups.get("state_ttl_hours", 24) if isinstance(ups, dict) else 24
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        ttl = 24

    fresh: list[Diagnosis] = []
    seen: set[str] = set()
    for d in diags:
        if d.code in seen:
            continue
        seen.add(d.code)
        if diag_cooldown_expired(cwd, d.code, ttl):
            fresh.append(d)
    if not fresh:
        return ""

    save_diag_ts(cwd, [d.code for d in fresh])
    return "\n".join(_render(d) for d in fresh)


# ── 具体诊断的构造器（集中放置，便于统一措辞与复核判据） ──────────────────────

def config_corrupt(detail: str) -> Diagnosis:
    """config.json 存在但解析失败，整份回退默认值。

    **只用于解析失败**，不要用于「文件不存在」——那是零配置新装，是正常状态。
    """
    return Diagnosis(
        code=CODE_CONFIG_CORRUPT,
        level=LEVEL_FATAL,
        message=f"config.json 解析失败，本轮已整份回退默认值（{safe_field(detail)}）",
        hint="你的 vault_path、scoring 权重、relevance 阈值等自定义配置本轮全部未生效；"
             "修好 JSON 语法即可恢复。",
    )


def vault_unreachable(vault_path: object) -> Diagnosis:
    """配置的 vault 路径不存在。零配置默认路径会被自动创建，走不到这里。"""
    return Diagnosis(
        code=CODE_VAULT_UNREACHABLE,
        level=LEVEL_FATAL,
        message=f"vault 路径不存在：{safe_field(vault_path)}",
        hint="本轮未加载任何笔记。请确认该路径，或改 config.json 的 vault_path。",
    )


def cache_broken(status: object, vault_path: object) -> Diagnosis:
    """索引文件损坏或异常膨胀。

    **只用于 CORRUPT / OVERSIZE**。「文件不存在」「版本不符」「0 条目」都是健康态，
    对它们告警会命中每个新装用户、以及下次 CACHE_VERSION bump 后的全部存量用户。
    """
    return Diagnosis(
        code=CODE_CACHE_BROKEN,
        level=LEVEL_FATAL,
        message=f"知识库索引不可用（{safe_field(status)}）：{safe_field(vault_path)}/.meta/",
        hint="本轮未加载任何笔记。跑一次 /summarize-session 可重建索引。",
    )


def vault_path_mismatch(vl_path: object, ss_path: object, config_fell_back: bool) -> Diagnosis:
    """两个 skill 的 vault 路径不一致。

    `config_fell_back` 是必需的：config 回退时 `vl_path` 是**默认值**而非用户的真实配置，
    此时二者「不一致」是回退的结果、不是配置错误。若照旧建议用户运行
    `/summarize-session --set-default` 对齐，他会把写端指针也改到那个错误的默认路径上
    ——用写端配置变更去「修复」读端配置损坏，比不提示更糟。
    """
    if config_fell_back:
        return Diagnosis(
            code=CODE_VAULT_PATH_MISMATCH,
            level=LEVEL_DEGRADED,
            message="两个 skill 的 vault 路径当前不一致",
            hint="这是上面 config 回退导致的，**不要**运行 --set-default 去对齐；"
                 "先修好 config.json。",
        )
    return Diagnosis(
        code=CODE_VAULT_PATH_MISMATCH,
        level=LEVEL_DEGRADED,
        message=(
            f"两个 skill 的 vault 路径不一致："
            f"vault-loader={safe_field(vl_path, 80)} vs "
            f"summarize-session={safe_field(ss_path, 80)}"
        ),
        hint="写入与读取会落在不同目录。运行 /summarize-session --set-default 或手动对齐。",
    )
