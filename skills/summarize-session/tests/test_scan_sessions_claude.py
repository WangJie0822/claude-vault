"""scan_sessions 的 Claude 形态守卫（2.0.0 起只扫描 Claude Code 会话）。

由 test_codex_sessions.py 拆出：唯一测共用逻辑的「首个意图跳过注入前言」改写为
Claude 形态，其余三条随 Codex 解析器删除。本文件只直接调纯函数或起子进程，不在本进程
调 main()：summarize-session 根没有 autouse 的 HOME 隔离；且清单默认路径的分支在
pytest 进程里与生产不同（同批收集的测试可能已把 context_vault 导入进来）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scan_sessions import extract_first_user_intent

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scan_sessions.py"


def test_first_intent_skips_injected_preambles():
    messages = [
        {"role": "user", "text": "<system-reminder>\n环境信息……\n</system-reminder>"},
        {"role": "user", "text": "# CLAUDE.md instructions for /work/repo\n\n规则……"},
        {"role": "assistant", "text": "好的"},
        {"role": "user", "text": "修复召回失效的问题"},
    ]
    assert extract_first_user_intent(messages) == "修复召回失效的问题"


def test_agents_md_preamble_is_no_longer_special():
    """AGENTS.md 前言是 Codex 的注入形态，2.0.0 起不再特判。"""
    messages = [{"role": "user", "text": "# AGENTS.md instructions for /work/repo"}]
    assert extract_first_user_intent(messages).startswith("# AGENTS.md instructions for")


def _run(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONUTF8": "1"})
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=60)


def test_runtime_flag_is_a_hidden_noop(tmp_path):
    """旧版 SKILL.md 仍会传 `--runtime claude`：plugin cache 里多版本并存，而定位器
    `sort -V | tail -1` 只取最高版本的脚本。它必须继续被接受，但 `--help` 不再展示，
    `codex` 一律拒绝。"""
    claude_dir = tmp_path / "claude-home"
    (claude_dir / "projects").mkdir(parents=True)
    manifest = tmp_path / "manifest.json"
    ok = _run(tmp_path, "--runtime", "claude", "--manifest", str(manifest),
              "--claude-dir", str(claude_dir), "--days", "1")
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["runtime"] == "claude"

    help_text = _run(tmp_path, "--help").stdout
    assert "--runtime" not in help_text
    assert "--codex-dir" not in help_text

    bad = _run(tmp_path, "--runtime", "codex", "--manifest", str(manifest),
               "--claude-dir", str(claude_dir))
    assert bad.returncode == 2


def test_codex_dir_flag_is_removed(tmp_path):
    r = _run(tmp_path, "--codex-dir", str(tmp_path), "--manifest", str(tmp_path / "m.json"))
    assert r.returncode == 2
    assert "unrecognized arguments" in r.stderr


# 行形态取自 Claude Code 2.1.293 的真实 transcript（只取键与 content 类型，正文为虚构）：
# 开头是无 message 的元数据行，cwd 先出现在 attachment 行，user 有 str / isMeta 的
# list[text] / list[tool_result] 三种 content，assistant 是 thinking / text / tool_use 列表。
_SESSION_ROWS = [
    {"type": "mode", "mode": "default", "sessionId": "sess-1"},
    {"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}, "isSnapshotUpdate": False},
    {"type": "attachment", "cwd": "/work/repo", "sessionId": "sess-1",
     "timestamp": "2026-10-08T01:00:00.000Z", "attachment": {}},
    {"type": "user", "cwd": "/work/repo", "sessionId": "sess-1", "timestamp": "2026-10-08T01:00:01.000Z",
     "message": {"role": "user", "content": "<system-reminder>\n环境信息……\n</system-reminder>"}},
    {"type": "user", "cwd": "/work/repo", "sessionId": "sess-1", "isMeta": True,
     "timestamp": "2026-10-08T01:00:01.000Z",
     "message": {"role": "user", "content": [{"type": "text", "text": "<system-reminder>技能列表</system-reminder>"}]}},
    {"type": "user", "cwd": "/work/repo", "sessionId": "sess-1", "timestamp": "2026-10-08T01:00:02.000Z",
     "message": {"role": "user", "content": "修复召回失效的问题"}},
    {"type": "assistant", "cwd": "/work/repo", "sessionId": "sess-1", "timestamp": "2026-10-08T01:00:03.000Z",
     "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "先看日志"}]}},
    {"type": "assistant", "cwd": "/work/repo", "sessionId": "sess-1", "timestamp": "2026-10-08T01:00:04.000Z",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "先查一下召回日志"}]}},
    {"type": "assistant", "cwd": "/work/repo", "sessionId": "sess-1", "timestamp": "2026-10-08T01:00:05.000Z",
     "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                                     "input": {"file_path": "a.py"}}]}},
    {"type": "user", "cwd": "/work/repo", "sessionId": "sess-1", "timestamp": "2026-10-08T01:00:06.000Z",
     "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                               "content": "文件内容"}]}},
]


def test_scan_lists_and_parses_a_real_shape_claude_session(tmp_path):
    """full-review P3A-M5：d99dff0 改写了 main() 的 finder / parser_fn 分派，此前没有一条
    用例喂过真实形态的会话（其余用例只调纯函数、扫空目录或只验 argparse）。"""
    claude_dir = tmp_path / "claude-home"
    proj = claude_dir / "projects" / "D--work-repo"
    proj.mkdir(parents=True)
    (proj / "sess-1.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in _SESSION_ROWS) + "\n", encoding="utf-8")
    manifest = tmp_path / "m.json"
    common = ("--claude-dir", str(claude_dir), "--manifest", str(manifest))

    scan = _run(tmp_path, *common, "--days", "1", "--min-size", "0")
    assert scan.returncode == 0, scan.stderr
    out = json.loads(scan.stdout)
    assert out["total_sessions_in_range"] == 1
    assert [s["session_id"] for s in out["sessions"]] == ["sess-1"]
    assert out["sessions"][0]["first_intent"] == "修复召回失效的问题"
    assert out["sessions"][0]["cwd"] == "/work/repo"

    one = _run(tmp_path, *common, "--session", "sess-1")
    assert one.returncode == 0, one.stderr
    parsed = json.loads(one.stdout)["sessions"][0]
    assert parsed["first_intent"] == "修复召回失效的问题"
    assert parsed["cwd"] == "/work/repo"
    assert any(m["role"] == "assistant" and "召回日志" in m["text"] for m in parsed["conversation"])
