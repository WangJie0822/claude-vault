"""vault-loader hook 统一 stdout 出口 + 用户可见摘要工具。

emit() 是两个 hook 唯一的 stdout 写出点：输出 JSON
（hookSpecificOutput.additionalContext 喂模型 + systemMessage 给用户看）。
"""
from __future__ import annotations

import json
import re
import sys

# 清洗可被终端解释的控制/转义字节，保留 \t \n \r
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x9b]")


def sanitize_for_display(s: str) -> str:
    """清洗进入 systemMessage（终端可见）的文本，防终端转义注入。
    控制字符替换为 U+FFFD（对齐 spec §3.4），保留 \\t \\n \\r。"""
    return _CTRL_RE.sub("�", s)


def approx_size_str(text: str) -> str:
    """大致字数标记。"""
    n = len(text)
    if n < 1000:
        return f"~{n} 字"
    return f"~{n / 1000:.1f}k 字"


def emit(additional_context: str | None, system_message: str | None, event: str) -> None:
    """两个 hook 唯一的 stdout 写出点。

    - additional_context：喂模型，None/空则省略 hookSpecificOutput。
    - system_message：给用户看，None/空则省略 systemMessage。
    - 两者皆空 → 静默（不输出 {} 空壳）。
    - JSON 失败 → 降级回纯文本 additional_context，保模型侧不丢注入。
    """
    if not additional_context and not system_message:
        return
    # 单出口兜底清洗：强制净化 systemMessage（防终端注入），绝不碰 additional_context（喂模型逐字）
    if system_message:
        system_message = sanitize_for_display(system_message)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    payload: dict = {}
    if system_message:
        payload["systemMessage"] = system_message
    if additional_context:
        payload["hookSpecificOutput"] = {
            "hookEventName": event,
            "additionalContext": additional_context,
        }
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        if additional_context:
            sys.stdout.write(additional_context)
        print(f"[vault-loader] JSON 输出降级回纯文本：{exc}", file=sys.stderr)
