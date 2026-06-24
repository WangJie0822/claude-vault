#!/usr/bin/env python3
"""SessionEnd hook 调用的入队脚本。

环境变量(测试时覆盖,生产用默认):
  AUTO_SKILL_ROOT    skill 根目录,默认 ~/.claude/skills/summarize-session
  AUTO_CLAUDE_DIR    Claude Code 数据目录,默认 ~/.claude

任何异常都静默退出 0,不阻塞 Claude Code 退出。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

from _auto_config import load_auto_config
from _auto_queue import enqueue, QueueEntry


def _skill_root() -> Path:
    return Path(os.environ.get(
        "AUTO_SKILL_ROOT", str(Path.home() / ".claude" / "skills" / "summarize-session")
    ))


def _claude_dir() -> Path:
    return Path(os.environ.get("AUTO_CLAUDE_DIR", str(Path.home() / ".claude")))


def _is_summarized(skill_root: Path, session_id: str) -> bool:
    f = skill_root / "summarized-sessions.json"
    if not f.exists():
        return False
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return session_id in set(data.get("sessions", []))
    except (json.JSONDecodeError, OSError):
        return False


def _find_session_jsonl(claude_dir: Path, session_id: str) -> Path | None:
    """在 ~/.claude/projects/*/<session_id>.jsonl 找会话文件。"""
    proj_root = claude_dir / "projects"
    if not proj_root.exists():
        return None
    for p in proj_root.glob(f"*/{session_id}.jsonl"):
        return p
    return None


def _scan_jsonl(jsonl_path: Path) -> dict | None:
    """计算会话指标。返回 None 表示文件无法解析。"""
    if not jsonl_path.exists():
        return None
    try:
        text = jsonl_path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return None

    msgs = 0
    has_edit_or_write = False
    has_bash = False
    timestamps = []
    first_user_text = ""

    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = d.get("timestamp")
        if ts:
            timestamps.append(ts)
        if d.get("type") in ("user", "assistant"):
            msgs += 1
        if d.get("type") == "user" and not first_user_text:
            content = d.get("message", {}).get("content", "")
            if isinstance(content, str):
                first_user_text = content[:200]
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        first_user_text = c.get("text", "")[:200]
                        break
        if d.get("type") == "assistant":
            content = d.get("message", {}).get("content", [])
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use":
                        name = c.get("name", "")
                        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                            has_edit_or_write = True
                        if name == "Bash":
                            has_bash = True

    duration_min = 0.0
    if len(timestamps) >= 2:
        try:
            t0 = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
            duration_min = (t1 - t0).total_seconds() / 60.0
        except ValueError:
            pass

    size_kb = jsonl_path.stat().st_size / 1024.0

    return {
        "total_messages": msgs,
        "size_kb": round(size_kb, 1),
        "duration_min": round(duration_min, 1),
        "has_edit_or_write": has_edit_or_write,
        "has_bash": has_bash,
        "first_intent": first_user_text,
    }


def _is_pure_command(first_intent: str) -> bool:
    """检测纯命令模式(单条 / 开头的 slash 命令,无后续上下文)。"""
    s = first_intent.strip()
    return s.startswith("/") and "\n" not in s and len(s) < 100


def main() -> int:
    parser = argparse.ArgumentParser(description="入队待自动总结的会话")
    parser.add_argument("--session", required=True, help="会话 ID(UUID)")
    parser.add_argument("--cwd", required=True, help="会话工作目录")
    args = parser.parse_args()

    try:
        return _do_enqueue(args.session, args.cwd)
    except Exception as e:
        # spec 要求:任何异常静默退出 0,不阻塞 Claude Code 退出
        print(f"enqueue: error (suppressed): {e}", file=sys.stderr)
        return 0


def _do_enqueue(session_id: str, cwd: str) -> int:
    skill_root = _skill_root()
    claude_dir = _claude_dir()

    # 暂停标志
    if (skill_root / ".auto-paused").exists():
        print("skipped: paused")
        return 0

    # 配置
    config_path = skill_root / "config.json"
    auto = load_auto_config(config_path)
    if not auto.enabled:
        print("skipped: auto disabled")
        return 0

    # 已总结
    if _is_summarized(skill_root, session_id):
        print("skipped: already summarized")
        return 0

    # 找会话 jsonl
    jsonl = _find_session_jsonl(claude_dir, session_id)
    if jsonl is None:
        print("skipped: session jsonl not found")
        return 0

    # 解析指标
    metrics = _scan_jsonl(jsonl)
    if metrics is None:
        print("skipped: session jsonl unreadable")
        return 0

    # 硬规则
    hr = auto.hard_rules
    if metrics["total_messages"] < hr.min_messages:
        print(f"skipped: hard_rule min_messages ({metrics['total_messages']}<{hr.min_messages})")
        return 0
    if metrics["size_kb"] < hr.min_size_kb:
        print(f"skipped: hard_rule min_size_kb ({metrics['size_kb']}<{hr.min_size_kb})")
        return 0
    if metrics["duration_min"] < hr.min_duration_min:
        print(f"skipped: hard_rule min_duration_min ({metrics['duration_min']}<{hr.min_duration_min})")
        return 0
    if hr.require_edit_or_write and not metrics["has_edit_or_write"]:
        print("skipped: hard_rule require_edit_or_write")
        return 0
    if _is_pure_command(metrics.get("first_intent", "")):
        print("skipped: hard_rule pure_command")
        return 0

    # 入队
    queue_path = skill_root / "auto-queue.jsonl"
    enqueue(queue_path, QueueEntry(
        session_id=session_id,
        cwd=cwd,
        enqueued_at=datetime.now(timezone.utc).astimezone().isoformat(),
        metrics={
            "total_messages": metrics["total_messages"],
            "size_kb": metrics["size_kb"],
            "duration_min": metrics["duration_min"],
            "has_edit_or_write": metrics["has_edit_or_write"],
        },
        failure_count=0,
    ))
    print(f"enqueued: {session_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
