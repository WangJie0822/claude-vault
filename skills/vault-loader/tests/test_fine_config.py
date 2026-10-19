# -*- coding: utf-8 -*-
"""_fine_config：精排配置的校验单点、新旧键兼容、会话设置与 persist 只收窄
（spec 2.4.0 §5.1 / §5.2 / §5.3 / §10.3 / §10.4）。"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import _fine_config as fc
from scripts._config_loader import DEFAULT_CONFIG, load_config_ex
from scripts._topic import effective_pin_max


def _cfg(**rel) -> dict:
    c = deepcopy(DEFAULT_CONFIG)
    c["relevance"].update(rel)
    return c


def _load(tmp_path: Path, data) -> dict:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return load_config_ex(p).config


def test_new_keys_have_spec_defaults():
    rel = DEFAULT_CONFIG["relevance"]
    got = {k: rel[k] for k in (
        "coarse_inject", "fine_rank", "fine_supplement", "auto_fulltext", "fine_pin_max",
        "fine_max_notes", "fine_model", "fine_timeout_sec", "fine_max_calls",
        "fine_session_max_bytes", "fine_catalog", "fine_catalog_max_chars",
        "fine_recent_prompts", "fine_exclude_tags")}
    assert got == {
        "coarse_inject": True, "fine_rank": True, "fine_supplement": True, "auto_fulltext": False,
        "fine_pin_max": 1, "fine_max_notes": 3, "fine_model": "haiku", "fine_timeout_sec": 120,
        "fine_max_calls": 20, "fine_session_max_bytes": 1000000, "fine_catalog": True,
        "fine_catalog_max_chars": 80000, "fine_recent_prompts": 3, "fine_exclude_tags": ["private"]}


@pytest.mark.parametrize("key, value, expected", [
    pytest.param("fine_rank", True, (True, True), id="bool-ok"),
    pytest.param("fine_rank", 1, (False, True), id="bool-rejects-int"),
    pytest.param("fine_rank", "false", (False, True), id="bool-rejects-str"),
    pytest.param("fine_pin_max", 3, (True, 3), id="int-ok"),
    pytest.param("fine_pin_max", True, (False, 1), id="int-rejects-bool"),
    pytest.param("fine_pin_max", 9, (False, 8), id="int-clip-hi"),
    pytest.param("fine_pin_max", -2, (False, 0), id="int-clip-lo"),
    pytest.param("fine_pin_max", float("inf"), (False, 1), id="int-rejects-inf"),
    pytest.param("fine_pin_max", float("nan"), (False, 1), id="int-rejects-nan"),
    pytest.param("fine_pin_max", 2.0, (True, 2), id="int-float-integral"),
    pytest.param("fine_pin_max", 2.5, (False, 2), id="int-float-fraction"),
    pytest.param("fine_pin_max", "3", (False, 1), id="int-rejects-str"),
    pytest.param("fine_timeout_sec", 5, (False, 10), id="timeout-lo"),
    pytest.param("fine_session_max_bytes", 10 ** 12, (False, 20_000_000), id="bytes-hi"),
    pytest.param("user_prompt_submit.max_notes", 10, (False, 8), id="max-notes-clip"),
    pytest.param("fine_model", "haiku", (True, "haiku"), id="model-ok"),
    pytest.param("fine_model", "claude-opus-5[1m]", (True, "claude-opus-5[1m]"), id="model-brackets"),
    pytest.param("fine_model", "a" * 64, (True, "a" * 64), id="model-64"),
    pytest.param("fine_model", "haiku\n", (False, "haiku"), id="model-trailing-newline"),
    pytest.param("fine_model", "-x", (False, "haiku"), id="model-leading-dash"),
    pytest.param("fine_model", "a" * 65, (False, "haiku"), id="model-too-long"),
    pytest.param("fine_model", "a&b", (False, "haiku"), id="model-cmd-meta"),
    pytest.param("fine_model", 5, (False, "haiku"), id="model-not-str"),
    pytest.param("fine_exclude_tags", ["private", "", 3], (False, ["private"]), id="tags-filter"),
    # S-L1：失败方向是「多排除」——单个字符串当一个 tag，空串与其他类型回落默认，不归一成 []
    pytest.param("fine_exclude_tags", "secret", (False, ["secret"]), id="tags-str-single"),
    pytest.param("fine_exclude_tags", "  ", (False, ["private"]), id="tags-blank-str-default"),
    pytest.param("fine_exclude_tags", None, (False, ["private"]), id="tags-none-default"),
    pytest.param("fine_exclude_tags", {"private": True}, (False, ["private"]), id="tags-dict-default"),
    pytest.param("fine_exclude_tags", 3, (False, ["private"]), id="tags-int-default"),
    pytest.param("fine_exclude_tags", [], (True, []), id="tags-empty-list-explicit"),
    pytest.param("fine_exclude_tags", ["a"], (True, ["a"]), id="tags-ok"),
    pytest.param("nope", 1, (False, None), id="unknown"),
])
def test_validate_value(key, value, expected):
    assert fc.validate_value(key, value) == expected


@pytest.mark.parametrize("rel, on", [
    pytest.param({}, True, id="defaults"),
    pytest.param({"fine_rank": False}, False, id="new-false"),
    pytest.param({"session_topic": False}, False, id="old-false"),
    pytest.param({"fine_rank": True, "session_topic": False}, False, id="either-false"),
    pytest.param({"session_topic": 0}, False, id="old-truthiness"),
    pytest.param({"fine_rank": "no"}, True, id="new-garbage-default"),
])
def test_fine_rank_on(rel, on):
    assert fc.fine_rank_on({"relevance": rel}) is on


def test_fine_rank_on_garbage_shapes():
    assert fc.fine_rank_on(None) is True
    assert fc.fine_rank_on({"relevance": "x"}) is True


def test_fine_supplement_on_needs_both():
    assert fc.fine_supplement_on({"relevance": {}}) is True
    assert fc.fine_supplement_on({"relevance": {"fine_supplement": False}}) is False
    assert fc.fine_supplement_on({"relevance": {"fine_rank": False}}) is False
    assert fc.fine_supplement_on({"relevance": {"session_topic": False}}) is False


@pytest.mark.parametrize("rel, expected", [
    pytest.param({}, 1, id="defaults"),
    pytest.param({"fine_pin_max": 2}, 2, id="new"),
    pytest.param({"fine_pin_max": 2, "session_pin_max": 3}, 2, id="new-wins"),
    pytest.param({"fine_pin_max": 1, "session_pin_max": 3}, 3, id="new-default-old-nondefault"),
    pytest.param({"session_pin_max": False}, 0, id="old-false-ruling9"),
    pytest.param({"session_pin_max": "3"}, 3, id="old-numeric-str"),
    pytest.param({"fine_pin_max": True}, 1, id="new-bool-rejected"),
    pytest.param({"fine_pin_max": False, "session_pin_max": False}, 0, id="mirrored-false"),
    pytest.param({"fine_pin_max": 9}, 8, id="new-clipped"),
])
def test_pin_max_raw(rel, expected):
    assert fc.pin_max_raw({"relevance": rel}) == expected


def test_effective_pin_max_reads_new_key():
    assert effective_pin_max({"relevance": {"fine_pin_max": 3}}) == 3
    assert effective_pin_max({"relevance": {"fine_pin_max": 3, "session_pin_enabled": False}}) == 0


@pytest.mark.parametrize("data, rank, pin", [
    pytest.param({}, True, 1, id="defaults"),
    pytest.param({"relevance": {"session_topic": False}}, False, 1, id="old-rank"),
    pytest.param({"relevance": {"session_topic": False, "fine_rank": True}}, True, 1,
                 id="new-rank-beats-old"),
    pytest.param({"relevance": {"session_pin_max": 3}}, True, 3, id="old-pin"),
    pytest.param({"relevance": {"session_pin_max": 3, "fine_pin_max": 1}}, True, 1,
                 id="new-pin-1-beats-old"),
    pytest.param({"relevance": {"fine_pin_max": 2}}, True, 2, id="new-pin"),
    pytest.param({"relevance": "garbage"}, True, 1, id="relevance-not-dict"),
])
def test_load_config_resolves_superseded(tmp_path, data, rank, pin):
    """§5.3 优先级：新键显式 > 旧键显式 > 默认。`new-pin-1-beats-old` 是只有按原始 override 判断
    才分得出来的那一档：默认 1 与显式 1 在合并后的 dict 里不可区分（F34）。"""
    rel = _load(tmp_path, data)["relevance"]
    assert rel["fine_rank"] == rel["session_topic"]
    assert rel["fine_pin_max"] == rel["session_pin_max"]
    cfg = {"relevance": rel}
    assert fc.fine_rank_on(cfg) is rank
    assert fc.pin_max_raw(cfg) == pin


def test_missing_and_corrupt_config_are_mirrored(tmp_path):
    missing = load_config_ex(tmp_path / "absent" / "config.json").config["relevance"]
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    corrupt = load_config_ex(bad).config["relevance"]
    for rel in (missing, corrupt):
        assert rel["fine_rank"] is True and rel["session_topic"] is True
        assert rel["fine_pin_max"] == 1 and rel["session_pin_max"] == 1


@pytest.mark.parametrize("raw", [0, None, [], "no", 1, "true"], ids=repr)
def test_load_invalid_fine_rank_is_absent_not_mirrored(tmp_path, raw):
    """§5.2：新键 `fine_rank` 只认 bool，其它类型当缺失。此前 `fine_rank: 0` 经加载被镜像进
    `session_topic`，`fine_rank_on(已加载 config)` 读到旧键 0 ⇒ 关；同一个值直接构造 dict 读出来却是开——
    两条路径对同一输入给出相反结论（review Minor #1）。"""
    rel = _load(tmp_path, {"relevance": {"fine_rank": raw}})["relevance"]
    assert rel["fine_rank"] is True and rel["session_topic"] is True
    assert fc.fine_rank_on({"relevance": rel}) is True
    assert fc.fine_rank_on({"relevance": {"fine_rank": raw}}) is True      # 直读口径一致


