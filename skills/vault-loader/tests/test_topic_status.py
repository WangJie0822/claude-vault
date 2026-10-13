# -*- coding: utf-8 -*-
"""LLM 精排状态：topics entry 的 state / fail、失败原因旁路、发起结果、describe_session_topic
（spec 2026-10-12 §3.2–3.3）。"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from scripts import _topic
from scripts._state import session_topics_path
from scripts._topic import (PENDING_GRACE_SEC, TOPIC_TIMEOUT_SEC, describe_session_topic,
                            run_extraction_child, save_session_topic, spawn_topic_extraction,
                            take_spawn_outcome)

ON = {"relevance": {"session_topic": True, "session_pin_max": 1},
      "user_prompt_submit": {"state_ttl_hours": 24}}
OFF = {"relevance": {"session_topic": False}}
KEYS = {"state", "fail", "words", "in_scoring", "n_pinned", "age_s", "pin_on", "stale_pins"}
NOW = time.time()
LOST_AGE = TOPIC_TIMEOUT_SEC + PENDING_GRACE_SEC + 1


def _mkrepo(root: Path) -> Path:
    g = root / ".git"
    g.mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def _stdin() -> str:
    return json.dumps({"prompt": "q", "candidates": [["a.md", "A"]], "pin_max": 1}, ensure_ascii=False)


def _entry(cwd: Path, sid: str = "s1") -> dict:
    return json.loads(session_topics_path(cwd).read_text(encoding="utf-8"))["topics"][sid]


def _set_entry(cwd: Path, sid: str = "s1", **fields) -> None:
    """直接写 topics 文件，模拟旧版本写出或被篡改的 entry。"""
    p = session_topics_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"topics": {sid: fields}}, ensure_ascii=False), encoding="utf-8")


def _d(cwd: Path, config=ON, **kw) -> dict:
    kw.setdefault("now", NOW)
    return describe_session_topic(cwd, "s1", config, **kw)


class _FakePopen:
    def __init__(self, *a, **kw):
        self.stdin = mock.MagicMock()


def test_placeholder_is_pending_and_child_success_is_ok(tmp_path, monkeypatch):
    cwd = _mkrepo(tmp_path / "p")
    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", _FakePopen)
    assert spawn_topic_extraction(cwd, "s1", "q", [], ON) is True
    assert take_spawn_outcome() == "spawned"
    assert take_spawn_outcome() is None                       # 取走即清空
    assert _entry(cwd)["state"] == "pending"
    monkeypatch.setattr(_topic, "_call_model", lambda text: "PINNED: 1\nWORDS: 精排")
    run_extraction_child([str(cwd), "s1"], stdin_text=_stdin())
    e = _entry(cwd)
    assert e["state"] == "ok" and "fail" not in e


def test_spawn_outcome_no_claude_and_spawn_error(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: None)
    assert spawn_topic_extraction(tmp_path, "s1", "q", [], ON) is False
    assert take_spawn_outcome() == "no_claude"

    def _boom(*a, **kw):
        raise OSError("spawn failed")

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", _boom)
    assert spawn_topic_extraction(tmp_path, "s2", "q", [], ON) is False
    assert take_spawn_outcome() == "spawn_error"


@pytest.mark.parametrize("reason", ["timeout", "exit_nonzero", "no_claude"])
def test_child_failure_records_reason(tmp_path, monkeypatch, reason):
    cwd = _mkrepo(tmp_path / "p")

    def fake(text):
        _topic._LAST_FAIL = reason
        return None

    monkeypatch.setattr(_topic, "_call_model", fake)
    run_extraction_child([str(cwd), "s1"], stdin_text=_stdin())
    e = _entry(cwd)
    assert (e["state"], e["fail"]) == ("failed", reason)


def test_stub_without_reason_records_error(tmp_path, monkeypatch):
    """现有替身只返回 None、不设 _LAST_FAIL：按 error 记，且不能读到上一次调用残留的原因。"""
    cwd = _mkrepo(tmp_path / "p")
    monkeypatch.setattr(_topic, "_LAST_FAIL", "timeout")      # 上一次调用的残留
    monkeypatch.setattr(_topic, "_call_model", lambda text: None)
    run_extraction_child([str(cwd), "s1"], stdin_text=_stdin())
    assert _entry(cwd)["fail"] == "error"


def test_placeholder_does_not_inherit_ok(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"], pinned=["a.md"], accounted=False, state="ok")
    save_session_topic(cwd, "s1", [], state="pending")
    e = _entry(cwd)
    assert e["state"] == "pending" and "fail" not in e
    assert e["pinned"] == ["a.md"]                            # pinned 照旧按 _KEEP 继承，由 stale_pins 标出


class _R:
    def __init__(self, rc: int, out: str = ""):
        self.returncode, self.stdout = rc, out


@pytest.mark.parametrize("which,mode,expect", [
    (None, "", "no_claude"),
    ("/usr/bin/claude", "rc1", "exit_nonzero"),
    ("/usr/bin/claude", "timeout", "timeout"),
    ("/usr/bin/claude", "oserror", "error"),
])
def test_call_model_sets_last_fail(monkeypatch, which, mode, expect):
    def fake_run(*a, **kw):
        if mode == "rc1":
            return _R(1)
        if mode == "timeout":
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
        raise OSError("x")

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: which)
    monkeypatch.setattr("scripts._topic.subprocess.run", fake_run)
    monkeypatch.setattr(_topic, "_LAST_FAIL", None)
    assert _topic._call_model("p") is None
    assert _topic._LAST_FAIL == expect


def test_describe_keys_are_constant(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    assert set(_d(cwd, config=OFF)) == KEYS
    assert set(_d(cwd)) == KEYS
    _set_entry(cwd, words=["精排"], ts=NOW - 5, state="ok")
    assert set(_d(cwd)) == KEYS


def test_describe_off_idle_unavailable(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    assert _d(cwd, config=OFF)["state"] == "off"
    assert _d(cwd)["state"] == "idle"
    d = _d(cwd, spawn_outcome="no_claude")
    assert (d["state"], d["fail"]) == ("unavailable", "no_claude")
    d = _d(cwd, spawn_outcome="spawn_error")
    assert (d["state"], d["fail"]) == ("unavailable", "spawn_error")


def test_describe_pending_then_lost(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    _set_entry(cwd, words=[], ts=NOW - 10, state="pending")
    d = _d(cwd)
    assert (d["state"], d["age_s"], d["fail"]) == ("pending", 10, None)
    _set_entry(cwd, words=[], ts=NOW - LOST_AGE, state="pending")
    d = _d(cwd)
    assert (d["state"], d["fail"]) == ("failed", "lost")


def test_describe_ok_counts_pins_within_pin_max(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    _set_entry(cwd, words=["精排", "排序"], pinned=["a.md", "b.md"], ts=NOW - 5, state="ok")
    d = _d(cwd)
    assert (d["state"], d["words"], d["fail"]) == ("ok", ["精排", "排序"], None)
    assert d["n_pinned"] == 1                                 # 按 effective_pin_max（session_pin_max=1）收口
    assert d["stale_pins"] is False and d["pin_on"] is True


def test_describe_failed_reason_whitelist(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    _set_entry(cwd, words=[], ts=NOW - 5, state="failed", fail="timeout")
    assert _d(cwd)["fail"] == "timeout"
    _set_entry(cwd, words=[], ts=NOW - 5, state="failed", fail="<script>")
    assert _d(cwd)["fail"] == "error"


def test_describe_tampered_state_and_words(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    _set_entry(cwd, words="不是列表", pinned="也不是", ts=NOW - 5, state="hacked")
    d = _d(cwd)
    assert (d["state"], d["fail"], d["words"], d["stale_pins"]) == ("failed", "error", [], False)


def test_describe_future_ts_gives_zero_age(tmp_path):
    """时钟容差（`_TS_SKEW_SEC`）之内的未来 ts 照常读，年龄按 0 计。"""
    cwd = _mkrepo(tmp_path / "p")
    _set_entry(cwd, words=["精排"], ts=time.time() + 60, state="ok")
    assert _d(cwd)["age_s"] == 0


def test_far_future_ts_is_invalid(tmp_path):
    """超过时钟容差的未来 ts 视为无效：否则伪造的条目永不过期（full-review S-3）。"""
    cwd = _mkrepo(tmp_path / "p")
    _set_entry(cwd, words=["精排"], ts=time.time() + 3600, state="ok")
    assert _d(cwd)["state"] == "idle"
    assert _topic.has_recent_topic_attempt(cwd, "s1", 24) is False


@pytest.mark.parametrize("lit", ["Infinity", "-Infinity", "NaN", "1e400", "1e300", "true"])
def test_describe_never_raises_on_bad_ts(tmp_path, lit):
    """非有限、超大或布尔的 ts：describe 不抛、键恒定、按「没有记录」处理（full-review Q1 / S-2）。"""
    cwd = _mkrepo(tmp_path / "p")
    p = session_topics_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"topics": {"s1": {"words": ["精排"], "ts": %s, "state": "ok"}}}' % lit,
                 encoding="utf-8")
    d = _d(cwd)
    assert set(d) == KEYS
    assert (d["state"], d["age_s"]) == ("idle", None)


def test_pending_timing_constants_are_pinned():
    """宽限边界用例的期望值写成字面量，常量被改时这里先红（full-review T-M4）。"""
    assert (TOPIC_TIMEOUT_SEC, PENDING_GRACE_SEC) == (120, 30)


@pytest.mark.parametrize("age,expect", [
    (121, ("pending", None)),        # 已过 claude 超时、仍在宽限内
    (150, ("pending", None)),        # 宽限边界
    (151, ("failed", "lost")),
])
def test_describe_pending_grace_boundary(tmp_path, age, expect):
    cwd = _mkrepo(tmp_path / "p")
    _set_entry(cwd, words=[], ts=NOW - age, state="pending")
    d = _d(cwd)
    assert (d["state"], d["fail"]) == expect


def test_describe_expired_entry_is_idle(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    _set_entry(cwd, words=["精排"], ts=time.time() - 25 * 3600, state="ok")
    assert _d(cwd)["state"] == "idle"


@pytest.mark.parametrize("rel", [
    {"session_topic": True, "session_pin_max": 0},
    {"session_topic": True, "session_pin_enabled": False},
])
def test_describe_pin_off(tmp_path, rel):
    assert _d(_mkrepo(tmp_path / "p"), config={"relevance": rel})["pin_on"] is False


def test_describe_stale_pins_while_failed(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    _set_entry(cwd, words=[], pinned=["a.md"], ts=NOW - 5, state="failed", fail="timeout")
    d = _d(cwd)
    assert d["stale_pins"] is True and d["n_pinned"] is None


def test_describe_dry_run_never_spawns(tmp_path):
    """dry_run 下主流程永不发起提炼：没有记录时报 idle + dry_run，而不是「等第一个有召回的提问」
    （full-review A1）；已有记录时照常报那份记录。"""
    cwd = _mkrepo(tmp_path / "p")
    dry = {**ON, "dry_run": True}
    d = _d(cwd, config=dry)
    assert (d["state"], d["fail"]) == ("idle", "dry_run")
    _set_entry(cwd, words=["精排"], ts=NOW - 5, state="ok")
    assert _d(cwd, config=dry)["state"] == "ok"


def test_describe_switch_default_matches_spawn_gate(tmp_path):
    """开关缺键时与 spawn 门禁同一默认（`rel_cfg.get("session_topic", False)`）：门禁不发起，面板就不能报 idle。"""
    assert _d(_mkrepo(tmp_path / "p"), config={"relevance": {}})["state"] == "off"


def test_describe_outputs_stay_within_wire_enums(tmp_path):
    """describe 产出的 state / fail 不超出模块常量 LLM_STATES / LLM_FAILS（跨语言 parity 的 Python 一侧）。"""
    cwd = _mkrepo(tmp_path / "p")
    seen = [_d(cwd, config=OFF), _d(cwd), _d(cwd, config={**ON, "dry_run": True}),
            _d(cwd, spawn_outcome="no_claude"), _d(cwd, spawn_outcome="spawn_error")]
    for fields in ({"state": "pending"}, {"state": "ok", "words": ["精排"]},
                   {"state": "failed", "fail": "timeout"}, {"state": "hacked"}):
        _set_entry(cwd, ts=NOW - 5, **fields)
        seen.append(_d(cwd))
    _set_entry(cwd, words=[], ts=NOW - LOST_AGE, state="pending")
    seen.append(_d(cwd))
    assert {d["state"] for d in seen} <= set(_topic.LLM_STATES)
    assert {d["fail"] for d in seen} - {None} <= set(_topic.LLM_FAILS)
    assert {d["state"] for d in seen} == set(_topic.LLM_STATES)       # 每个取值都走到过


def test_new_session_survives_forged_future_entries(tmp_path):
    """32 条伪造的未来条目不能把刚写入的会话挤出去，否则 spawn 门禁每轮重开、每轮付费（full-review S-3）。"""
    cwd = _mkrepo(tmp_path / "p")
    p = session_topics_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    far = time.time() + 10 * 365 * 86400
    p.write_text(json.dumps({"topics": {f"f{i}": {"words": [], "ts": far} for i in range(32)}}),
                 encoding="utf-8")
    save_session_topic(cwd, "s1", [], state="pending")
    assert _topic.has_recent_topic_attempt(cwd, "s1", 24) is True
    topics = json.loads(p.read_text(encoding="utf-8"))["topics"]
    assert not any(k.startswith("f") for k in topics)               # 无效条目在写入时清掉


def test_eviction_never_drops_the_just_written_session(tmp_path):
    """条数淘汰与字节裁剪同一不变量：刚写入的这条永不被淘汰（容差内的未来 ts 也挤不掉它）。"""
    cwd = _mkrepo(tmp_path / "p")
    p = session_topics_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    soon = time.time() + 200                                        # 在 _TS_SKEW_SEC 之内，仍是有效条目
    p.write_text(json.dumps({"topics": {f"f{i}": {"words": [], "ts": soon} for i in range(32)}}),
                 encoding="utf-8")
    save_session_topic(cwd, "s1", [], state="pending")
    topics = json.loads(p.read_text(encoding="utf-8"))["topics"]
    assert "s1" in topics and len(topics) == _topic.MAX_TOPIC_SESSIONS
    assert _topic.has_recent_topic_attempt(cwd, "s1", 24) is True


def test_describe_legacy_entries(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    _set_entry(cwd, words=["精排"], ts=NOW - 5)
    assert _d(cwd)["state"] == "ok"
    _set_entry(cwd, words=[], ts=NOW - 5)
    assert _d(cwd)["state"] == "pending"
    _set_entry(cwd, words=[], ts=NOW - LOST_AGE)
    d = _d(cwd)
    assert (d["state"], d["fail"]) == ("failed", "lost")


def test_describe_stale_pins_while_pending(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    _set_entry(cwd, words=[], pinned=["a.md"], ts=NOW - 5, state="pending")
    d = _d(cwd)
    assert d["stale_pins"] is True and d["n_pinned"] is None


def test_failed_then_placeholder_self_heals(tmp_path):
    """子进程秒失败先写 failed、父进程随后写 pending 占位覆盖它：超时后必须判结果丢失（节点 1 T1）。"""
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", [], state="failed", fail="no_claude")
    save_session_topic(cwd, "s1", [], state="pending")
    ts = _entry(cwd)["ts"]
    d = describe_session_topic(cwd, "s1", ON, now=ts + LOST_AGE)
    assert (d["state"], d["fail"]) == ("failed", "lost")
