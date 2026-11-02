"""真实临时会话与磁盘状态，不启动网络或模型。"""
from dataclasses import replace
import json
import sys
from types import SimpleNamespace

import pytest

from context_vault import control as c
from context_vault.runtime import HookContext, RuntimeKind
from context_vault.session_source import resolve_current_session


def test_loader_rejects_namespace_package_without_origin(monkeypatch):
    monkeypatch.setitem(sys.modules, "scripts", SimpleNamespace(__file__=None))
    try:
        c.loader_module("_note_paths")
    except Exception as exc:
        assert isinstance(exc, ValueError) and str(exc) == "unexpected loader package"
    else:
        pytest.fail("untrusted namespace package accepted")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    for sid in ("thread-a", "thread-b"):
        path = tmp_path / ".codex/sessions" / ("rollout-test-" + sid + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": sid, "cwd": str(tmp_path / "project"), "source": "vscode"}}) + "\n", encoding="utf-8")
    return tmp_path


def attach(home, sid="thread-a"):
    ref = resolve_current_session(host="codex", context={"thread_id": sid}, home=home)
    return c.attach(ref, home=home)


def ctx(home, sid="thread-a", turn="turn-a", epoch=0):
    return HookContext.from_payload({"hook_event_name": "UserPromptSubmit", "session_id": sid,
        "thread_id": sid, "turn_id": turn, "actor_kind": "main", "context_epoch": epoch,
        "cwd": str(home / "project")}, runtime=RuntimeKind.CODEX)


def report():
    return {"v": 1, "status": "scored", "metrics_on": True, "prompt_h": "0123456789abcdef",
            "injected": [{"path": "notes/a.md", "kind": "admitted_list"}],
            "acked": [], "persisted": [], "rejected": []}


def test_epoch_clears_history_but_retains_editable_intents_without_annotations(env):
    cap = attach(env)['capability']
    turn = ctx(env, turn='before-compact')
    claim = c.claim_control(turn, home=env)
    actual = report()
    actual['injected'].append({'path': 'notes/b.md', 'kind': 'admitted_list'})
    c.ack_control(turn, revision=claim['revision'], report=actual, home=env)
    old = c.get_state(cap, home=env)
    ids = list(old['items'])
    c.submit_ops(cap, {'epoch': 0, 'operation_id': 'two-intents', 'pin': ids, 'exclude': ids}, home=env)
    c.advance_epoch(replace(turn, context_epoch=1), home=env)
    state = c.get_state(cap, home=env)
    assert state['snapshot'] is None and state.get('display') is None
    assert state['items'] and all(item['kind'] == 'user_intent' for item in state['items'].values())
    assert all('annotation_id' not in item and 'prompt_h' not in item for item in state['items'].values())
    assert not set(ids).intersection(state['items'])
    remaining = next(key for key, item in state['items'].items() if item['path'] == 'notes/b.md')
    c.submit_ops(cap, {'epoch': 1, 'operation_id': 'partial-remove', 'pin': [remaining], 'exclude': [remaining]}, home=env)
    current = c.get_state(cap, home=env)
    assert current['intent']['pin'] == current['intent']['exclude'] == ['notes/b.md']
    with pytest.raises(ValueError):
        c.submit_ops(cap, {'epoch': 1, 'operation_id': 'old-ann', 'annotations': [
            {'item': ids[0], 'verdict': 'relevant', 'annotation_id': old['items'][ids[0]]['annotation_id']}]}, home=env)


def seed(home, cap, sid="thread-a"):
    turn = ctx(home, sid, "initial")
    claim = c.claim_control(turn, home=home)
    assert claim is not None
    c.ack_control(turn, revision=claim["revision"], report=report(), home=home)
    state = c.get_state(cap, home=home)
    assert len(state["items"]) == 1
    return next(iter(state["items"]))


def annotation(home, cap, item, verdict="relevant"):
    return {"item": item, "verdict": verdict,
            "annotation_id": c.get_state(cap, home=home)["items"][item]["annotation_id"]}


@pytest.mark.parametrize("same_prompt", [False, True])
def test_old_annotation_cannot_follow_stable_note_identity_into_new_turn(env, same_prompt):
    """从真实 TurnReport 连续产出 A/B；笔记操作身份稳定，标注必须对应显示过的轮次。"""
    cap = attach(env)["capability"]
    core = c.loader_module("_session_ops")
    states = []
    for turn_id, prompt_h in (("turn-A", "a" * 16), ("turn-B", ("a" if same_prompt else "b") * 16)):
        turn = ctx(env, turn=turn_id)
        claim = c.claim_control(turn, home=env)
        actual = core.TurnReport(status="scored", metrics_on=True, prompt_h=prompt_h)
        actual.injected = [{"path": "notes/a.md", "kind": "admitted_list"}]
        c.ack_control(turn, revision=claim["revision"], report=actual.to_snapshot(), home=env)
        states.append(c.get_state(cap, home=env))
    old_id, new_id = (next(iter(state["items"])) for state in states)
    assert old_id == new_id
    old_item, new_item = (state["items"][old_id] for state in states)
    old_annotation = {"item": old_id, "verdict": "relevant"}
    if "annotation_id" in old_item:
        old_annotation["annotation_id"] = old_item["annotation_id"]
    try:
        c.submit_ops(cap, {"epoch": 0, "operation_id": "old-annotation", "annotations": [old_annotation]}, home=env)
    except ValueError as exc:
        assert str(exc) == "stale annotation identity"
    else:
        pytest.fail("旧轮次标注被接受，会被当前笔记身份重定向到新轮次")
    # 拒收不得产生半条标注；同一旧笔记 ID 仍可用于 pin/exclude。
    c.submit_ops(cap, {"epoch": 0, "operation_id": "pin-stable", "pin": [old_id]}, home=env)
    c.submit_ops(cap, {"epoch": 0, "operation_id": "new-annotation", "annotations": [{
        "item": new_id, "verdict": "irrelevant", "annotation_id": new_item.get("annotation_id")}]}, home=env)
    claimed = c.claim_control(ctx(env, turn="turn-C"), home=env)["core"]
    assert claimed["pin"] == ["notes/a.md"]
    assert [(ann["prompt_h"], ann["verdict"]) for ann in claimed["annotations"]] == [
        (("a" if same_prompt else "b") * 16, "irrelevant")]


@pytest.mark.parametrize("token", [None, "", "f" * 32, "中文"])
def test_annotation_requires_current_server_identity(env, token):
    cap = attach(env)["capability"]
    item = seed(env, cap)
    ann = {"item": item, "verdict": "relevant"}
    if token is not None:
        ann["annotation_id"] = token
    with pytest.raises(ValueError, match="stale annotation identity"):
        c.submit_ops(cap, {"epoch": 0, "operation_id": "missing-or-forged-token", "annotations": [ann]}, home=env)
    assert c.claim_control(ctx(env), home=env)["core"]["annotations"] == []


def test_submit_is_queued_until_real_matching_ack(env):
    cap = attach(env)["capability"]
    item = seed(env, cap)
    queued = c.submit_ops(cap, {"epoch": 0, "operation_id": "op-one", "exclude": [item]}, home=env)
    assert queued["status"] == "queued" and queued["queued_revision"] == 1
    assert c.get_state(cap, home=env)["applied_revision"] == 0
    turn = ctx(env)
    claim = c.claim_control(turn, home=env)
    assert claim["core"]["exclude"] == ["notes/a.md"]
    c.ack_control(turn, revision=claim["revision"], report=report(), home=env)
    assert c.get_state(cap, home=env)["applied_revision"] == 1


def test_two_threads_and_forged_capability_are_isolated(env):
    a, b = attach(env)["capability"], attach(env, "thread-b")["capability"]
    item = seed(env, a)
    assert seed(env, b, "thread-b") == item
    c.submit_ops(a, {"epoch": 0, "operation_id": "op-one", "exclude": [item]}, home=env)
    assert c.claim_control(ctx(env, "thread-b"), home=env)["core"]["exclude"] == []
    with pytest.raises(ValueError):
        c.get_state(a[:-1] + ("x" if a[-1] != "x" else "y"), home=env)
    for path in (env / ".context-vault").rglob("*.json"):
        assert a not in path.read_text(encoding="utf-8")


def test_capability_secret_cannot_be_rebound_to_other_thread_route(env):
    a, b = attach(env)["capability"], attach(env, "thread-b")["capability"]
    forged = b.split(".")[0] + "." + a.split(".")[1]
    with pytest.raises(ValueError, match="capability"):
        c.get_state(forged, home=env)


def test_duplicate_cannot_take_or_ack_winners_claim(env):
    cap = attach(env)["capability"]
    item = seed(env, cap)
    c.submit_ops(cap, {"epoch": 0, "operation_id": "op-one", "pin": [item]}, home=env)
    turn = ctx(env)
    claim = c.claim_control(turn, home=env)
    assert c.claim_control(turn, home=env) is None
    c.ack_control(replace(turn, event_id="other-turn"), revision=1, report=report(), home=env)
    assert c.get_state(cap, home=env)["applied_revision"] == 0
    c.ack_control(turn, revision=claim["revision"], report=report(), home=env)
    assert c.get_state(cap, home=env)["applied_revision"] == 1


def test_gate_or_crash_never_claims_operation_applied(env, monkeypatch):
    cap = attach(env)["capability"]
    item = seed(env, cap)
    c.submit_ops(cap, {"epoch": 0, "operation_id": "op-one", "exclude": [item]}, home=env)
    turn = ctx(env)
    claim = c.claim_control(turn, home=env)
    c.ack_control(turn, revision=claim["revision"], report=dict(report(), status="gate:keywords"), home=env)
    assert c.get_state(cap, home=env)["applied_revision"] == 0
    again = c.claim_control(ctx(env, turn="turn-b"), home=env)
    assert again["core"]["exclude"] == ["notes/a.md"]
    now = c.time.time()
    monkeypatch.setattr(c, "time", SimpleNamespace(time=lambda: now + c.CLAIM_TTL + 1))
    c.ack_control(ctx(env, turn="turn-b"), revision=again["revision"], report=report(), home=env)
    assert c.get_state(cap, home=env)["applied_revision"] == 0


def test_stale_epoch_and_unknown_actor_cannot_claim_or_submit(env):
    cap = attach(env)["capability"]
    seed(env, cap)
    assert c.claim_control(replace(ctx(env), actor_kind="unknown"), home=env) is None
    assert c.claim_control(replace(ctx(env), runtime=RuntimeKind.CLAUDE), home=env) is None
    assert c.claim_control(replace(ctx(env), context_epoch=-1), home=env) is None
    with pytest.raises(ValueError):
        c.submit_ops(cap, {"epoch": 1, "operation_id": "op-one", "settings": {}}, home=env)


@pytest.mark.parametrize("ops", [{"unknown": True}, {"pin": ["../../private.md"]},
    {"pin": ["f" * 24] * 9}, {"settings": {"unknown": 1}}, {"settings": {"fine_model": "x" * 129}},
    {"settings": {"fine_max_notes": 999}}, {"thread_id": "thread-b"}])
def test_untrusted_operation_shapes_are_rejected(env, ops):
    cap = attach(env)["capability"]
    seed(env, cap)
    with pytest.raises(ValueError):
        c.submit_ops(cap, dict(epoch=0, operation_id="op-one", **ops), home=env)


def test_annotation_and_persist_queue_are_idempotent(env):
    cap = attach(env)["capability"]
    item = seed(env, cap)
    ops = {"epoch": 0, "operation_id": "op-one", "annotations": [annotation(env, cap, item)],
           "settings": {"fine_max_notes": 2}, "persist": ["fine_max_notes"]}
    assert c.submit_ops(cap, ops, home=env) == c.submit_ops(cap, ops, home=env)
    turn = ctx(env)
    claim = c.claim_control(turn, home=env)
    assert len(claim["core"]["annotations"]) == 1 and claim["core"]["persist"] == ["fine_max_notes"]
    aid = claim["core"]["annotations"][0]["id"]
    c.ack_control(turn, revision=claim["revision"], report=dict(report(), acked=[aid], persisted=["fine_max_notes"]), home=env)
    c.submit_ops(cap, ops, home=env)
    claim = c.claim_control(ctx(env, turn="later"), home=env)
    assert claim["core"]["annotations"] == claim["core"]["persist"] == []


def test_unacknowledged_annotation_does_not_show_revision_applied(env):
    cap = attach(env)["capability"]
    item = seed(env, cap)
    c.submit_ops(cap, {"epoch": 0, "operation_id": "op-one", "annotations": [annotation(env, cap, item)]}, home=env)
    turn = ctx(env)
    claim = c.claim_control(turn, home=env)
    c.ack_control(turn, revision=claim["revision"], report=report(), home=env)
    assert c.get_state(cap, home=env)["applied_revision"] == 0


def test_newer_persist_with_same_key_survives_older_ack(env):
    cap = attach(env)["capability"]
    seed(env, cap)
    for op_id, value in (("old", 1), ("new", 2)):
        c.submit_ops(cap, {"epoch": 0, "operation_id": op_id, "settings": {"fine_max_notes": value},
                          "persist": ["fine_max_notes"]}, home=env)
        if op_id == "old":
            claim = c.claim_control(ctx(env), home=env)
    c.ack_control(ctx(env), revision=claim["revision"], report=dict(report(), persisted=["fine_max_notes"]), home=env)
    newer = c.claim_control(ctx(env, turn="next"), home=env)
    assert newer["core"]["persist"] == ["fine_max_notes"]
    assert newer["core"]["settings"]["fine_max_notes"] == 2


def test_metrics_off_and_detached_lease_stop_new_plaintext_snapshots(env, monkeypatch):
    cap = attach(env)["capability"]
    turn = ctx(env, turn="initial")
    claim = c.claim_control(turn, home=env)
    assert claim is not None
    c.ack_control(turn, revision=claim["revision"], report=dict(report(), metrics_on=False), home=env)
    item = next(iter(c.get_state(cap, home=env)["items"]))
    with pytest.raises(ValueError, match="metrics_off"):
        c.submit_ops(cap, {"epoch": 0, "operation_id": "op-one", "annotations": [annotation(env, cap, item)]}, home=env)
    c.detach(cap, home=env)
    assert c.claim_control(ctx(env), home=env) is None


def test_expired_attach_does_not_request_snapshot(env, monkeypatch):
    cap = attach(env)["capability"]
    now = c.time.time()
    monkeypatch.setattr(c, "time", SimpleNamespace(time=lambda: now + c.ATTACH_TTL + 1))
    assert c.claim_control(ctx(env), home=env) is None


def test_control_consumes_real_turn_report_and_preserves_each_annotation_kind(env):
    cap = attach(env)["capability"]
    ops = c.loader_module("_session_ops")
    actual = ops.TurnReport(status="scored", metrics_on=True, prompt_h="0123456789abcdef")
    actual.injected = [{"path": "notes/a.md", "title": "a", "kind": "admitted_list"}]
    actual.pins_llm = [{"path": "notes/a.md", "rendered": True}]
    turn = ctx(env, turn="initial")
    claim = c.claim_control(turn, home=env)
    c.ack_control(turn, revision=claim["revision"], report=actual.to_snapshot(), home=env)
    state = c.get_state(cap, home=env)
    assert {item.get("kind") for item in state["items"].values()} == {"admitted_list", "llm_pinned"}


def test_distinct_operation_ids_cannot_collide_in_annotation_identity(env):
    cap = attach(env)["capability"]
    item = seed(env, cap)
    for op_id, count, verdict in (("choice", 11, "relevant"), ("choice1", 1, "irrelevant")):
        c.submit_ops(cap, {"epoch": 0, "operation_id": op_id,
            "annotations": [annotation(env, cap, item, verdict)] * count}, home=env)
    claim = c.claim_control(ctx(env), home=env)
    assert len(claim["core"]["annotations"]) == 12
    assert len({item["id"] for item in claim["core"]["annotations"]}) == 12


@pytest.mark.parametrize("receipt", ["acked", "rejected"])
def test_gate_consumes_actual_annotation_receipts_without_advancing_revision(env, receipt):
    cap = attach(env)["capability"]
    item = seed(env, cap)
    c.submit_ops(cap, {"epoch": 0, "operation_id": "op-one",
        "annotations": [annotation(env, cap, item)]}, home=env)
    turn = ctx(env)
    claim = c.claim_control(turn, home=env)
    aid = claim["core"]["annotations"][0]["id"]
    actual = c.loader_module("_session_ops").TurnReport(status="gate:keywords", metrics_on=True)
    if receipt == "acked":
        actual.acked = [aid]
    else:
        actual.rejected = [{"what": "annotation", "key": aid, "reason": "no_record"}]
    c.ack_control(turn, revision=claim["revision"], report=actual.to_snapshot(), home=env)
    assert c.get_state(cap, home=env)["applied_revision"] == 0
    assert c.claim_control(ctx(env, turn="next"), home=env)["core"]["annotations"] == []


def test_gate_retains_previous_note_identity_for_control(env):
    cap = attach(env)["capability"]
    item = seed(env, cap)
    turn = ctx(env)
    claim = c.claim_control(turn, home=env)
    actual = c.loader_module("_session_ops").TurnReport(status="gate:keywords", metrics_on=True)
    c.ack_control(turn, revision=claim["revision"], report=actual.to_snapshot(), home=env)
    assert item in c.get_state(cap, home=env)["items"]
    queued = c.submit_ops(cap, {"epoch": 0, "operation_id": "op-one", "exclude": [item],
        "annotations": [annotation(env, cap, item)]}, home=env)
    assert queued["queued_revision"] == 1


def test_gate_removes_previous_supplement_from_browser_items_without_losing_scored_annotation(env):
    cap = attach(env)['capability']
    core = c.loader_module('_session_ops')
    first = ctx(env, turn='scored-supp')
    claim = c.claim_control(first, home=env)
    report = core.TurnReport(status='scored', metrics_on=True, prompt_h='a' * 16,
        injected=[{'path': 'notes/a.md', 'kind': 'admitted_list'}], supps=[{'path': 'notes/b.md'}])
    c.ack_control(first, revision=claim['revision'], report=report.to_snapshot(), home=env)
    before = c.get_state(cap, home=env)
    assert {v['kind'] for v in before['items'].values()} == {'admitted_list', 'llm_supplement'}
    gate = ctx(env, turn='gate-no-supp')
    claim = c.claim_control(gate, home=env)
    c.ack_control(gate, revision=claim['revision'], report=core.TurnReport(status='gate:keywords').to_snapshot(), home=env)
    after = c.get_state(cap, home=env)
    expected = {key:v for key,v in before['items'].items() if v['kind'] == 'admitted_list'}
    assert after['items'] == expected


@pytest.mark.parametrize("field", ["snapshot", "snapshot_ts", "items", "display"])
def test_capability_expiry_blocks_late_plaintext_report(env, monkeypatch, field):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(c, "time", SimpleNamespace(time=lambda: clock.now))
    cap = attach(env)["capability"]
    seed(env, cap)
    clock.now = 100 + c.CAPABILITY_TTL - 10
    c.get_state(cap, home=env)
    turn = ctx(env)
    claim = c.claim_control(turn, home=env)
    paths = list((env / ".context-vault/state/live/codex").glob("*.json"))
    assert len(paths) == 1
    before = json.loads(paths[0].read_text(encoding="utf-8"))
    clock.now += 11
    c.ack_control(turn, revision=claim["revision"],
        report=dict(report(), injected=[{"path": "notes/new.md", "kind": "admitted_list"}]), home=env)
    after = json.loads(paths[0].read_text(encoding="utf-8"))
    assert after[field] == before[field]


def test_session_start_and_user_prompt_keep_independent_visible_identities(env):
    cap = attach(env)["capability"]
    core = c.loader_module("_session_ops")
    for event, turn_id, ss, pins in (("SessionStart", "start", [{"path": "notes/shared.md"}], []),
        ("UserPromptSubmit", "pin", [], [{"path": "notes/shared.md", "rendered": True}]),
        ("UserPromptSubmit", "empty", [], [])):
        turn = replace(ctx(env, turn=turn_id), event=event)
        claim = c.claim_control(turn, home=env)
        actual = core.TurnReport(event=event, status="scored", session_start=ss, pins_user=pins)
        c.ack_control(turn, revision=claim["revision"], report=actual.to_snapshot(), home=env)
    items = c.get_state(cap, home=env)["items"]
    assert [item["path"] for item in items.values()] == ["notes/shared.md"]
    assert all(item["_event"] == "SessionStart" for item in items.values())
