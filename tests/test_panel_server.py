"""真实随机回环端口与协议状态；不调用模型。"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading

import pytest

from context_vault import control, panel_server
from context_vault.runtime import HookContext, RuntimeKind
from context_vault.session_source import resolve_current_session


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    source = tmp_path / ".codex/sessions/rollout-example-thread-a.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-a", "source": "vscode", "cwd": str(tmp_path)}}) + "\n", encoding="utf-8")
    ref = resolve_current_session(host="codex", context={"thread_id": "thread-a"}, home=tmp_path)
    cap = control.attach(ref, home=tmp_path)["capability"]
    server = panel_server.create_server(capability=cap, thread_id="thread-a", home=tmp_path)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    yield server, cap, tmp_path
    server.shutdown(); server.server_close(); worker.join(timeout=5)


def request(panel, method="GET", path="/api/state", *, token=True, thread="thread-a", origin=None, host=None, body=None):
    server, cap, _ = panel
    port = server.server_address[1]
    headers = {"Host": host or f"127.0.0.1:{port}", "Origin": origin or f"http://127.0.0.1:{port}",
               "X-Context-Vault-Thread": thread}
    if token:
        headers["Authorization"] = "Bearer " + cap
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse(); data = response.read(); status = response.status
    result_headers = dict(response.getheaders()); conn.close()
    return status, data, result_headers


def test_state_requires_capability_origin_host_and_exact_thread(panel):
    assert request(panel, token=False)[0] == 401
    assert request(panel, origin="https://untrusted.invalid")[0] == 403
    assert request(panel, host="untrusted.invalid")[0] == 403
    assert request(panel, thread="thread-b")[0] == 403
    status, data, headers = request(panel)
    assert status == 200 and json.loads(data)["thread_id"] == "thread-a"
    assert panel[1].encode() not in data
    assert headers["Cache-Control"] == "no-store"


def test_queue_is_not_reported_as_applied_and_bad_operations_are_rejected(panel):
    status, data, _ = request(panel, "POST", "/api/ops", body=json.dumps({"epoch": 0, "operation_id": "set-one", "settings": {"fine_max_notes": 1}}))
    assert status == 200 and json.loads(data) == {"status": "queued", "queued_revision": 1}
    state = json.loads(request(panel)[1])
    assert state["queued_revision"] == 1 and state["applied_revision"] == 0
    for extra in ({"path": "../../secret"}, {"settings": {"unknown": True}}, {"exclude": ["made-up-id"]}):
        assert request(panel, "POST", "/api/ops", body=json.dumps({"epoch": 0, "operation_id": "bad-one", **extra}))[0] == 400
    assert request(panel, "POST", "/api/ops", body="x" * 100001)[0] == 413


def test_static_assets_cannot_traverse_and_never_embed_capability(panel):
    for path in ("/", "/panel.js", "/panel.css"):
        status, data, headers = request(panel, path=path, token=False)
        assert status == 200 and data
        assert panel[1].encode() not in data
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    for path in ("/../control.py", "/%2e%2e/control.py", "/api/state?capability=wrong", "/arbitrary"):
        assert request(panel, path=path, token=False)[0] in (400, 401, 404)


def test_startup_rejects_other_thread_even_with_valid_capability(panel):
    with pytest.raises(ValueError, match="thread"):
        panel_server.create_server(capability=panel[1], thread_id="thread-b", home=panel[2])
