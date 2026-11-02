"""性能预算契约：Windows重标定、其它平台原值、中位数与越界拦截。"""
from __future__ import annotations

import ast
import inspect
import sys

import pytest

from tests.integration import test_perf as perf


def _api():
    assert callable(getattr(perf, "_budget_seconds", None)), "缺少统一平台预算入口"
    assert callable(getattr(perf, "_assert_within_budget", None)), "缺少可验证的预算断言"
    return perf._budget_seconds, perf._assert_within_budget


@pytest.mark.parametrize("platform,kind,expected", [
    ("win32", "startup", 0.75), ("win32", "prompt", 0.65), ("win32", "metrics", 0.75),
    ("linux", "startup", 0.5), ("linux", "prompt", 0.3), ("linux", "metrics", 0.5),
    ("darwin", "startup", 0.5), ("darwin", "prompt", 0.3), ("darwin", "metrics", 0.5),
])
def test_platform_budgets_are_explicit(platform, kind, expected):
    choose, _ = _api()
    assert choose(kind, platform) == expected


def test_default_budget_uses_actual_platform():
    choose, _ = _api()
    expected = (0.75, 0.65, 0.75) if sys.platform == "win32" else (0.5, 0.3, 0.5)
    assert tuple(choose(k) for k in ("startup", "prompt", "metrics")) == expected


@pytest.mark.parametrize("platform,kind,bound", [
    ("win32", "prompt", 0.65), ("win32", "startup", 0.75), ("win32", "metrics", 0.75),
    ("linux", "prompt", 0.3), ("darwin", "startup", 0.5),
])
def test_budget_boundary_is_strict_and_diagnostic(platform, kind, bound):
    _, check = _api()
    check([bound - 0.001] * 7, kind, "boundary", platform=platform)
    for sample in (bound, bound + 0.001):
        with pytest.raises(AssertionError, match="budget="):
            check([sample] * 7, kind, "boundary", platform=platform)


def test_single_spike_keeps_median_of_seven():
    _, check = _api()
    check([0.4] * 6 + [9.0], "prompt", "spike", platform="win32")


def test_large_regression_is_still_rejected():
    _, check = _api()
    with pytest.raises(AssertionError, match="budget="):
        check([1.0] * 7, "prompt", "regression", platform="win32")


@pytest.mark.parametrize("count", [0, 6, 8])
def test_partial_or_warmup_included_samples_are_rejected(count):
    _, check = _api()
    with pytest.raises(AssertionError, match="样本数"):
        check([0.1] * count, "prompt", "incomplete", platform="win32")


def test_sampling_policy_is_preserved():
    assert perf.WARMUP == 1 and perf.SAMPLES == 7


@pytest.mark.parametrize("name,kind", [
    ("test_session_start_under_500ms", "startup"),
    ("test_prompt_submit_under_300ms", "prompt"),
    ("test_prompt_submit_with_mod_request_under_300ms", "prompt"),
    ("test_prompt_submit_with_metrics_enabled_stays_within_budget", "metrics"),
    ("test_prompt_submit_steady_state_with_fine_under_300ms", "prompt"),
    ("test_prompt_submit_steady_state_with_large_fine_payload_under_300ms", "prompt"),
    ("test_first_turn_topic_spawn_overhead_bounded", "prompt"),
])
def test_each_real_entry_checks_its_samples_unconditionally(name, kind):
    """逐入口钉直接接线；定义、注释或其它入口的调用不能满足本条。"""
    body = ast.parse(inspect.getsource(getattr(perf, name))).body[0].body
    calls = [node.value for node in body
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
             and isinstance(node.value.func, ast.Name)
             and node.value.func.id == "_assert_within_budget"]
    assert len(calls) == 1, f"{name} 缺少无条件预算接线"
    call = calls[0]
    assert (isinstance(call.args[0], ast.Name) and call.args[0].id == "samples"
            and isinstance(call.args[1], ast.Constant) and call.args[1].value == kind), name
    assert body[-1] is next(node for node in body
                            if isinstance(node, ast.Expr) and node.value is call), name
