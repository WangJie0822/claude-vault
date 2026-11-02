"""本地文本控制与结构化读；所有 HOME 与 Vault 都是合成目录。"""
from __future__ import annotations

import importlib.util
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import replace
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cv_text_control", ROOT / "scripts/vault_control.py")
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
from context_vault import control
from context_vault.codex_entry import run_hook
from context_vault.session_source import resolve_current_session


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex")); monkeypatch.setenv("CODEX_THREAD_ID", "thread-a")
    for sid in ("thread-a", "thread-b"):
        source = tmp_path / ".codex/sessions" / ("rollout-example-" + sid + ".jsonl")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "vscode", "cwd": str(tmp_path)}}) + "\n", encoding="utf-8")
    vault = tmp_path / "vault"; (vault / "notes").mkdir(parents=True)
    (vault / "notes/recall.md").write_text("---\ntags: [recall]\nsummary: recall 测试正文与使用场景\n---\n成功读取合成正文", encoding="utf-8")
    result = subprocess.run([sys.executable, str(ROOT / "skills/summarize-session/scripts/rebuild_index.py"), "--vault", str(vault)], capture_output=True, timeout=60)
    assert result.returncode == 0
    config = tmp_path / ".context-vault/config.json"; config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({"vault_path": str(vault), "relevance": {"fine_rank": False}, "metrics": {"enabled": False}}), encoding="utf-8")
    return tmp_path, vault, config


def call(capsys, *args):
    rc = cli.main(["--host", "codex", "--thread", "thread-a", *args])
    captured = capsys.readouterr()
    return rc, json.loads(captured.out) if captured.out.strip() else None, captured.err


def test_cli_partial_removal_keeps_other_retained_epoch_intents(env, capsys):
    from context_vault.runtime import HookContext, RuntimeKind
    home, _, _ = env
    ref = resolve_current_session(host='codex', context={'thread_id': 'thread-a'}, home=home)
    cap = control.attach(ref, home=home)['capability']
    ctx = HookContext.from_payload({'hook_event_name': 'UserPromptSubmit', 'session_id': 'thread-a',
        'thread_id': 'thread-a', 'turn_id': 'seed', 'actor_kind': 'main'}, runtime=RuntimeKind.CODEX)
    claim = control.claim_control(ctx, home=home)
    report = control.loader_module('_session_ops').TurnReport(status='scored')
    report.injected = [{'path': 'notes/a.md', 'kind': 'admitted_list'}, {'path': 'notes/b.md', 'kind': 'admitted_list'}]
    control.ack_control(ctx, revision=claim['revision'], report=report.to_snapshot(), home=home)
    ids = list(control.get_state(cap, home=home)['items'])
    control.submit_ops(cap, {'epoch': 0, 'operation_id': 'seed-intent', 'pin': ids, 'exclude': ids}, home=home)
    control.advance_epoch(replace(ctx, context_epoch=1), home=home)
    state = call(capsys, 'status')[1]
    removed = next(key for key,item in state['items'].items() if item['path'] == 'notes/a.md')
    for kind in ('pin', 'exclude'):
        assert call(capsys, '--remove', kind, removed)[0] == 0
    state = call(capsys, 'status')[1]
    assert state['intent']['pin'] == state['intent']['exclude'] == ['notes/b.md']


def test_text_status_is_readonly_and_settings_queue_without_panel(env, capsys):
    home, _, _ = env
    rc, state, error = call(capsys, "status")
    assert rc == 0, error
    assert state["thread_id"] == "thread-a" and state["snapshot"] is None
    assert not (home / ".context-vault/state/live").exists()
    rc, queued, error = call(capsys, "set", "fine_max_notes", "1")
    assert rc == 0 and queued["status"] == "queued", error
    assert call(capsys, "status")[1]["applied_revision"] == 0
    assert all("attachment" not in json.loads(p.read_text(encoding="utf-8")) for p in (home / ".context-vault/state/live").rglob("*.json"))


