"""执行面板实际 JS 的 DOM 行为；不替代真实浏览器验证。"""
from pathlib import Path
import json
import subprocess

import pytest

from context_vault.exe_search import which_no_cwd
from test_codex_panel_bootstrap import desktop, invoke

ROOT = Path(__file__).resolve().parents[1]


def test_panel_renders_queued_model_and_refresh_contract(tmp_path):
    from context_vault import control
    from context_vault.panel_view import merge_display
    Report = control.loader_module('_session_ops').TurnReport
    start = Report(event='SessionStart', status='scored', session_start=[{'path': 'fixed.md', 'title': '真实启动项'}])
    turn = Report(status='scored', metrics_on=True, prompt_h='a' * 16,
                  llm={'state': 'off', 'words': [], 'calls': 0},
                  injected=[{'path': 'coarse.md', 'kind': 'admitted_list', 'title': '真实粗排项'}],
                  pins_llm=[{'path': 'coarse.md', 'title': '真实点名项'}])
    items = {}
    display = None
    for report in (start, turn):
        snapshot = report.to_snapshot()
        display = merge_display(display, snapshot, report.event)
        items.update(control._snapshot_items(snapshot, report.event, epoch=1, turn_id='real-turn'))
    fixture = tmp_path / 'actual-turn-report.json'
    fixture.write_text(json.dumps({'epoch': 1, 'queued_revision': 0, 'applied_revision': 0,
        'items': items, 'display': display, 'intent': {}, 'snapshot': {'core': turn.to_snapshot()}}), encoding='utf-8')
    node = which_no_cwd("node")
    assert node, "panel UI checks require Node.js"
    result = subprocess.run([node, str(ROOT / "tests/panel-ui.cjs"),
                             str(ROOT / "context_vault/panel/panel.js"), str(fixture)],
                            capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS queued-vs-applied" in result.stdout


@pytest.mark.parametrize('intent_keys,undo_key', [
    (('unpin_llm',), 'unpin_llm'),
    (('exclude', 'unpin_llm'), 'exclude'),
    (('exclude', 'unpin_llm'), 'unpin_llm'),
    (('pin', 'unpin_llm'), 'pin'),
    (('pin',), 'pin'),
], ids=['unpin-alone', 'overlap-undo-exclude', 'overlap-undo-unpin',
        'unpin-with-pin-undo-pin', 'pin-alone'])
def test_panel_compact_intents_remain_visible_and_independently_editable(desktop, tmp_path, intent_keys, undo_key):
    """真实控制通道跨 compact 后，DOM 操作只撤销选中的意图，不复用旧标注。"""
    from context_vault import control
    from context_vault.runtime import HookContext, RuntimeKind

    home, _, _, config, ref = desktop
    settings = json.loads(config.read_text(encoding='utf-8'))
    settings['metrics'] = {'enabled': True}
    config.write_text(json.dumps(settings), encoding='utf-8')
    invoke(desktop, 'SessionStart')
    capability = control.attach(ref, home=home)['capability']
    ctx = HookContext.from_payload({'hook_event_name': 'UserPromptSubmit', 'session_id': ref.thread_id,
        'thread_id': ref.thread_id, 'turn_id': 'before-intent-compact', 'actor_kind': 'main'},
        runtime=RuntimeKind.CODEX)
    claim = control.claim_control(ctx, home=home)
    paths = ['notes/navigation.md', '项目笔记/panel-project/fixed.md']
    report = control.loader_module('_session_ops').TurnReport(status='scored', metrics_on=True,
        prompt_h='a' * 16, pins_llm=[{'path': path} for path in paths])
    control.ack_control(ctx, revision=claim['revision'], report=report.to_snapshot(), home=home)
    before = control.local_state(ref, home=home)
    ids = [next(key for key, item in before['items'].items()
                if item['path'] == path and item.get('kind') == 'llm_pinned') for path in paths]
    control.submit_ops(capability, {'epoch': before['epoch'], 'operation_id': 'queue-intents',
        **{key: ids for key in intent_keys}}, home=home)
    invoke(desktop, 'SessionStart', source='compact')
    state = control.local_state(ref, home=home)
    assert state['epoch'] == 1
    assert all(state['intent'][key] == paths for key in intent_keys)
    intent_items = {key: item for key, item in state['items'].items() if item.get('kind') == 'user_intent'}
    assert {item['path'] for item in intent_items.values()} == set(paths)
    assert not set(ids).intersection(intent_items)
    assert all('annotation_id' not in item and 'prompt_h' not in item for item in intent_items.values())
    fixture = tmp_path / 'compact-control-state.json'
    fixture.write_text(json.dumps({'state': state, 'undo_key': undo_key, 'path': paths[0]}), encoding='utf-8')
    node = which_no_cwd('node')
    assert node, 'panel UI checks require Node.js'
    result = subprocess.run([node, str(ROOT / 'tests/panel-ui.cjs'),
        str(ROOT / 'context_vault/panel/panel.js'), str(fixture), 'compact-intent'],
        capture_output=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    operations = json.loads(result.stdout)['operations']
    assert len(operations) == 1
    assert set(operations[0]) == {'epoch', 'operation_id', undo_key}
    control.submit_ops(capability, operations[0], home=home)
    after = control.local_state(ref, home=home)
    for key in intent_keys:
        assert after['intent'][key] == (paths[1:] if key == undo_key else paths)
    # 旧轮次身份和新 intent 身份均不能用来投递旧标注。
    new_id = next(key for key, item in intent_items.items() if item['path'] == paths[0])
    assert state['snapshot']['core']['metrics_on'] is True
    for identity, reason in ((ids[0], 'unknown note identity'), (new_id, 'stale annotation identity')):
        with pytest.raises(ValueError, match=reason):
            control.submit_ops(capability, {'epoch': state['epoch'], 'operation_id': 'reject-' + identity,
                'annotations': [{'item': identity, 'annotation_id': before['items'][ids[0]]['annotation_id'],
                                 'verdict': 'relevant'}]}, home=home)
    assert control.local_state(ref, home=home)['intent'] == after['intent']
