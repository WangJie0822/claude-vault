"""展示合并以真实 TurnReport 产物为输入，避免与生产 schema 分叉。"""
from copy import deepcopy

import pytest

from context_vault import control


def test_display_retains_start_and_last_scored_across_gate_without_stale_supplements():
    from context_vault.panel_view import merge_display
    Report = control.loader_module('_session_ops').TurnReport
    start = Report(event='SessionStart', status='scored')
    start.session_start = [{'path': 'project/fixed.md', 'title': '固定上下文'}]
    display = merge_display(None, start.to_snapshot(), start.event)
    scored = Report(status='scored', metrics_on=True, prompt_h='a' * 16)
    scored.injected = [{'path': 'notes/a.md', 'kind': 'admitted_list', 'score': 9}]
    scored.pins_llm = [{'path': 'notes/a.md', 'kind': 'llm_pinned'}]
    scored.supps = [{'path': 'notes/b.md', 'kind': 'llm_supplement'}]
    scored.settings = {'effective': {'fine_rank': False}, 'locked': [], 'source': {'fine_rank': 'config'}}
    scored.thresholds = {'min_score': 6}
    display = merge_display(display, scored.to_snapshot(), 'UserPromptSubmit')
    before = deepcopy(display)
    gate = Report(status='gate:short_prompt', metrics_on=True, llm={'state': 'idle'})
    result = merge_display(display, gate.to_snapshot(), 'UserPromptSubmit')
    assert display == before, 'merge must not mutate previous state'
    assert result['session_start'] == start.session_start
    assert result['injected'] == scored.injected and result['pins']['llm'] == scored.pins_llm
    assert result['supps'] == [] and result['gate_now'] is True
    assert result['settings'] == scored.settings and result['thresholds'] == scored.thresholds
    assert result['llm'] == gate.llm and result['metrics_on'] is True
    assert result['prompt_h'] == 'a' * 16


def test_unknown_display_values_remain_unknown_and_threads_do_not_share_lists():
    from context_vault.panel_view import merge_display
    a = merge_display(None, {'status': 'gate:empty', 'settings': None}, 'UserPromptSubmit')
    assert a.get('settings') is None and a.get('llm') is None and a.get('metrics_on') is None
    b = merge_display(None, {'status': 'scored', 'injected': [{'path': 'b.md'}]}, 'UserPromptSubmit')
    assert a.get('injected') is None and b['injected'] == [{'path': 'b.md'}]


@pytest.mark.parametrize('event', ['SessionStart', 'UserPromptSubmit'])
@pytest.mark.parametrize('enabled', [True, False])
def test_current_metrics_policy_updates_without_changing_last_scored_target(event, enabled):
    from context_vault.panel_view import merge_display
    Report = control.loader_module('_session_ops').TurnReport
    scored = Report(status='scored', metrics_on=True, prompt_h='a' * 16)
    scored.injected = [{'path': 'notes/a.md', 'kind': 'admitted_list'}]
    previous = merge_display(None, scored.to_snapshot(), 'UserPromptSubmit')
    if enabled:
        previous = merge_display(previous,
            Report(status='gate:short_prompt', metrics_on=False).to_snapshot(), 'UserPromptSubmit')
        assert previous['metrics_on'] is False
    before = deepcopy(previous)
    latest = Report(event=event, status='gate:short_prompt', metrics_on=enabled)
    result = merge_display(previous, latest.to_snapshot(), event)
    assert result['metrics_on'] is enabled
    assert result['prompt_h'] == 'a' * 16
    assert result['injected'] == scored.injected
    assert previous == before