def test_load_invalid_fine_rank_falls_back_to_old_key(tmp_path):
    rel = _load(tmp_path, {"relevance": {"fine_rank": 0, "session_topic": False}})["relevance"]
    assert rel["fine_rank"] is False and rel["session_topic"] is False
    assert fc.fine_rank_on({"relevance": rel}) is False


@pytest.mark.parametrize("rel_in, expected", [
    pytest.param({"fine_pin_max": "3"}, 1, id="str-absent-default"),
    pytest.param({"fine_pin_max": True}, 1, id="bool-absent-default"),
    pytest.param({"fine_pin_max": None}, 1, id="null-absent-default"),
    pytest.param({"fine_pin_max": [2]}, 1, id="list-absent-default"),
    pytest.param({"fine_pin_max": "3", "session_pin_max": 2}, 2, id="str-absent-old-key"),
    pytest.param({"fine_pin_max": True, "session_pin_max": 2}, 2, id="bool-absent-old-key"),
    pytest.param({"fine_pin_max": True, "session_pin_max": False}, 0, id="bool-absent-old-false"),
    pytest.param({"fine_pin_max": 9}, 8, id="clip-hi"),
    pytest.param({"fine_pin_max": 9, "session_pin_max": 3}, 8, id="clip-hi-beats-old"),
    pytest.param({"fine_pin_max": -2, "session_pin_max": 3}, 0, id="clip-lo-beats-old"),
    pytest.param({"fine_pin_max": 2.5}, 2, id="fraction"),
])
def test_load_pin_new_key_follows_52(tmp_path, rel_in, expected):
    """新键类型不合格 ⇒ 当缺失（回落旧键、再回落默认）且不镜像；类型合格但越界 / 有小数 ⇒ 取 §5.2 截断后的
    归一值（与 `pin_max_raw` 直读同值，`test_pin_max_raw[new-clipped]`）。每条同时断言加载路径与直读路径同值。"""
    rel = _load(tmp_path, {"relevance": rel_in})["relevance"]
    assert rel["fine_pin_max"] == rel["session_pin_max"]
    assert fc.pin_max_raw({"relevance": rel}) == expected
    assert fc.pin_max_raw({"relevance": rel_in}) == expected


