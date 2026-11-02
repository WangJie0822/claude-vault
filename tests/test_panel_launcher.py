"""应用内启动：临时 HOME、真实回环服务，无模型或系统浏览器。"""
import http.client
import importlib
import importlib.util
import json
import io
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
import webbrowser
from concurrent.futures import ThreadPoolExecutor

import pytest

from context_vault import control, panel_server
from context_vault.session_source import resolve_current_session


@pytest.fixture
def identity(tmp_path, monkeypatch):
    for key in ('HOME', 'USERPROFILE'):
        monkeypatch.setenv(key, str(tmp_path))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / '.codex'))
    for sid in ('panel-a', 'panel-b'):
        path = tmp_path / '.codex/sessions' / ('rollout-' + sid + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': sid, 'source': 'vscode', 'originator': 'Codex Desktop', 'cwd': str(tmp_path)}}) + '\n', encoding='utf-8')
    return tmp_path, resolve_current_session(host='codex', context={'thread_id': 'panel-a'}, home=tmp_path)


def get(port, path, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('GET', path, headers=headers or {})
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    conn.close()
    return result


def test_originator_comes_from_verified_first_metadata(identity):
    home, ref = identity
    assert getattr(ref, 'originator', '') == 'Codex Desktop'
    with ref.transcript_path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'type': 'session_meta', 'payload': {'id': 'panel-a', 'originator': 'other'}}) + '\n')
    assert resolve_current_session(host='codex', context={'thread_id': 'panel-a'}, home=home).originator == 'Codex Desktop'


def test_ticket_exchange_is_single_use_and_not_anonymous_authorization(identity):
    home, ref = identity
    cap = control.attach(ref, home=home)['capability']
    server = panel_server.create_server(capability=cap, thread_id=ref.thread_id, home=home,
                                       launch_ticket='synthetic-launch-ticket')
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        port = server.server_port
        assert get(port, '/api/state')[0] == 401
        assert get(port, '/open/synthetic-launch-ticket', {'Origin': 'https://foreign.invalid'})[0] == 403
        status, headers, body = get(port, '/open/synthetic-launch-ticket')
        assert status == 303 and headers['Location'] == '/#' + cap and body == b''
        assert headers['Cache-Control'] == 'no-store' and headers['Referrer-Policy'] == 'no-referrer'
        assert get(port, '/open/synthetic-launch-ticket')[0] == 410
        assert get(port, '/api/state')[0] == 401
        assert get(port, '/api/state', {'Authorization': 'Bearer ' + cap, 'X-Context-Vault-Thread': 'panel-b'})[0] == 403
        assert get(port, '/api/state', {'Authorization': 'Bearer ' + cap, 'X-Context-Vault-Thread': 'panel-a'})[0] == 200
        for path in ('/', '/panel.js', '/health'):
            assert cap.encode() not in get(port, path)[2]
        state = control.local_state(ref, home=home)
        assert 'synthetic-launch-ticket' not in json.dumps(state)
        assert cap not in json.dumps(state)
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)


def test_only_one_concurrent_request_can_consume_ticket(identity):
    home, ref = identity
    cap = control.attach(ref, home=home)['capability']
    server = panel_server.create_server(capability=cap, thread_id=ref.thread_id, home=home, launch_ticket='race')
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            calls = [pool.submit(get, server.server_port, '/open/race') for _ in range(4)]
            statuses = [call.result(timeout=10)[0] for call in calls]
        assert sorted(statuses) == [303, 410, 410, 410]
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)


def test_expired_ticket_does_not_grant_access(identity):
    home, ref = identity
    cap = control.attach(ref, home=home)['capability']
    server = panel_server.create_server(capability=cap, thread_id=ref.thread_id, home=home, launch_ticket='expired')
    server.ticket_deadline = time.monotonic() - 1
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    try:
        assert get(server.server_port, '/open/expired')[0] == 410
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)


