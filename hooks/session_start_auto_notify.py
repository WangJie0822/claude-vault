#!/usr/bin/env python3
"""SessionStart hook：检查未审草稿和昨夜失败，有则输出提示（注入会话）。

行为等价复刻旧 session-start-auto-notify.sh：
- 调用 _count_drafts_and_failures.py 拿 JSON（INFO 为空/解析失败 → 静默 exit 0）
- drafts>0 → 打印草稿待审提示
- failures_yesterday>0 → 打印昨夜失败提示
- 始终 exit 0

golden：两条提示文案逐字节复刻旧 .sh 第 16/20 行（标点均为 ASCII 冒号/逗号）。
计数脚本路径经 CLAUDE_PLUGIN_ROOT 或 __file__ 相对解析；用 run_subprocess([sys.executable, COUNTER], ...) 调。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# H6：Windows python 默认编码可能非 utf-8，输出含中文/emoji，先 reconfigure
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# H7：兜住 helper import 期故障，避免拖垮会话
try:
    from _hook_common import fail_open, run_subprocess
except Exception:
    sys.exit(0)

# 计数脚本路径：优先 CLAUDE_PLUGIN_ROOT 环境变量，回退到相对 __file__ 定位
_PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parent.parent)
COUNTER = os.path.join(_PLUGIN_ROOT, "skills", "summarize-session", "scripts", "_count_drafts_and_failures.py")

# golden：逐字节复刻旧 session-start-auto-notify.sh 第 16/20 行（冒号/逗号为 ASCII）
_DRAFTS_MSG = "\U0001f4dd 自动总结草稿待审:{drafts} 项,运行 /summarize-session --review-drafts 查看"
_FAILS_MSG = "⚠️ 昨夜自动总结失败 {fails} 个会话,详见 {log_file}"


def run_counter():
    """调计数脚本，返回 stdout 字符串；任何失败返回 ''。"""
    try:
        cp = run_subprocess(
            [sys.executable, COUNTER],
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        return ""
    return cp.stdout or ""


def render(info_raw):
    """解析 INFO JSON，返回需打印的提示行列表。INFO 空/非法 → []（静默）。

    等价旧 .sh：drafts>0 一行、failures_yesterday>0 一行；都 0 / 空 → 无输出。
    """
    if not info_raw or not info_raw.strip():
        return []
    try:
        info = json.loads(info_raw)
    except Exception:
        return []
    if not isinstance(info, dict):
        return []
    lines = []
    # 复刻 shell 数值比较 `[ "$DRAFTS" -gt 0 ]`：仅当可转 int 且 >0
    drafts = info.get("drafts", 0)
    fails = info.get("failures_yesterday", 0)
    log_file = info.get("log_file", "")
    try:
        if int(drafts) > 0:
            lines.append(_DRAFTS_MSG.format(drafts=drafts))
    except (TypeError, ValueError):
        pass
    try:
        if int(fails) > 0:
            lines.append(_FAILS_MSG.format(fails=fails, log_file=log_file))
    except (TypeError, ValueError):
        pass
    return lines


def main():
    info_raw = run_counter()
    for line in render(info_raw):
        print(line)


if __name__ == "__main__":
    fail_open(main)
