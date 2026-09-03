from __future__ import annotations

import json
from pathlib import Path

from scan_sessions import find_codex_session_files, parse_codex_session


def _write_rollout(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"timestamp": "2026-08-28T01:00:00Z", "type": "session_meta",
         "payload": {"id": "thread-1", "cwd": "/work/repo"}},
        {"timestamp": "2026-08-28T01:01:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "implement context vault"}]}},
        {"timestamp": "2026-08-28T01:02:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "done"}]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_codex_session_adapter_isolated_from_claude_shape(tmp_path):
    rollout = tmp_path / "sessions/2026/08/28/rollout-thread-1.jsonl"
    _write_rollout(rollout)
    sessions = find_codex_session_files(str(tmp_path), days=36500)
    assert [item["session_id"] for item in sessions] == ["thread-1"]
    parsed = parse_codex_session(str(rollout))
    assert parsed["experimental"] is True
    assert parsed["cwd"] == "/work/repo"
    assert [item["role"] for item in parsed["messages"]] == ["user", "assistant"]


def test_unknown_codex_shape_fails_closed(tmp_path):
    rollout = tmp_path / "sessions/rollout-unknown.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"different","payload":{}}\n', encoding="utf-8")
    assert find_codex_session_files(str(tmp_path), days=36500) == []


def _write_rollout_with_agents_preamble(path: Path) -> None:
    """复刻真实 Codex rollout：AGENTS.md 被注入成第一条 `role: user` 消息。

    本机 63 个真实 rollout 实测 **30 条（47.6%）** 是这个形态。原 fixture 只有一条
    干净的用户消息，够不着这个分支——这正是「手工构造被测对象、绕过真实形态」
    导致整层零覆盖的例子。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    preamble = ("# AGENTS.md instructions for /work/repo\n\n<INSTRUCTIONS>\n"
                "# AGENTS.md\n\nSee CLAUDE.md.\n</INSTRUCTIONS>")
    rows = [
        {"timestamp": "2026-08-28T01:00:00Z", "type": "session_meta",
         "payload": {"id": "thread-2", "cwd": "/work/repo"}},
        {"timestamp": "2026-08-28T01:01:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": preamble}]}},
        {"timestamp": "2026-08-28T01:02:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "修复召回失效的问题"}]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_first_intent_skips_injected_agents_md(tmp_path):
    """会话主题必须取真正的用户提问，不能取宿主注入的 AGENTS.md 前言。"""
    rollout = tmp_path / "sessions/2026/08/28/rollout-thread-2.jsonl"
    _write_rollout_with_agents_preamble(rollout)
    parsed = parse_codex_session(str(rollout))
    assert parsed["first_intent"] == "修复召回失效的问题", \
        f"取到的是注入前言而非真实提问：{parsed['first_intent']!r}"


def test_codex_date_matches_claude_format(tmp_path):
    """同一个 `date` 键在两个 runtime 下必须是同一种格式。

    Codex 侧此前直接存原始 ISO-8601（`2026-08-28T01:00:00Z`），而 Claude 侧是
    `%Y-%m-%d %H:%M`——下游按后者解析必然失败或时区错位。
    """
    import re

    rollout = tmp_path / "sessions/2026/08/28/rollout-thread-1.jsonl"
    _write_rollout(rollout)
    parsed = parse_codex_session(str(rollout))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", parsed["date"]), \
        f"date 格式与 Claude 侧不一致：{parsed['date']!r}"