@pytest.mark.parametrize("key, value, expected", [
    pytest.param("fine_rank", True, (True, True), id="bool-ok"),
    pytest.param("fine_rank", 0, (False, True), id="bool-key-non-bool"),
    pytest.param("fine_pin_max", 3, (True, 3), id="int-ok"),
    pytest.param("fine_pin_max", 9, (True, 8), id="int-clip-is-explicit"),
    pytest.param("fine_pin_max", 2.5, (True, 2), id="int-fraction-is-explicit"),
    pytest.param("fine_pin_max", True, (False, 1), id="int-bool-not-explicit"),
    pytest.param("fine_pin_max", "3", (False, 1), id="int-str-not-explicit"),
    pytest.param("fine_pin_max", float("nan"), (False, 1), id="int-nan-not-explicit"),
    pytest.param("fine_model", "bad model", (False, "haiku"), id="model-bad-not-explicit"),
    pytest.param("fine_exclude_tags", "x", (True, ["x"]), id="tags-str-is-explicit"),
    pytest.param("fine_exclude_tags", 5, (False, ["private"]), id="tags-not-list-not-explicit"),
    pytest.param("fine_exclude_tags", ["a", 1], (True, ["a"]), id="tags-filtered-explicit"),
    pytest.param("nope", 1, (False, None), id="unknown"),
])
def test_explicit_value(key, value, expected):
    """`explicit_value` 与 `validate_value` 共用同一个归一实现（单点），只多回答一件事：用户写的值
    是不是类型合格的显式设置（类型不合格 ⇒ 调用方当缺失处理）。"""
    assert fc.explicit_value(key, value) == expected


