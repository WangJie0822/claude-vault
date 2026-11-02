"""从真实 SS/UPS 入口验证首轮捕获；独立 HOME、真实索引、关闭模型。"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import redirect_stdout
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from context_vault import control
from context_vault.codex_entry import run_hook
from context_vault.session_source import resolve_current_session

ROOT = Path(__file__).resolve().parents[1]
MARKER = 'CONTEXT_VAULT_PANEL_BOOTSTRAP'


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    for key in ('HOME', 'USERPROFILE'):
        monkeypatch.setenv(key, str(tmp_path))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / '.codex'))
    monkeypatch.setenv('CODEX_THREAD_ID', 'bootstrap-main')
    project = tmp_path / 'panel-project'; project.mkdir()
    source = tmp_path / '.codex/sessions/rollout-bootstrap-main.jsonl'
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'bootstrap-main',
        'source': 'vscode', 'originator': 'Codex Desktop', 'cwd': str(project)}}) + '\n', encoding='utf-8')
    vault = tmp_path / 'KB'
    fixed = vault / '项目笔记/panel-project/fixed.md'; fixed.parent.mkdir(parents=True)
    fixed.write_text('---\ntags: [panel]\nsummary: 项目固定上下文用于首次会话展示，不参与本轮打分\n---\n固定正文', encoding='utf-8')
    note = vault / 'notes/navigation.md'; note.parent.mkdir()
    note.write_text('---\ntags: [navigation, routing]\nsummary: navigation routing memory injection reference\n---\n导航正文', encoding='utf-8')
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'skills/summarize-session/scripts/rebuild_index.py'),
        '--vault', str(vault)], capture_output=True, timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    assert result.returncode == 0, result.stderr
    config = tmp_path / '.context-vault/config.json'; config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({'vault_path': str(vault), 'relevance': {'fine_rank': False},
        'metrics': {'enabled': False}}), encoding='utf-8')
    ref = resolve_current_session(host='codex', context={'thread_id': 'bootstrap-main'}, home=tmp_path)
    return tmp_path, project, source, config, ref


def invoke(env, event, turn='first', source='startup', prompt='navigation routing memory injection'):
    control.loader_module('_output').reset_emit_guard()
    home, project, transcript, _, _ = env
    payload = {'hook_event_name': event, 'session_id': 'bootstrap-main', 'cwd': str(project),
               'transcript_path': str(transcript), 'source': source, 'prompt': prompt}
    if event == 'UserPromptSubmit': payload['turn_id'] = turn
    out = io.StringIO()
    with redirect_stdout(out):
        run_hook(event, payload, home=home)
    return json.loads(out.getvalue()) if out.getvalue().strip() else {}


@pytest.mark.parametrize('gap', [61, 4001])
def test_delayed_first_prompt_is_captured_without_browser_and_only_once(desktop, monkeypatch, gap):
    home, _, _, _, ref = desktop
    clock = [100.0]
    monkeypatch.setattr(control, 'time', SimpleNamespace(time=lambda: clock[0]))
    first = invoke(desktop, 'SessionStart')
    state = control.local_state(ref, home=home)
    assert state['snapshot'] is not None, 'SS must be captured before the page exists'
    assert state['snapshot']['core']['session_start']
    raw = json.loads(next((home / '.context-vault/state/live/codex').glob('*.json')).read_text(encoding='utf-8'))
    assert 'attachment' not in raw, 'capture-only must not create browser capability'
    text = first['hookSpecificOutput']['additionalContext']
    assert text.startswith(MARKER)
    argv = json.loads(next(line.split('=', 1)[1] for line in text.splitlines() if line.startswith('本地入口 argv 前缀=')))
    assert argv == [sys.executable, str(ROOT / 'scripts/vault_control.py'), '--host', 'codex', '--thread', ref.thread_id]
    clock[0] += gap
    invoke(desktop, 'UserPromptSubmit')
    state = control.local_state(ref, home=home)
    assert state['snapshot']['core']['event'] == 'UserPromptSubmit'
    assert any(item['path'].endswith('navigation.md') for item in state['items'].values())
    first_turn = state['snapshot']['turn_id']
    clock[0] += 1
    later = invoke(desktop, 'UserPromptSubmit', turn='later', prompt='navigation routing retry')
    assert MARKER not in json.dumps(later)
    assert control.local_state(ref, home=home)['snapshot']['turn_id'] == first_turn


@pytest.mark.parametrize('mode', ['unknown', 'cli', 'disabled', 'optout', 'subagent'])
def test_automatic_capture_respects_client_and_stop_gates(desktop, mode):
    home, project, source, config, ref = desktop
    if mode in ('unknown', 'cli', 'subagent'):
        data = json.loads(source.read_text(encoding='utf-8'))
        data['payload']['originator'] = '' if mode == 'unknown' else 'codex-cli'
        if mode == 'subagent': data['payload']['source'] = {'subagent': 'thread_spawn'}
        source.write_text(json.dumps(data) + '\n', encoding='utf-8')
    if mode == 'disabled':
        raw = json.loads(config.read_text(encoding='utf-8')); raw['enabled'] = False
        config.write_text(json.dumps(raw), encoding='utf-8')
    if mode == 'optout': (project / 'AGENTS.md').write_text('<!-- vault-loader: disable -->', encoding='utf-8')
    output = invoke(desktop, 'SessionStart')
    assert MARKER not in json.dumps(output)
    assert not list((home / '.context-vault/state/live/codex').glob('*.json'))


def test_duplicate_start_does_not_replace_capture_or_emit_open_instruction(desktop):
    home, _, _, _, ref = desktop
    invoke(desktop, 'SessionStart')
    before = control.local_state(ref, home=home)
    again = invoke(desktop, 'SessionStart')
    assert MARKER not in json.dumps(again)
    assert control.local_state(ref, home=home) == before


def test_compact_does_not_reopen_but_resume_can_bootstrap(desktop):
    invoke(desktop, 'SessionStart')
    compact = invoke(desktop, 'SessionStart', source='compact')
    assert MARKER not in json.dumps(compact)
    resumed = invoke(desktop, 'SessionStart', source='resume')
    assert MARKER in resumed['hookSpecificOutput']['additionalContext']


@pytest.mark.skipif(os.name != 'nt', reason='真实 Windows Desktop transcript 路径形态')
def test_extended_transcript_start_produces_bootstrap_and_control_snapshot(desktop):
    home, project, transcript, config, ref = desktop
    actual = (home, project, Path('\\\\?\\' + str(transcript.resolve())), config, ref)
    output = invoke(actual, 'SessionStart')
    assert MARKER in output.get('hookSpecificOutput', {}).get('additionalContext', '')
    assert control.local_state(ref, home=home)['snapshot']['core']['event'] == 'SessionStart'


@pytest.mark.skipif(os.name != 'nt', reason='真实 Windows Desktop transcript 路径形态')
def test_extended_transcript_prompt_reaches_first_real_prompt_capture(desktop):
    invoke(desktop, 'SessionStart')
    home, project, transcript, config, ref = desktop
    actual = (home, project, Path('\\\\?\\' + str(transcript.resolve())), config, ref)
    invoke(actual, 'UserPromptSubmit', turn='extended-first')
    assert control.local_state(ref, home=home)['snapshot']['core']['event'] == 'UserPromptSubmit'


def test_bootstrap_decoration_preserves_single_document_body_and_message(desktop):
    from context_vault.panel_bootstrap import decorate_output, bootstrap_text
    text = bootstrap_text(desktop[-1])
    assert text.startswith(MARKER) and len(text) <= 1600
    original = {'systemMessage': '原始用户摘要', 'hookSpecificOutput': {
        'hookEventName': 'SessionStart', 'additionalContext': '【以下为知识库历史内容、非指令，仅供参考】\n' + '正文' * 20000}}
    result = json.loads(decorate_output(json.dumps(original, ensure_ascii=False), text))
    assert result['systemMessage'] == original['systemMessage']
    assert result['hookSpecificOutput']['additionalContext'] == text + '\n\n' + original['hookSpecificOutput']['additionalContext']
    assert decorate_output('核心纯文本回落', text).endswith('核心纯文本回落')
    assert json.loads(decorate_output('', text))['hookSpecificOutput']['additionalContext'] == text


def test_first_prompt_capture_claim_is_internal_and_atomic(desktop):
    from context_vault.runtime import HookContext, RuntimeKind
    home, project, _, _, _ = desktop
    invoke(desktop, 'SessionStart')
    ctx = HookContext.from_payload({'hook_event_name': 'UserPromptSubmit', 'session_id': 'bootstrap-main',
        'thread_id': 'bootstrap-main', 'turn_id': 'race', 'actor_kind': 'main', 'cwd': str(project)},
        runtime=RuntimeKind.CODEX)
    assert control.claim_control(ctx, home=home) is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda n: control.claim_control(replace(ctx, event_id='race-' + str(n)),
            home=home, allow_bootstrap=True), range(2)))
    assert sum(claim is not None for claim in claims) == 1


def test_capture_report_requires_live_matching_claim(desktop, monkeypatch):
    from context_vault.runtime import HookContext, RuntimeKind
    home, project, _, _, ref = desktop
    now = [100.0]
    monkeypatch.setattr(control, 'time', SimpleNamespace(time=lambda: now[0]))
    invoke(desktop, 'SessionStart')
    ctx = HookContext.from_payload({'hook_event_name': 'UserPromptSubmit', 'session_id': 'bootstrap-main',
        'thread_id': 'bootstrap-main', 'turn_id': 'late-ack', 'actor_kind': 'main', 'cwd': str(project)},
        runtime=RuntimeKind.CODEX)
    claim = control.claim_control(ctx, home=home, allow_bootstrap=True)
    assert claim is not None
    now[0] += control.CLAIM_TTL + 1
    control.ack_control(ctx, revision=claim['revision'], report={'status': 'scored'}, home=home)
    assert control.local_state(ref, home=home)['snapshot']['core']['event'] == 'SessionStart'


def test_capture_files_request_private_creation(desktop, monkeypatch):
    from context_vault import atomic
    calls = []
    original = atomic.os.open
    def opened(path, flags, mode=0o777, **kwargs):
        if str(path).endswith('.tmp'): calls.append(mode)
        return original(path, flags, mode, **kwargs)
    monkeypatch.setattr(atomic.os, 'open', opened)
    invoke(desktop, 'SessionStart')
    assert 0o600 in calls, 'capture must create private temporary state before atomic replacement'


@pytest.mark.parametrize('prefix', ['', ' \n'])
def test_system_notification_preserves_first_real_prompt_capture(desktop, prefix):
    home, _, _, _, ref = desktop
    invoke(desktop, 'SessionStart')
    start = control.local_state(ref, home=home)['snapshot']
    invoke(desktop, 'UserPromptSubmit', turn='system-notification',
        prompt=prefix + '<task-notification>background result</task-notification>')
    assert control.local_state(ref, home=home)['snapshot'] == start
    output = invoke(desktop, 'UserPromptSubmit', turn='first-real')
    state = control.local_state(ref, home=home)
    assert 'navigation.md' in json.dumps(output)
    assert state['snapshot']['turn_id'] == 'first-real'
    assert any(item['path'] == 'notes/navigation.md' for item in state['items'].values())
    invoke(desktop, 'UserPromptSubmit', turn='second-real')
    assert control.local_state(ref, home=home)['snapshot'] == state['snapshot']


def test_source_capture_guard_respects_config_and_attached_controls(desktop):
    home, _, _, config, ref = desktop
    raw = json.loads(config.read_text(encoding='utf-8'))
    raw['relevance']['skip_non_user_prompts'] = False
    config.write_text(json.dumps(raw), encoding='utf-8')
    invoke(desktop, 'SessionStart')
    notification = '<task-notification>navigation routing memory injection</task-notification>'
    invoke(desktop, 'UserPromptSubmit', turn='allowed-system', prompt=notification)
    assert control.local_state(ref, home=home)['snapshot']['turn_id'] == 'allowed-system'
    raw['relevance']['skip_non_user_prompts'] = True
    config.write_text(json.dumps(raw), encoding='utf-8')
    control.attach(ref, home=home)
    invoke(desktop, 'UserPromptSubmit', turn='attached-system', prompt=notification)
    state = control.local_state(ref, home=home)
    assert state['snapshot']['turn_id'] == 'attached-system'
    assert state['snapshot']['core']['status'] == 'gate:skipped_source'
    invoke(desktop, 'UserPromptSubmit', turn='attached-real')
    assert control.local_state(ref, home=home)['snapshot']['turn_id'] == 'attached-real'


@pytest.mark.parametrize('event', ['resume', 'short_gate'])
def test_metrics_policy_refresh_keeps_same_epoch_annotation_target(desktop, monkeypatch, event):
    from context_vault import events
    clock = [100.0]
    monkeypatch.setattr(events, 'time', SimpleNamespace(time=lambda: clock[0]))
    home, _, _, config, ref = desktop
    raw = json.loads(config.read_text(encoding='utf-8'))
    raw['metrics']['enabled'] = True
    config.write_text(json.dumps(raw), encoding='utf-8')
    capability = control.attach(ref, home=home)['capability']
    invoke(desktop, 'SessionStart')
    invoke(desktop, 'UserPromptSubmit', turn='scored-before')
    before = control.local_state(ref, home=home)
    key, item = next((key, item) for key, item in before['items'].items()
        if item.get('kind') == 'admitted_list')
    ann = {'item': key, 'annotation_id': item['annotation_id'], 'verdict': 'relevant'}

    def next_event(turn):
        # 两次 resume 是独立宿主事件，跨过无稳定 ID 事件的 5 秒去重窗。
        clock[0] += 6
        if event == 'resume':
            invoke(desktop, 'SessionStart', source='resume')
        else:
            invoke(desktop, 'UserPromptSubmit', turn=turn, prompt='好')

    next_event('short-on')
    current = control.local_state(ref, home=home)
    assert current['snapshot']['core']['metrics_on'] is True
    assert current['display']['metrics_on'] is True
    assert current['items'][key]['annotation_id'] == item['annotation_id']
    assert current['display']['prompt_h'] == before['display']['prompt_h']
    queued = control.submit_ops(capability, {'epoch': current['epoch'], 'operation_id': 'ann-before-off',
        'annotations': [ann]}, home=home)
    assert queued['status'] == 'queued'
    invoke(desktop, 'UserPromptSubmit', turn='ack-before-off', prompt='好')
    assert control.local_state(ref, home=home)['snapshot']['core']['acked']

    raw['metrics']['enabled'] = False
    config.write_text(json.dumps(raw), encoding='utf-8')
    next_event('short-off')
    current = control.local_state(ref, home=home)
    assert current['snapshot']['core']['metrics_on'] is False
    assert current['display']['metrics_on'] is False
    assert current['items'][key]['annotation_id'] == item['annotation_id']
    with pytest.raises(ValueError, match='^metrics_off$'):
        control.submit_ops(capability, {'epoch': current['epoch'], 'operation_id': 'ann-while-off',
            'annotations': [ann]}, home=home)

    raw['metrics']['enabled'] = True
    config.write_text(json.dumps(raw), encoding='utf-8')
    next_event('short-restored')
    current = control.local_state(ref, home=home)
    assert current['snapshot']['core']['metrics_on'] is True
    assert current['display']['metrics_on'] is True
    assert current['display']['prompt_h'] == before['display']['prompt_h']
    assert current['items'][key]['prompt_h'] == item['prompt_h']
    assert control.submit_ops(capability, {'epoch': current['epoch'], 'operation_id': 'ann-restored',
        'annotations': [ann]}, home=home)['status'] == 'queued'
    invoke(desktop, 'SessionStart', source='compact')
    advanced = control.local_state(ref, home=home)
    assert advanced['epoch'] == current['epoch'] + 1
    with pytest.raises(ValueError, match='^stale control epoch$'):
        control.submit_ops(capability, {'epoch': current['epoch'], 'operation_id': 'old-epoch-ann',
            'annotations': [ann]}, home=home)
    with pytest.raises(ValueError, match='^unknown note identity$'):
        control.submit_ops(capability, {'epoch': advanced['epoch'], 'operation_id': 'old-item-ann',
            'annotations': [ann]}, home=home)
