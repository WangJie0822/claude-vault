"""捕获合并默认模型冒充用户选择、优先级颠倒与跨宿主授权泄漏。"""
from copy import deepcopy

import pytest

from context_vault.host_config import effective_host_config, resolve_model_selection


@pytest.mark.parametrize("raw,task,session,want,source", [
    ({}, None, None, None, "codex-native"),
    ({"relevance": {"fine_model": "configured-codex-model"}}, None, None,
     "configured-codex-model", "legacy-explicit"),
    ({"relevance": {"fine_model": "haiku"}}, None, None, "haiku", "legacy-explicit"),
    ({"runtimes": {"codex": {"model": "Org/Custom.Model"}},
      "relevance": {"fine_model": "legacy"}}, None, None, "Org/Custom.Model", "runtime"),
    ({"runtimes": {"codex": {"model": "runtime"}}}, None, "session", "session", "session"),
    ({"runtimes": {"codex": {"model": "runtime"}}}, "task", "session", "task", "task"),
    ({"runtimes": {"codex": {"model": "", "profile": "", "model_provider": ""}}},
     None, None, None, "codex-native"),
])
def test_model_precedence(raw, task, session, want, source):
    selected = resolve_model_selection(raw, host="codex", task_model=task, session_model=session)
    assert (selected.backend, selected.model, selected.source) == ("codex", want, source)


def test_profile_and_provider_survive_task_model_override():
    selected = resolve_model_selection({"runtimes": {"codex": {
        "profile": "work", "model_provider": "company-gateway"}}}, host="codex", task_model="custom")
    assert (selected.model, selected.profile, selected.provider) == ("custom", "work", "company-gateway")


@pytest.mark.parametrize("bad", [True, 12, [], None, "-flag", "a\nb", "x & whoami", "x\"y", ""])
def test_explicit_invalid_legacy_model_is_rejected_not_replaced(bad):
    with pytest.raises(ValueError, match="model"):
        resolve_model_selection({"relevance": {"fine_model": bad}}, host="codex")


@pytest.mark.parametrize("field", ["profile", "model_provider"])
def test_invalid_provider_or_profile_is_rejected_even_with_task_override(field):
    with pytest.raises(ValueError, match=field):
        resolve_model_selection({"runtimes": {"codex": {field: "x; command"}}},
                                host="codex", task_model="valid")


@pytest.mark.parametrize("model", [None, "haiku", "sonnet", "opus", "claude-custom"])
def test_claude_model_compatibility(model):
    raw = {} if model is None else {"relevance": {"fine_model": model}}
    selected = resolve_model_selection(raw, host="claude")
    assert (selected.backend, selected.model, selected.profile, selected.provider) == (
        "claude", model or "haiku", None, None)


def _base():
    return {"enabled": True, "relevance": {
        "fine_model": "haiku", "fine_rank": True, "session_topic": True,
        "fine_supplement": True, "fine_catalog": True, "fine_recent_prompts": 3,
        "min_topical_score": 4}, "runtimes": {"codex": {"enabled": True}}}


def test_codex_native_model_and_no_inherited_claude_model_authorization():
    cfg = effective_host_config({}, host="codex", base=_base())
    assert cfg["relevance"]["fine_model"] is None
    assert cfg["relevance"]["fine_rank"] is False
    assert cfg["relevance"]["session_topic"] is False
    assert cfg["relevance"]["fine_supplement"] is False
    assert cfg["relevance"]["min_topical_score"] == 4


def test_explicit_codex_opt_in_and_overrides_leave_input_and_claude_unchanged():
    raw = {"runtimes": {"codex": {"model": "my-model", "relevance": {
        "fine_rank": True, "fine_supplement": True, "fine_catalog": False,
        "fine_recent_prompts": 1}}}}
    base, original = _base(), deepcopy(_base())
    cfg = effective_host_config(raw, host="codex", base=base)
    assert cfg["relevance"]["fine_rank"] is True
    assert cfg["relevance"]["session_topic"] is True
    assert cfg["relevance"]["fine_model"] == "my-model"
    assert cfg["relevance"]["fine_catalog"] is False
    assert cfg["relevance"]["fine_recent_prompts"] == 1
    assert base == original
    assert effective_host_config(raw, host="claude", base=base) == original


@pytest.mark.parametrize("disabled", ["global", "host", "rank", "legacy", "supplement"])
def test_disable_wins_over_codex_opt_in(disabled):
    base = _base()
    raw = {"runtimes": {"codex": {"relevance": {"fine_rank": True, "fine_supplement": True}}}}
    if disabled == "global":
        base["enabled"] = False
    elif disabled == "host":
        raw["runtimes"]["codex"]["enabled"] = False
    else:
        base["relevance"][{"rank": "fine_rank", "legacy": "session_topic",
                            "supplement": "fine_supplement"}[disabled]] = False
    cfg = effective_host_config(raw, host="codex", base=base)
    assert cfg["relevance"]["fine_supplement"] is False
    if disabled != "supplement":
        assert cfg["relevance"]["fine_rank"] is False
        assert cfg["relevance"]["session_topic"] is False


@pytest.mark.parametrize("raw", [None, [], {"runtimes": []}, {"runtimes": {"codex": "bad"}}])
def test_malformed_input_cannot_enable_paid_path(raw):
    cfg = effective_host_config(raw, host="codex", base=_base())
    assert cfg["relevance"]["fine_rank"] is False
    assert cfg["relevance"]["fine_supplement"] is False


def test_unknown_host_is_rejected():
    with pytest.raises(ValueError, match="host"):
        resolve_model_selection({}, host="../elsewhere")


@pytest.mark.parametrize("value,expected", [(None, 3), (True, 3), ("2", 3), (-1, 0), (1, 1), (99, 8)])
def test_codex_ups_count_is_scoped_and_legacy_default_is_inherited(value, expected):
    base = {**_base(), "user_prompt_submit": {"max_notes": 3}}
    raw = {"runtimes": {"codex": {"user_prompt_submit": {"max_notes": value}}}}
    assert effective_host_config(raw, host="codex", base=base)["user_prompt_submit"]["max_notes"] == expected
    assert effective_host_config(raw, host="claude", base=base)["user_prompt_submit"]["max_notes"] == 3
    assert base["user_prompt_submit"]["max_notes"] == 3