@pytest.mark.parametrize("raw, want", [
    pytest.param("secret", ["secret"], id="str"),
    pytest.param(None, ["private"], id="none"),
    pytest.param({"private": True}, ["private"], id="dict"),
    pytest.param(["a"], ["a"], id="list"),
])
def test_fine_exclude_tags_non_list_never_reads_empty(raw, want):
    """S-L1：手改 config 写成 `"fine_exclude_tags": "private"` 等非 list 值时，经 `get_value` 与会话设置
    （整轮只读它产出的 config）读到的都不是 `[]`——此前两处都归一成 `[]`，失败方向是「什么都不排除」。"""
    cfg = {"relevance": {"fine_exclude_tags": raw}}
    assert fc.get_value(cfg, "fine_exclude_tags") == want
    assert fc.apply_session_settings(cfg, {}).config["relevance"]["fine_exclude_tags"] == want


def test_resolve_superseded_is_reexported():
    from scripts import _config_loader
    assert fc.resolve_superseded is _config_loader.resolve_superseded


def test_session_settings_override_and_mirror():
    v = fc.apply_session_settings(_cfg(), {"fine_pin_max": 2, "coarse_inject": False,
                                           "user_prompt_submit.max_notes": 5})
    rel = v.config["relevance"]
    assert v.effective["fine_pin_max"] == 2
    assert rel["fine_pin_max"] == 2 and rel["session_pin_max"] == 2
    assert effective_pin_max(v.config) == 2
    assert v.config["user_prompt_submit"]["max_notes"] == 5
    assert rel["coarse_inject"] is False
    assert v.session == {"fine_pin_max": 2, "coarse_inject": False, "user_prompt_submit.max_notes": 5}
    assert v.source["fine_pin_max"] == "session"
    assert v.source["fine_model"] == "default"
    assert set(v.source) == set(fc.PANEL_KEYS) == set(v.effective) == set(v.base)
    assert v.rejected == [] and v.locked == []


def test_session_cannot_unlock_config_false():
    v = fc.apply_session_settings(_cfg(fine_rank=False, session_topic=False),
                                  {"fine_rank": True, "fine_supplement": False})
    assert v.locked == ["fine_rank"]
    assert v.effective["fine_rank"] is False
    assert {"what": "settings", "key": "fine_rank", "reason": "locked"} in v.rejected
    assert v.session == {"fine_supplement": False}
    assert fc.fine_rank_on(v.config) is False
    assert v.source["fine_rank"] == "config"


def test_session_settings_reject_unknown_and_bad_values():
    v = fc.apply_session_settings(_cfg(), {"fine_timeout_sec": 30, "fine_pin_max": True,
                                           "fine_model": "a b"})
    assert {(r["key"], r["reason"]) for r in v.rejected} == {
        ("fine_timeout_sec", "unknown"), ("fine_pin_max", "bad_value"), ("fine_model", "bad_value")}
    assert all(r["what"] == "settings" for r in v.rejected)
    assert v.session == {}


def test_session_settings_do_not_mutate_input():
    c = _cfg()
    before = deepcopy(c)
    fc.apply_session_settings(c, {"fine_pin_max": 3})
    assert c == before


