"""vault-loader hook 统一 stdout 出口 + 用户可见摘要工具。

emit() 是两个 hook 唯一的 stdout 写出点：输出 JSON
（hookSpecificOutput.additionalContext 喂模型 + systemMessage 给用户看）。
"""
from __future__ import annotations

import json
import re
import sys

# 清洗可被终端解释的控制/转义字节，保留 \t \n \r
# 显示侧（systemMessage 终端渲染）：含 \x9b（C1 CSI，部分终端等价 ESC[ 触发转义序列）——
# 终端转义安全要求剥 C0+C1+DEL。FIX-6：与下方注入侧的范围差异是刻意的，见 _CTRL_CHARS_RE 注释。
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x9b]")


def sanitize_for_display(s: str) -> str:
    """清洗进入 systemMessage（终端可见）的文本，防终端转义注入。
    控制字符替换为 U+FFFD（对齐 spec §3.4），保留 \\t \\n \\r。"""
    return _CTRL_RE.sub("�", s)


# 注入侧（additionalContext 喂模型，不经终端渲染）：按 spec §4 只剥 C0+DEL，不含 C1（如 \x9b）——
# C1 不是注入向量（无终端渲染消费者）。FIX-6：刻意与上方 _CTRL_RE 不对齐，勿把这里的宽松范围
# 抄去显示侧（会重开终端转义漏洞），也勿把显示侧的 C1 剥离抄来这里（对喂模型内容无意义地更激进）。
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_injected_text(text: str, keep_newlines: bool = True) -> str:
    """净化进入 additionalContext 的笔记内容（F5）：剥控制字符（保 \\t）；
    keep_newlines=False 时换行折叠为空格（清单单行项，防 summary 伪造分隔符/新段落）。"""
    cleaned = _CTRL_CHARS_RE.sub("", text)
    if keep_newlines:
        return cleaned.replace("\r", "")
    # 折叠含 Unicode 行分隔符（U+2028/U+2029/U+0085）：三者语义即换行，
    # 未折叠会让伪造内容脱离清单单行项、破坏「单行清单项」保证（同 \r\n 风险）。
    return re.sub(r"[\r\n\u2028\u2029\x85]+", " ", cleaned)


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
    # 单出口兜底清洗：强制净化 systemMessage（防终端注入）。additional_context 由调用方
    # 在上游（build_injection_text_ss / prompt_submit_load 组装阶段）经 sanitize_injected_text
    # 净化后传入，emit 自身不二次处理。
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