def test_single_key_persist_preserves_other_settings_through_real_hook(env, capsys, monkeypatch):
    """persist 只限定落盘键；未选的模型与 false 必须继续传给下一轮。"""
    home, _, config = env
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["relevance"] = {"fine_rank": True, "fine_supplement": True, "fine_max_notes": 4}
    raw["runtimes"] = {"codex": {"model": "CV/Runtime", "profile": "work", "model_provider": "gateway",
        "relevance": {"fine_rank": True, "fine_supplement": True}}}
    config.write_text(json.dumps(raw), encoding="utf-8")
    ref = resolve_current_session(host="codex", context={"thread_id": "thread-a"}, home=home)
    control.attach(ref, home=home)
    def command(*args):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/vault_control.py"),
            "--host", "codex", "--thread", "thread-a", *args], cwd=str(ROOT),
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), capture_output=True,
            text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    expected = {"fine_model": "CV/Session", "fine_rank": False, "fine_max_notes": 2}
    for key, value in expected.items():
        command("set", key, value if key == "fine_model" else json.dumps(value))
    command("persist", "fine_max_notes")
    state_mod, fine = control.loader_module("_state"), control.loader_module("_fine_state")
    state_mod.configure_context("codex", control.core_session_id("thread-a"))
    topic = control.loader_module("_topic")
    spawned = []
    monkeypatch.setattr(topic, "spawn_fine", lambda *args, **kwargs: spawned.append((args, kwargs)) or "no_codex")
    run_hook("UserPromptSubmit", {"hook_event_name": "UserPromptSubmit", "session_id": "thread-a",
        "turn_id": "persist-settings", "cwd": str(home), "prompt": "recall 测试模型配置"}, home=home)
    captured = capsys.readouterr()
    assert "degraded" not in captured.err
    status = command("status")
    stored = fine.load_fine(control.core_session_id("thread-a"))
    assert stored.turn_seq == 1
    assert stored.session_settings == expected
    assert status["intent"]["settings"] == expected
    assert spawned == [], "false 不得在 persist 后恢复成开启并发起模型"
    written = json.loads(config.read_text(encoding="utf-8"))
    assert written["relevance"] == raw["relevance"]
    assert written["runtimes"]["codex"] == {**raw["runtimes"]["codex"], "relevance": {
        **raw["runtimes"]["codex"]["relevance"], "fine_max_notes": 2}}
    assert status["snapshot"]["core"]["persisted"] == ["fine_max_notes"]
    # 完整替换协议仍保留 {} 的显式清空语义。
    control.submit_local_ops(ref, {"epoch": status["epoch"], "operation_id": "clear-settings",
        "settings": {}}, home=home)
    assert control.local_state(ref, home=home)["intent"]["settings"] == {}
    run_hook("UserPromptSubmit", {"hook_event_name": "UserPromptSubmit", "session_id": "thread-a",
        "turn_id": "clear-settings", "cwd": str(home), "prompt": "recall 清空会话设置"}, home=home)
    assert fine.load_fine(control.core_session_id("thread-a")).session_settings == {}


def test_text_entry_rejects_other_current_thread(env, capsys):
    assert cli.main(["--thread", "thread-b", "status"]) == 2
    assert "thread" in capsys.readouterr().err


def test_subagent_source_cannot_use_main_thread_text_controls(env, capsys):
    home, _, _ = env
    source = home / ".codex/sessions/rollout-example-thread-a.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-a", "cwd": str(home),
                      "source": {"subagent": "thread_spawn"}}}) + "\n", encoding="utf-8")
    assert call(capsys, "status")[0] == 2
    assert not (home / ".context-vault/state/live").exists()


def test_failed_browser_open_detaches_and_does_not_return_capability(env, capsys, monkeypatch):
    home, _, _ = env
    seen = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: seen.append(url) or False)
    rc, result, error = call(capsys, "open")
    assert rc == 0 and result["status"] == "browser_unavailable", error
    capability = seen[0].split("#", 1)[1]
    assert capability not in json.dumps(result)
    path = next((home / ".context-vault/state/live/codex").glob("*.json"))
    assert json.loads(path.read_text())["attachment"]["lease_until"] == 0


def seed(env, capsys):
    home, _, _ = env
    ref = resolve_current_session(host="codex", context={"thread_id": "thread-a"}, home=home)
    control.attach(ref, home=home)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "thread-a", "turn_id": "turn-a", "cwd": str(home),
               "transcript_path": str(ref.transcript_path), "prompt": "recall 测试"}
    run_hook("UserPromptSubmit", payload, home=home); capsys.readouterr()
    state = call(capsys, "status")[1]
    assert state["items"]
    return next(iter(state["items"]))


def test_read_note_accepts_only_server_identity_and_metrics_off_has_no_new_trace(env, capsys):
    home, _, _ = env
    item = seed(env, capsys)
    before = {str(p): p.read_bytes() for p in (home / ".context-vault").rglob("*.json")}
    rc, read, error = call(capsys, "read-note", item)
    assert rc == 0 and "成功读取合成正文" in read["text"], error
    assert read["observed"] is False and read["reason"] == "metrics_off"
    assert before == {str(p): p.read_bytes() for p in (home / ".context-vault").rglob("*.json")}
    assert call(capsys, "read-note", "../../secret")[0] == 2