def test_config_values_are_normalized_without_session():
    """F40：粗排篇数等配置值也在这一处截到区间，下游可直接下标读取。"""
    c = deepcopy(DEFAULT_CONFIG)
    c["user_prompt_submit"]["max_notes"] = 10
    c["relevance"]["fine_timeout_sec"] = 1
    c["relevance"]["fine_model"] = "bad model"
    v = fc.apply_session_settings(c, None)
    assert v.config["user_prompt_submit"]["max_notes"] == 8
    assert v.config["relevance"]["fine_timeout_sec"] == 10
    assert v.config["relevance"]["fine_model"] == "haiku"
    assert v.source["user_prompt_submit.max_notes"] == "config"
    assert v.session == {} and v.rejected == []


def test_session_non_dict_is_ignored():
    v = fc.apply_session_settings(_cfg(), ["fine_pin_max", 3])
    assert v.session == {} and v.rejected == []


def test_apply_fail_open_on_garbage_config():
    v = fc.apply_session_settings("x", {"fine_pin_max": 2})
    assert isinstance(v.config, dict)
    assert v.effective["fine_pin_max"] == 2


def _boom(*_a, **_k):
    raise RuntimeError("forced")


def test_failure_view_is_complete(monkeypatch, capsys):
    """异常兜底返回的 view 也要能被下游按键下标读取：此前 effective / base / source 全空，
    `view.effective["fine_pin_max"]` 直接 KeyError（review Minor #3）。会话值不应用，其余字段如实。"""
    cfg = _cfg(fine_pin_max=2)
    monkeypatch.setattr(fc, "_set", _boom)              # 只在 try 块里用到 ⇒ 走异常分支
    v = fc.apply_session_settings(cfg, {"fine_pin_max": 3, "fine_rank": False})
    assert set(v.effective) == set(v.base) == set(v.source) == set(fc.PANEL_KEYS)
    assert v.effective == v.base
    assert v.effective["fine_pin_max"] == 2             # 会话值未应用，config 的 2 保留
    assert v.source["fine_pin_max"] == "config" and v.source["fine_model"] == "default"
    assert v.session == {} and v.locked == [] and v.rejected == []
    assert v.config == cfg
    assert "会话设置应用失败" in capsys.readouterr().err


def test_failure_view_survives_broken_get_value(monkeypatch):
    """兜底分支自己读 config 也可能失败：退到 DEFAULT_CONFIG 的默认值，键集仍然完整。"""
    monkeypatch.setattr(fc, "validate_value", _boom)    # get_value 经它 ⇒ 主路径与兜底第一层都抛
    v = fc.apply_session_settings(_cfg(fine_pin_max=2), {})
    assert set(v.effective) == set(v.base) == set(v.source) == set(fc.PANEL_KEYS)
    assert v.effective == {k: fc.default_of(k) for k in fc.PANEL_KEYS}
    assert set(v.source.values()) == {"default"}
    assert v.session == {} and v.locked == []


@pytest.mark.parametrize("key, value, expected", [
    pytest.param("fine_rank", False, None, id="rank-off"),
    pytest.param("fine_rank", True, None, id="rank-on-noop"),
    pytest.param("auto_fulltext", True, "widen", id="fulltext-on"),
    pytest.param("auto_fulltext", False, None, id="fulltext-off"),
    pytest.param("fine_pin_max", 0, None, id="pin-down"),
    pytest.param("fine_pin_max", 2, "widen", id="pin-up"),
    pytest.param("user_prompt_submit.max_notes", 2, None, id="notes-down"),
    pytest.param("user_prompt_submit.max_notes", 4, "widen", id="notes-up"),
    pytest.param("fine_model", "haiku", None, id="model-haiku"),
    pytest.param("fine_model", "sonnet", "widen", id="model-other"),
    pytest.param("fine_timeout_sec", 30, "bad_key", id="not-panel"),
    pytest.param("fine_pin_max", "x", "bad_value", id="bad-value"),
])
def test_check_persist_only_narrows(key, value, expected):
    v = fc.apply_session_settings(_cfg(), {})
    assert fc.check_persist(key, value, v) == expected


def test_persist_judged_against_config_not_session():
    v = fc.apply_session_settings(_cfg(), {"user_prompt_submit.max_notes": 6})
    assert fc.check_persist("user_prompt_submit.max_notes", 6, v) == "widen"
    v2 = fc.apply_session_settings(_cfg(fine_model="sonnet"), {})
    assert fc.check_persist("fine_model", "sonnet", v2) is None
    assert fc.check_persist("fine_model", "opus", v2) == "widen"
