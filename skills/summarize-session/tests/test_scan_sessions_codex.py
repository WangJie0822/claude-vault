"""Codex CLI 使用精确线程和共享来源；所有 HOME 均为合成目录。"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/scan_sessions.py"


def _source(home, sid="thread-a", malformed=False, source="vscode"):
    p = home / ".codex/sessions/2026/10/20" / ("rollout-example-" + sid + ".jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "session_meta", "timestamp": "2026-10-20T00:00:00Z",
             "payload": {"id": sid, "cwd": str(home / "project"), "source": source}},
            {"type": "response_item", "timestamp": "2026-10-20T00:01:00Z", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "整理会话设计与验证结果"}]}}]
    p.write_text("\n".join(json.dumps(r) for r in rows) + ("\n{broken" if malformed else "\n"), encoding="utf-8")
    return p


def _run(home, *args, thread="thread-a"):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / ".codex"), PYTHONUTF8="1")
    env.pop("CODEX_THREAD_ID", None)
    if thread:
        env["CODEX_THREAD_ID"] = thread
    return subprocess.run([sys.executable, str(SCRIPT), "--runtime", "codex", *args], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_codex_parse_and_timerange_share_exact_thread(tmp_path):
    _source(tmp_path)
    other = _source(tmp_path, "thread-b")
    os.utime(other, (2000000000, 2000000000))
    p = _run(tmp_path, "--parse", "--session", "thread-a")
    assert p.returncode == 0, p.stderr
    item = json.loads(p.stdout)["sessions"][0]
    assert item["conversation"] == [{"role": "user", "text": "整理会话设计与验证结果"}]
    assert item["completeness"] == "complete"
    p = _run(tmp_path, "--timerange", str(tmp_path / "project"))
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["session_id"] == "thread-a"


def test_codex_marks_preserve_other_hosts_and_ignore_unscoped_old_ids(tmp_path):
    _source(tmp_path)
    manifest = tmp_path / ".context-vault/sessions/summarized-sessions.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"schema": 2, "sessions": ["thread-a", {"id": "claude-one", "runtime": "claude"}]}), encoding="utf-8")
    p = _run(tmp_path, "--min-size", "0")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["unsummarized_count"] == 1
    p = _run(tmp_path, "--mark-current", str(tmp_path / "project"))
    assert p.returncode == 0, p.stderr
    entries = json.loads(manifest.read_text(encoding="utf-8"))["sessions"]
    assert {"id": "claude-one", "runtime": "claude"} in entries
    assert {"id": "thread-a", "runtime": "codex"} in entries
    assert {"id": "thread-a", "runtime": "claude-legacy"} in entries


def test_codex_incomplete_or_unknown_identity_is_never_marked(tmp_path):
    _source(tmp_path, malformed=True)
    p = _run(tmp_path, "--mark-current", str(tmp_path / "project"))
    assert p.returncode == 2 and "incomplete" in p.stdout
    p = _run(tmp_path, "--mark-current", str(tmp_path / "project"), thread="")
    assert p.returncode == 2 and "identity" in p.stdout
    assert not (tmp_path / ".context-vault/sessions/summarized-sessions.json").exists()


def test_manual_codex_summary_rebuilds_artifacts_before_exact_mark(tmp_path):
    _source(tmp_path)
    _source(tmp_path, "thread-b")
    parsed = _run(tmp_path, "--session", "thread-a", "--parse")
    assert parsed.returncode == 0
    assert json.loads(parsed.stdout)["sessions"][0]["completeness"] == "complete"
    vault = tmp_path / "vault"
    note = vault / "技术笔记/codex-summary.md"
    note.parent.mkdir(parents=True)
    (vault / "CLAUDE.md").write_text("# 知识库\n<!-- 索引区 -->\n<!-- /索引区 -->\n", encoding="utf-8")
    note.write_text("---\ncategory: 技术笔记\ntags: [codex, session]\nsummary: 整理会话设计与验证结果，记录可复跑的知识沉淀过程\n---\n# 会话设计\n整理会话设计与验证结果\n", encoding="utf-8")
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONUTF8="1")
    rebuilt = subprocess.run([sys.executable, str(SCRIPT.parent / "rebuild_index.py"), "--vault", str(vault), "--emit", "all"],
                             env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    report = json.loads(rebuilt.stdout)
    assert report["index_updated"] and report["cache_written"]
    entries = json.loads((vault / ".meta/frontmatter-cache.json").read_text(encoding="utf-8"))["entries"]
    assert "技术笔记/codex-summary.md" in entries
    assert "技术笔记 索引" in (vault / "CLAUDE.md").read_text(encoding="utf-8")
    assert "codex-summary" in (vault / "技术笔记/技术笔记 索引.md").read_text(encoding="utf-8")
    marked = _run(tmp_path, "--mark", "thread-a")
    assert marked.returncode == 0, marked.stdout + marked.stderr
    rows = json.loads((tmp_path / ".context-vault/sessions/summarized-sessions.json").read_text(encoding="utf-8"))["sessions"]
    assert {"runtime": "codex", "id": "thread-a"} in rows
    assert {"runtime": "codex", "id": "thread-b"} not in rows


def test_custom_manifest_stays_namespaced_when_claude_marks_later(tmp_path):
    _source(tmp_path)
    manifest = tmp_path / "custom.json"
    marked = _run(tmp_path, "--manifest", str(manifest), "--mark", "thread-a")
    assert marked.returncode == 0, marked.stderr
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONUTF8="1")
    p = subprocess.run([sys.executable, str(SCRIPT), "--runtime", "claude", "--manifest",
                        str(manifest), "--mark", "claude-one"], env=env, capture_output=True,
                        text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    entries = json.loads(manifest.read_text(encoding="utf-8"))["sessions"]
    assert {"runtime": "codex", "id": "thread-a"} in entries
    assert {"runtime": "claude", "id": "claude-one"} in entries


def test_history_keeps_valid_sessions_when_other_sources_have_bad_first_records(tmp_path):
    _source(tmp_path)
    broken = _source(tmp_path, "thread-b")
    broken.write_text("{not-json\n", encoding="utf-8")
    array = _source(tmp_path, "thread-c")
    array.write_text("[]\n", encoding="utf-8")
    p = _run(tmp_path, "--min-size", "0")
    assert p.returncode == 0, p.stdout + p.stderr
    got = json.loads(p.stdout)
    assert [item["session_id"] for item in got["sessions"]] == ["thread-a"]
    assert len(got["skipped_sources"]) == 2
    assert {item["reason"] for item in got["skipped_sources"]} == {"invalid_metadata"}


@pytest.mark.parametrize("source,actor", [({"subagent": "reviewer"}, "subagent"), ("future-host", "unknown")])
@pytest.mark.parametrize("operation", ["parse", "mark", "mark-current", "timerange", "touched-repos"])
def test_non_main_codex_sources_are_rejected_before_output_or_mark(tmp_path, source, actor, operation):
    """由真实 session_meta 解析角色；拒绝整批标记且不碰既有 manifest。"""
    _source(tmp_path, "thread-main")
    _source(tmp_path, source=source)
    manifest = tmp_path / ".context-vault/sessions/summarized-sessions.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"schema": 2, "sessions": [{"runtime": "claude", "id": "keep"}]}), encoding="utf-8")
    before = manifest.read_bytes()
    if operation == "parse":
        args = ("--parse", "--session", "thread-main", "thread-a")
    elif operation == "mark":
        args = ("--mark", "thread-main", "thread-a")
    else:
        args = ("--" + operation, str(tmp_path / "project"))
    result = _run(tmp_path, *args)
    assert result.returncode == 2, result.stdout + result.stderr
    out = json.loads(result.stdout)
    assert "main" in out["error"] and actor in out["error"], out
    assert "sessions" not in out and "session_id" not in out
    assert manifest.read_bytes() == before


@pytest.mark.parametrize("source", ["cli", "vscode", "exec", "app-server"])
def test_all_verified_main_sources_can_be_parsed_and_marked(tmp_path, source):
    _source(tmp_path, source=source)
    parsed = _run(tmp_path, "--parse", "--session", "thread-a")
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr
    assert json.loads(parsed.stdout)["sessions"][0]["actor_kind"] == "main"
    marked = _run(tmp_path, "--mark", "thread-a")
    assert marked.returncode == 0, marked.stdout + marked.stderr
    manifest = tmp_path / ".context-vault/sessions/summarized-sessions.json"
    assert {"runtime": "codex", "id": "thread-a"} in json.loads(manifest.read_text(encoding="utf-8"))["sessions"]


def test_history_excludes_non_main_sources_with_inspectable_reasons(tmp_path):
    _source(tmp_path)
    _source(tmp_path, "thread-sub", source={"subagent": "reviewer"})
    _source(tmp_path, "thread-unknown", source="future-host")
    result = _run(tmp_path, "--parse", "--min-size", "0")
    assert result.returncode == 0, result.stdout + result.stderr
    out = json.loads(result.stdout)
    assert [s["session_id"] for s in out["sessions"]] == ["thread-a"]
    assert out["total_sessions_in_range"] == 1
    assert {(s["source"], s["reason"], s["actor_kind"]) for s in out["skipped_sources"]} == {
        ("rollout-example-thread-sub.jsonl", "actor_not_main", "subagent"),
        ("rollout-example-thread-unknown.jsonl", "actor_not_main", "unknown"),
    }