def test_prepare_launches_real_child_without_exposing_capability(identity, monkeypatch):
    home, ref = identity
    assert importlib.util.find_spec('context_vault.panel_launcher') is not None, 'missing in-app launcher'
    launcher = importlib.import_module('context_vault.panel_launcher')
    children = []
    commands = []
    original = launcher.subprocess.Popen
    def record(*args, **kwargs):
        commands.append(args[0])
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(launcher.subprocess, 'Popen', record)
    monkeypatch.setattr(webbrowser, 'open', lambda *_: pytest.fail('must not open system browser'))
    result = launcher.prepare_panel(ref, home)
    parsed = urlsplit(result['url'])
    cap = None
    try:
        assert result['status'] == 'prepared' and result['thread_id'] == ref.thread_id
        assert parsed.hostname == '127.0.0.1' and parsed.path.startswith('/open/')
        assert not parsed.query and not parsed.fragment
        status, headers, _ = get(parsed.port, parsed.path)
        assert status == 303
        cap = headers['Location'].split('#', 1)[1]
        assert cap not in json.dumps(result)
        assert cap not in json.dumps(commands)
        state = control.local_state(ref, home=home)
        assert state['panel_base_url'] == 'http://127.0.0.1:' + str(parsed.port) + '/'
        saved = ''.join(p.read_text(encoding='utf-8') for p in (home / '.context-vault').rglob('*.json'))
        assert cap not in saved and parsed.path[6:] not in saved
    finally:
        if cap:
            conn = http.client.HTTPConnection('127.0.0.1', parsed.port, timeout=5)
            conn.request('POST', '/api/close', '{}', {'Authorization': 'Bearer ' + cap,
                'X-Context-Vault-Thread': ref.thread_id, 'Origin': 'http://127.0.0.1:' + str(parsed.port),
                'Content-Type': 'application/json'})
            conn.getresponse().read(); conn.close()
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


def test_prepare_failure_is_sanitized_and_detaches(identity, monkeypatch):
    home, ref = identity
    launcher = importlib.import_module('context_vault.panel_launcher')
    def fail(*args, **kwargs):
        raise OSError('synthetic-secret-must-not-leak')
    monkeypatch.setattr(launcher.subprocess, 'Popen', fail)
    with pytest.raises(ValueError, match='^panel preparation failed$'):
        launcher.prepare_panel(ref, home)
    path = next((home / '.context-vault/state/live/codex').glob('*.json'))
    assert json.loads(path.read_text(encoding='utf-8'))['attachment']['lease_until'] == 0


@pytest.mark.parametrize('failure', ['terminate', 'wait'])
def test_cleanup_failure_cannot_keep_attachment_or_replace_public_error(identity, monkeypatch, failure):
    home, ref = identity
    launcher = importlib.import_module('context_vault.panel_launcher')
    class Child:
        stdin = io.BytesIO()
        stdout = io.BytesIO(b'not-json')
        killed = False
        def poll(self): return None
        def terminate(self):
            if failure == 'terminate': raise OSError('termination denied')
        def wait(self, timeout):
            raise launcher.subprocess.TimeoutExpired('child', timeout)
        def kill(self): self.killed = True
    child = Child()
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: child)
    with pytest.raises(ValueError, match='^panel preparation failed$'):
        launcher.prepare_panel(ref, home)
    path = next((home / '.context-vault/state/live/codex').glob('*.json'))
    assert json.loads(path.read_text(encoding='utf-8'))['attachment']['lease_until'] == 0
    assert child.stdout.closed and child.killed


def test_unresponsive_reader_is_not_closed_under_its_buffer_lock(identity, monkeypatch):
    home, ref = identity
    launcher = importlib.import_module('context_vault.panel_launcher')
    release, finished = threading.Event(), threading.Event()
    class Reader:
        closed = False
        def readline(self, size):
            try:
                release.wait(3)
                return b''
            finally: finished.set()
        def close(self):
            assert finished.is_set(), 'closing a buffer with an active reader can wait without a bound'
            self.closed = True
    class Child:
        stdin = io.BytesIO()
        stdout = Reader()
        def poll(self): return None
        def terminate(self): raise OSError('synthetic termination denied')
        def kill(self): raise OSError('synthetic kill denied')
    child = Child()
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: child)
    monkeypatch.setattr(launcher, 'READY_TIMEOUT', 0.01)
    try:
        with pytest.raises(ValueError, match='^panel preparation failed$'):
            launcher.prepare_panel(ref, home)
        assert child.stdout.closed is False
    finally:
        release.set()
        assert finished.wait(1)