def test_structured_success_is_observed_once_and_failed_read_is_not(env, capsys, monkeypatch):
    home, vault, config = env
    data = json.loads(config.read_text()); data["metrics"]["enabled"] = True; config.write_text(json.dumps(data), encoding="utf-8")
    item = seed(env, capsys)
    first = call(capsys, "read-note", item)[1]
    assert first["observed"] is True and first["deduplicated"] is False
    assert call(capsys, "read-note", item)[1]["deduplicated"] is True
    saved = {str(p): p.read_bytes() for p in (home / ".context-vault/state/live").rglob("*.json")}
    original = Path.open
    def denied(path, *args, **kwargs):
        if path == vault / "notes/recall.md":
            raise PermissionError("synthetic denial")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    assert call(capsys, "read-note", item)[0] == 2
    assert saved == {str(p): p.read_bytes() for p in (home / ".context-vault/state/live").rglob("*.json")}


def test_successful_reads_reach_next_real_metric_record_once(env, capsys):
    home, _, config = env
    data = json.loads(config.read_text()); data["metrics"]["enabled"] = True; config.write_text(json.dumps(data), encoding="utf-8")
    item = seed(env, capsys)
    assert call(capsys, "read-note", item)[1]["observed"]
    assert call(capsys, "read-note", item)[1]["deduplicated"]
    def turn(turn_id):
        run_hook("UserPromptSubmit", {"hook_event_name": "UserPromptSubmit", "session_id": "thread-a", "turn_id": turn_id,
                 "cwd": str(home), "prompt": "recall 新的设计"}, home=home)
        capsys.readouterr()
    turn("after-read")
    files = list((home / ".context-vault/metrics/codex/v2").rglob("*.jsonl"))
    rows = [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines()]
    assert sum(row.get("n_vault_reads", 0) for row in rows) == 1
    turn("after-consumption")
    rows = [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines()]
    assert sum(row.get("n_vault_reads", 0) for row in rows) == 1


def test_real_annotation_cli_passes_displayed_turn_identity_to_hook(env, capsys):
    home, _, config = env
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["metrics"]["enabled"] = True
    config.write_text(json.dumps(raw), encoding="utf-8")
    item = seed(env, capsys)
    ref = resolve_current_session(host="codex", context={"thread_id": "thread-a"}, home=home)
    displayed = control.local_state(ref, home=home)
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/vault_control.py"),
        "--host", "codex", "--thread", "thread-a", "--verdict", "relevant", "annotate", item],
        cwd=str(ROOT), env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "queued"
    saved = json.loads(control._ref_path(ref, home).read_text(encoding="utf-8"))
    assert len(saved["annotations"]) == 1
    annotation = saved["annotations"][0]
    assert annotation["prompt_h"] == displayed["items"][item]["prompt_h"]
    run_hook("UserPromptSubmit", {"hook_event_name": "UserPromptSubmit", "session_id": "thread-a",
        "turn_id": "after-annotation", "cwd": str(home), "prompt": "recall 标注后续提问"}, home=home)
    captured = capsys.readouterr()
    assert "degraded" not in captured.err
    applied = control.local_state(ref, home=home)
    assert annotation["id"] in applied["snapshot"]["core"]["acked"]
    assert applied["applied_revision"] == 1


def test_open_uses_private_stdin_and_browser_fragment_then_service_closes(env, capsys, monkeypatch):
    urls, commands, processes = [], [], []
    original = cli.subprocess.Popen
    def spawn(args, **kwargs):
        commands.append(args)
        process = original(args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(cli.subprocess, "Popen", spawn)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: urls.append(url) or True)
    rc, result, error = call(capsys, "open")
    assert rc == 0 and result["status"] == "opened", error
    url = urlsplit(urls[0]); capability = url.fragment
    try:
        assert capability and capability not in json.dumps(result)
        assert capability not in json.dumps(commands)
        conn = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
        conn.request("POST", "/api/close", "{}", {"Origin": "http://" + url.netloc,
                     "Authorization": "Bearer " + capability, "X-Context-Vault-Thread": "thread-a",
                     "Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 200 and json.loads(response.read())["status"] == "detached"
        conn.close()
    finally:
        # 即使断言失败，也只清理本用例启动的服务。
        control.detach(capability, home=env[0])
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)


def test_read_remains_bounded_when_note_grows_after_stat(env, capsys, monkeypatch):
    item = seed(env, capsys)
    from context_vault import structured_read
    original, sizes = Path.open, []
    class Growing(io.BytesIO):
        def read(self, size=-1):
            sizes.append(size)
            assert 0 < size <= structured_read.MAX_NOTE_BYTES + 1
            return b"x" * size
    def opening(path, *args, **kwargs):
        if path == env[1] / "notes/recall.md":
            return Growing()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opening)
    rc, result, error = call(capsys, "read-note", item)
    assert rc == 2 and "limit" in error and result is None
    assert sizes == [structured_read.MAX_NOTE_BYTES + 1]
