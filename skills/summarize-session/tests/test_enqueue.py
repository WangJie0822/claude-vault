from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

ENQUEUE = Path(__file__).resolve().parent.parent / "scripts" / "enqueue_auto_summary.py"


def _make_jsonl(path: Path, *, msgs: int, has_edit_or_write: bool,
                duration_min: float, first_user_text: str = "hello") -> None:
    """生成模拟 Claude Code 会话 JSONL 文件(只关心字段够用即可)。"""
    base = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
    lines = []
    for i in range(msgs):
        ts = (base + timedelta(minutes=i * (duration_min / max(msgs - 1, 1)))).isoformat()
        if i == 0:
            entry = {
                "type": "user", "timestamp": ts,
                "message": {"role": "user", "content": first_user_text},
            }
        elif i % 2 == 1:
            content = []
            if has_edit_or_write and i == 1:
                content.append({
                    "type": "tool_use", "name": "Edit",
                    "input": {"file_path": "/tmp/x.py"}
                })
            else:
                content.append({"type": "text", "text": f"resp {i}"})
            entry = {
                "type": "assistant", "timestamp": ts,
                "message": {"role": "assistant", "content": content},
            }
        else:
            entry = {
                "type": "user", "timestamp": ts,
                "message": {"role": "user", "content": f"u {i}"},
            }
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _setup_env(tmp_path: Path, *, auto_enabled: bool = True,
               paused: bool = False, summarized_sessions: list[str] | None = None,
               hard_rules_overrides: dict | None = None) -> dict[str, str]:
    """构造一个完整的隔离环境,返回需要的 env 变量。

    NOTE: 默认把 min_size_kb 拉到 0,以便测试构造的小型 jsonl(~2.6KB) 能通过 size_kb
    检查,从而走到后续硬规则。调用方 override 时若需校验 size_kb 限制,可显式传入。
    """
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    # 默认 hard_rules:把 size_kb 拉到 0,让小型 jsonl 能通过 size 检查走到后续规则
    hard_rules: dict = {"min_size_kb": 0}
    if hard_rules_overrides:
        hard_rules.update(hard_rules_overrides)
    config = {"auto": {"enabled": auto_enabled, "dry_run": True,
                       "hard_rules": hard_rules}}
    (skill_root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if paused:
        (skill_root / ".auto-paused").touch()
    summarized = {"sessions": summarized_sessions or [], "updated": "2026-04-23T00:00:00"}
    (skill_root / "summarized-sessions.json").write_text(json.dumps(summarized), encoding="utf-8")

    claude_dir = tmp_path / "claude_dir"
    (claude_dir / "projects").mkdir(parents=True)
    return {
        "AUTO_SKILL_ROOT": str(skill_root),
        "AUTO_CLAUDE_DIR": str(claude_dir),
    }


def _make_session_file(env: dict[str, str], project_name: str, session_id: str,
                       cwd: str, **jsonl_kw) -> Path:
    proj_dir = Path(env["AUTO_CLAUDE_DIR"]) / "projects" / project_name
    proj_dir.mkdir(parents=True, exist_ok=True)
    f = proj_dir / f"{session_id}.jsonl"
    _make_jsonl(f, **jsonl_kw)
    return f


def _run(env: dict[str, str], session_id: str, cwd: str) -> tuple[int, str, str]:
    full_env = {**__import__("os").environ, **env}
    p = subprocess.run(
        [sys.executable, str(ENQUEUE), "--session", session_id, "--cwd", cwd],
        capture_output=True, text=True, env=full_env,
    )
    return p.returncode, p.stdout, p.stderr


def test_drops_when_already_summarized(tmp_path: Path):
    env = _setup_env(tmp_path, summarized_sessions=["s1"])
    _make_session_file(env, "proj", "s1", "/cwd", msgs=20, has_edit_or_write=True, duration_min=10)
    rc, out, err = _run(env, "s1", "/cwd")
    assert rc == 0
    assert "skipped: already summarized" in out
    queue = Path(env["AUTO_SKILL_ROOT"]) / "auto-queue.jsonl"
    assert not queue.exists() or queue.read_text() == ""


def test_drops_when_auto_paused(tmp_path: Path):
    env = _setup_env(tmp_path, paused=True)
    _make_session_file(env, "proj", "s1", "/cwd", msgs=20, has_edit_or_write=True, duration_min=10)
    rc, out, err = _run(env, "s1", "/cwd")
    assert rc == 0
    assert "skipped: paused" in out


def test_drops_when_auto_disabled(tmp_path: Path):
    env = _setup_env(tmp_path, auto_enabled=False)
    _make_session_file(env, "proj", "s1", "/cwd", msgs=20, has_edit_or_write=True, duration_min=10)
    rc, out, err = _run(env, "s1", "/cwd")
    assert rc == 0
    assert "skipped: auto disabled" in out


def test_drops_when_too_few_messages(tmp_path: Path):
    env = _setup_env(tmp_path)
    _make_session_file(env, "proj", "s1", "/cwd", msgs=3, has_edit_or_write=True, duration_min=10)
    rc, out, err = _run(env, "s1", "/cwd")
    assert rc == 0
    assert "skipped: hard_rule min_messages" in out


def test_drops_when_no_edit_or_write(tmp_path: Path):
    env = _setup_env(tmp_path)
    _make_session_file(env, "proj", "s1", "/cwd", msgs=20, has_edit_or_write=False, duration_min=10)
    rc, out, err = _run(env, "s1", "/cwd")
    assert rc == 0
    assert "skipped: hard_rule require_edit_or_write" in out


def test_drops_when_too_short_duration(tmp_path: Path):
    env = _setup_env(tmp_path, hard_rules_overrides={"min_duration_min": 60})
    _make_session_file(env, "proj", "s1", "/cwd", msgs=20, has_edit_or_write=True, duration_min=10)
    rc, out, err = _run(env, "s1", "/cwd")
    assert rc == 0
    assert "skipped: hard_rule min_duration_min" in out


def test_enqueue_when_passes_all_rules(tmp_path: Path):
    env = _setup_env(tmp_path)
    _make_session_file(env, "proj", "s1", "/cwd",
                       msgs=20, has_edit_or_write=True, duration_min=10)
    rc, out, err = _run(env, "s1", "/cwd")
    assert rc == 0, f"stderr={err}"
    assert "enqueued" in out
    queue = Path(env["AUTO_SKILL_ROOT"]) / "auto-queue.jsonl"
    assert queue.exists()
    line = queue.read_text(encoding="utf-8").strip()
    data = json.loads(line)
    assert data["session_id"] == "s1"
    assert data["cwd"] == "/cwd"
    assert data["failure_count"] == 0
    assert "metrics" in data
    assert data["metrics"]["has_edit_or_write"] is True


def test_silent_exit_on_missing_jsonl(tmp_path: Path):
    """spec 要求 enqueue 异常不阻塞会话退出。"""
    env = _setup_env(tmp_path)
    rc, out, err = _run(env, "missing-session-id", "/cwd")
    assert rc == 0
    assert "skipped: session jsonl not found" in out


def test_size_kb_threshold(tmp_path: Path):
    env = _setup_env(tmp_path, hard_rules_overrides={"min_size_kb": 1000})
    _make_session_file(env, "proj", "s1", "/cwd",
                       msgs=20, has_edit_or_write=True, duration_min=10)
    rc, out, err = _run(env, "s1", "/cwd")
    assert "skipped: hard_rule min_size_kb" in out
