"""与 Claude absorb 对齐的展示合并；不授予授权、不保存笔记正文。"""
from copy import deepcopy


def merge_display(previous, report, event):
    result = deepcopy(previous) if isinstance(previous, dict) else {}
    for field in ('settings', 'llm', 'vault_root'):
        if report.get(field) is not None:
            result[field] = deepcopy(report[field])
    # 当前策略随每轮刷新，标注目标仍保留最近打分轮的 prompt_h 与条目。
    if type(report.get('metrics_on')) is bool:
        result['metrics_on'] = report['metrics_on']
    if event == 'SessionStart':
        result['session_start'] = deepcopy(report.get('session_start', []))
        return result
    status = report.get('status')
    result['status'] = status
    result['gate_now'] = isinstance(status, str) and status.startswith('gate:')
    result['supps'] = deepcopy(report.get('supps', []))
    result['rejected'] = deepcopy(report.get('rejected', []))
    if status in ('scored', 'dry_run'):
        for field in ('injected', 'pins', 'excluded', 'thresholds', 'prompt_h'):
            result[field] = deepcopy(report.get(field))
    return result
