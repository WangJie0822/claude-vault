"""Codex 声明命令 → stdin → 共用核心 → 输出/控制回执的完整本地链路。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from context_vault import control
from context_vault.session_source import resolve_current_session

HOOKS = json.loads((ROOT / "hooks/codex-hooks.json").read_text(encoding="utf-8"))["hooks"]
NOTE = "notes/recall.md"


@pytest.fixture
def env(tmp_home, tmp_vault, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_home / ".codex"))
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    (tmp_vault / "notes").mkdir()
    (tmp_vault / NOTE).write_text("---\ntags: [hook, recall]\nsummary: hook recall 输入输出设计\n---\n# recall\n测试正文\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(ROOT / "skills/summarize-session/scripts/rebuild_index.py"),
        "--vault", str(tmp_vault)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    config = {"vault_path": str(tmp_vault), "opt_out_paths": [], "metrics": {"enabled": True},
              "relevance": {"fine_rank": False, "fine_supplement": False},
              "keyword_to_tags": {"project": ["hook"]}}
    path = tmp_home / ".context-vault/config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")
    for sid in ("thread-a", "thread-b"):
        p = tmp_home / ".codex/sessions" / ("rollout-test-" + sid + ".jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"type": "session_meta", "timestamp": "2026-10-20T00:00:00Z",
            "payload": {"id": sid, "cwd": str(project), "source": "vscode"}}) + "\n", encoding="utf-8")
    return {"home": tmp_home, "project": project, "vault": tmp_vault, "config": config, "config_path": path}


def _payload(env, event="UserPromptSubmit", *, sid="thread-a", turn="turn-a", prompt="hook recall", source="startup"):
    payload = {"hook_event_name": event, "session_id": sid, "cwd": str(env["project"]),
        "transcript_path": str(env["home"] / ".codex/sessions" / ("rollout-test-" + sid + ".jsonl")),
        "prompt": prompt, "source": source}
    if event == "UserPromptSubmit":
        payload["turn_id"] = turn
    return payload


def _run_timed(env, event="UserPromptSubmit", *, env_extra=None, **kwargs):
    """计时覆盖宿主声明的完整 shell/wrapper/适配器；不含构造夹具。"""
    payload = _payload(env, event, **kwargs)
    spec = HOOKS[event][0]["hooks"][0]
    command = spec["commandWindows" if os.name == "nt" else "command"].replace("${PLUGIN_ROOT}", ROOT.as_posix())
    argv = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
            if os.name == "nt" else ["/bin/sh", "-c", command])
    proc_env = dict(os.environ, PLUGIN_ROOT=str(ROOT), CLAUDE_PLUGIN_ROOT=str(ROOT),
        CLAUDE_CODE_ENTRYPOINT="sdk-py", PYTHONUTF8="1")
    proc_env.update(env_extra or {})
    started = perf_counter()
    result = subprocess.run(argv, input=json.dumps(payload).encode("utf-8"), capture_output=True,
        cwd=env["project"], env=proc_env, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    elapsed = perf_counter() - started
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return elapsed, (json.loads(result.stdout) if result.stdout.strip() else {}), result.stderr.decode("utf-8", "replace")


def _run(env, event="UserPromptSubmit", **kwargs):
    _, result, error = _run_timed(env, event, **kwargs)
    return result, error


def test_declared_command_timer_includes_outer_process_delay(env, monkeypatch):
    """假时钟只替换本模块；外层耗时必须进入样本，不能只计共享核心。"""
    from tests.integration import test_codex_recall as current
    calls, clock = [], [0.0]
    def delayed(argv, **kwargs):
        calls.append((argv, kwargs))
        clock[0] += 1.0
        return subprocess.CompletedProcess(argv, 0, stdout=b"{}", stderr=b"")
    monkeypatch.setattr(current, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(current.subprocess, "run", delayed)
    elapsed, result, error = _run_timed(env)
    assert elapsed == 1.0 and result == {} and error == ""
    argv, options = calls[0]
    assert argv[0] == ("powershell.exe" if os.name == "nt" else "/bin/sh")
    assert "run-codex-hook" in argv[-1]
    assert json.loads(options["input"])["session_id"] == "thread-a"
    assert options["timeout"] == 60


def _attach(env, sid="thread-a"):
    ref = resolve_current_session(host="codex", context={"thread_id": sid}, home=env["home"])
    return control.attach(ref, home=env["home"])["capability"]


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_codex_declared_command_runs_real_recall_without_live_plaintext(env, event):
    result, error = _run(env, event)
    assert result.get("hookSpecificOutput", {}).get("hookEventName") == event, (result, error)
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "recall" in text and "非指令" in text
    assert not (env["home"] / ".context-vault/state/live/codex").exists()
    if event == "UserPromptSubmit":
        files = list((env["home"] / ".context-vault/metrics/codex/v2").rglob("*.jsonl"))
        assert files
        records = [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines()]
        assert any(r.get("runtime") == "codex" for r in records)


def test_control_is_queued_until_next_winning_hook_and_duplicate_preserves_it(env):
    cap = _attach(env)
    _run(env, turn="first")
    state = control.get_state(cap, home=env["home"])
    assert state["items"]
    item = next(k for k, v in state["items"].items() if v["path"] == NOTE)
    queued = control.submit_ops(cap, {"epoch": 0, "operation_id": "exclude-one", "exclude": [item]}, home=env["home"])
    assert queued["queued_revision"] == 1
    before = control.get_state(cap, home=env["home"])
    _run(env, turn="first")
    duplicate = control.get_state(cap, home=env["home"])
    assert duplicate["applied_revision"] == 0 and duplicate["snapshot_ts"] == before["snapshot_ts"]
    _run(env, turn="next")
    applied = control.get_state(cap, home=env["home"])
    assert applied["applied_revision"] == 1
    assert NOTE in [item["path"] for item in applied["snapshot"]["core"]["excluded"]]


def test_disabled_hook_reports_status_but_does_not_ack_queued_change(env):
    cap = _attach(env)
    _run(env, turn="first")
    state = control.get_state(cap, home=env["home"])
    assert state["items"]
    item = next(iter(state["items"]))
    control.submit_ops(cap, {"epoch": 0, "operation_id": "exclude-one", "exclude": [item]}, home=env["home"])
    env["config_path"].write_text(json.dumps(dict(env["config"], enabled=False)), encoding="utf-8")
    _run(env, turn="disabled")
    after = control.get_state(cap, home=env["home"])
    assert after["applied_revision"] == 0
    assert after["snapshot"]["core"]["status"] == "disabled"


def test_runtime_disabled_attached_panel_has_terminal_disabled_status(env, monkeypatch):
    cap = _attach(env)
    monkeypatch.setenv("VAULT_LOADER_DISABLE", "1")
    _run(env, turn="runtime-disabled")
    assert control.get_state(cap, home=env["home"])["snapshot"]["core"]["status"] == "disabled"


def test_closing_panel_keeps_intent_without_publishing_another_snapshot(env):
    cap = _attach(env)
    _run(env, turn="first")
    state = control.get_state(cap, home=env["home"])
    item = next(iter(state["items"]))
    control.submit_ops(cap, {"epoch": 0, "operation_id": "exclude-one", "exclude": [item]}, home=env["home"])
    control.detach(cap, home=env["home"])
    _run(env, turn="closed")
    after = control.get_state(cap, home=env["home"])
    assert after["applied_revision"] == 1
    assert after["snapshot"] == state["snapshot"]


def test_session_start_snapshot_does_not_ack_ups_operations(env):
    cap = _attach(env)
    _run(env, turn="first")
    control.submit_ops(cap, {"epoch": 0, "operation_id": "setting-one",
        "settings": {"fine_max_notes": 2}}, home=env["home"])
    _run(env, event="SessionStart", source="resume")
    assert control.get_state(cap, home=env["home"])["applied_revision"] == 0
    _run(env, turn="next")
    assert control.get_state(cap, home=env["home"])["applied_revision"] == 1


def test_custom_codex_model_survives_real_control_core_disk_and_snapshot(env):
    cap = _attach(env)
    model = "CV/" + "a" * 100
    control.submit_ops(cap, {"epoch": 0, "operation_id": "long-model", "settings": {"fine_model": model}}, home=env["home"])
    _run(env, turn="set-model")
    state = control.get_state(cap, home=env["home"])
    assert state["applied_revision"] == 1
    assert state["snapshot"]["core"]["settings"]["effective"]["fine_model"] == model
    files = list((env["home"] / ".context-vault/state/codex/v2/fine").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["session_settings"].get("fine_model") == model
    _run(env, turn="read-back-model")
    state = control.get_state(cap, home=env["home"])
    assert state["snapshot"]["core"]["settings"]["effective"]["fine_model"] == model


def test_disabled_without_panel_does_not_create_runtime_state(env):
    env["config_path"].write_text(json.dumps(dict(env["config"], enabled=False)), encoding="utf-8")
    result, error = _run(env)
    assert result == {}, error
    assert not (env["home"] / ".context-vault/state").exists()


def test_runtime_disable_still_invalidates_inflight_epoch_on_compact(env, monkeypatch):
    from context_vault import lifecycle
    _run(env, turn="before-disable")
    assert lifecycle.current_epoch(home=env["home"], thread_id="thread-a") == 0
    monkeypatch.setenv("VAULT_LOADER_DISABLE", "1")
    result, error = _run(env, event="SessionStart", source="compact")
    assert result == {}, error
    assert lifecycle.current_epoch(home=env["home"], thread_id="thread-a") == 1


def test_compact_advances_epoch_and_keeps_exclusion_intent(env):
    cap = _attach(env)
    _run(env, turn="first")
    state = control.get_state(cap, home=env["home"])
    item = next(iter(state["items"]))
    control.submit_ops(cap, {"epoch": 0, "operation_id": "exclude-one", "exclude": [item]}, home=env["home"])
    compact, error = _run(env, event="SessionStart", source="compact")
    current = control.get_state(cap, home=env["home"])
    assert current["epoch"] == 1, (current, error)
    assert current["snapshot"]["epoch"] == 1
    assert NOTE not in [item["path"] for item in current["snapshot"]["core"]["session_start"]]
    with pytest.raises(ValueError, match="epoch"):
        control.submit_ops(cap, {"epoch": 0, "operation_id": "stale", "settings": {}}, home=env["home"])
    _run(env, turn="after-compact")
    assert control.get_state(cap, home=env["home"])["applied_revision"] == 1


def test_resume_preserves_old_snapshot_until_real_start_and_fork_is_independent(env):
    cap = _attach(env)
    _run(env, turn="first")
    old = control.get_state(cap, home=env["home"])
    reopened = _attach(env)
    assert control.get_state(reopened, home=env["home"])["snapshot_ts"] == old["snapshot_ts"]
    _run(env, event="SessionStart", source="resume")
    assert control.get_state(reopened, home=env["home"])["epoch"] == 0
    other = _attach(env, "thread-b")
    _run(env, event="SessionStart", sid="thread-b", source="fork")
    state = control.get_state(other, home=env["home"])
    assert state["queued_revision"] == state["applied_revision"] == 0
    assert state["snapshot"]["thread_id"] == "thread-b"


def test_optout_without_panel_does_not_create_runtime_state(env):
    env["config_path"].write_text(json.dumps(dict(env["config"], opt_out_paths=[str(env["project"])])), encoding="utf-8")
    result, error = _run(env)
    assert result == {}, error
    assert not (env["home"] / ".context-vault/state").exists()


@pytest.mark.parametrize("model", [None, "CV/" + "a" * 100])
@pytest.mark.parametrize("outcome", ["spawned", "spawn_error", "no_codex"])
def test_real_hook_preserves_codex_selection_through_parent_spawn(env, monkeypatch, model, outcome):
    from context_vault.codex_entry import run_hook
    config = dict(env["config"])
    config["relevance"] = {"fine_rank": True, "fine_supplement": True}
    runtime = {"relevance": {"fine_rank": True, "coarse_inject": False}, "profile": "work"}
    if model is not None:
        runtime["model"] = model
    config["runtimes"] = {"codex": runtime}
    env["config_path"].write_text(json.dumps(config), encoding="utf-8")
    cap = _attach(env)
    module = control.loader_module("prompt_submit_load")
    seen = []
    def spawn(cwd, session_id, payload):
        seen.append(payload)
        return outcome  # 只截取真实父入口产物；不启动模型。
    monkeypatch.setattr(module, "spawn_fine", spawn)
    run_hook("UserPromptSubmit", _payload(env, turn="model-parent"), home=env["home"])
    assert len(seen) == 1
    payload = seen[0]
    assert payload["cfg"].get("backend") == "codex"
    assert payload["cfg"]["model_selection"]["model"] == model
    assert payload["cfg"]["model_selection"]["profile"] == "work"
    assert payload["session_source"]["thread_id"] == "thread-a"
    assert payload["codex_context"] == {"thread_id": "thread-a", "epoch": 0}
    llm = control.get_state(cap, home=env["home"])["snapshot"]["core"]["llm"]
    if outcome == "spawned":
        assert (llm["state"], llm["fail"]) == ("pending", None)
    else:
        assert (llm["state"], llm["fail"]) == ("unavailable", outcome)


def test_global_fine_disable_blocks_codex_opt_in_before_spawn(env, monkeypatch):
    from context_vault.codex_entry import run_hook
    config = dict(env["config"], runtimes={"codex": {"relevance": {"fine_rank": True, "coarse_inject": False}}})
    env["config_path"].write_text(json.dumps(config), encoding="utf-8")
    module = control.loader_module("prompt_submit_load")
    seen = []
    monkeypatch.setattr(module, "spawn_fine", lambda *args: seen.append(args) or "spawn_error")
    run_hook("UserPromptSubmit", _payload(env, turn="fine-off"), home=env["home"])
    assert seen == []


@pytest.mark.parametrize("key,value", [("fine_max_notes", 1), ("user_prompt_submit.max_notes", 1), ("fine_rank", False)])
def test_codex_persist_narrowing_targets_runtime_without_modifying_claude(env, key, value):
    cap = _attach(env)
    before = json.loads(env["config_path"].read_text(encoding="utf-8"))
    control.submit_ops(cap, {"epoch": 0, "operation_id": "persist-small", "settings": {key: value},
                           "persist": [key]}, home=env["home"])
    _run(env, turn="persist-small")
    state = control.get_state(cap, home=env["home"])
    assert state["applied_revision"] == 1
    assert state["snapshot"]["core"]["persisted"] == [key]
    raw = json.loads(env["config_path"].read_text(encoding="utf-8"))
    section, leaf = (key.split(".", 1) if "." in key else ("relevance", key))
    assert raw["runtimes"]["codex"][section][leaf] == value
    assert {k: v for k, v in raw.items() if k != "runtimes"} == before


def test_core_read_denial_is_visible_and_keeps_real_identity_without_ack(env, monkeypatch, capsys):
    from context_vault.codex_entry import run_hook
    cap = _attach(env)
    _run(env, turn="first")
    control.submit_ops(cap, {"epoch": 0, "operation_id": "setting-one", "settings": {"fine_max_notes": 2}}, home=env["home"])
    module = control.loader_module("prompt_submit_load")
    def deny(_):
        raise PermissionError("synthetic external vault denial")
    monkeypatch.setattr(module, "load_cache_status", deny)
    payload = _payload(env, turn="denied")
    before = json.dumps(payload, sort_keys=True)
    run_hook("UserPromptSubmit", payload, home=env["home"])
    assert json.dumps(payload, sort_keys=True) == before
    state = control.get_state(cap, home=env["home"])
    assert state["applied_revision"] == 0
    assert state["snapshot"]["thread_id"] == "thread-a"
    assert state["snapshot"]["turn_id"] == "denied"
    assert state["snapshot"]["core"]["status"] == "error"
    output = capsys.readouterr()
    assert "PermissionError" in output.err and "hook recall" not in output.err
