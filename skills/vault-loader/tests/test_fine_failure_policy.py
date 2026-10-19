# -*- coding: utf-8 -*-
"""精排失败停用（spec §6.2 / F53 / F66），取代 1.x 的「每个 TTL 窗口最多提炼一次」负缓存。

旧守卫 `test_session_topic_wiring.py::test_repeated_extraction_failure_does_not_spawn_every_round`
钉的是「失败后 TTL 窗口内不再 spawn」。2.4.0 起这条承诺退役：首轮失败下一轮继续走首轮模式，代价改由
「连续 2 次失败停用逐轮补充、30 分钟后恢复」「找不到 claude 停用到 state_ttl_hours 后重新探测」
「每会话调用 / 外发上限不恢复」收敛。从真实 UPS 入口跑，拦住 spawn 只数调用次数，不起真实子进程。
"""
from __future__ import annotations

import dataclasses
import importlib
import io
import json
import sys
import time
from pathlib import Path

import pytest

SID = "sess-fail-policy"


@pytest.fixture
def cwd(tmp_home: Path, tmp_vault: Path, write_frontmatter_cache) -> Path:
    write_frontmatter_cache({
        "技术笔记/quota.md": {"tags": ["配额", "fulltext"], "category": "技术笔记",
                              "summary": "fulltext 配额的实现与坑", "mtime": 1900000000},
        "技术笔记/other-a.md": {"tags": ["其它"], "category": "技术笔记",
                                "summary": "无关笔记 a 的摘要", "mtime": 1900000000},
        "技术笔记/other-b.md": {"tags": ["其它"], "category": "技术笔记",
                                "summary": "无关笔记 b 的摘要", "mtime": 1900000000},
    })
    for rel in ("技术笔记/quota.md", "技术笔记/other-a.md", "技术笔记/other-b.md"):
        p = tmp_vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# x", encoding="utf-8")
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}), encoding="utf-8")
    d = tmp_home.parent / "p-fail"
    d.mkdir()
    return d


def _patch_spawn(monkeypatch) -> list:
    """拦住精排 spawn。三个可能的引用点都替换（以 Task 17 实际导入为准），不起真实子进程。"""
    calls: list = []

    def _fake(*a, **k):
        calls.append(a)
        return "spawned"

    for mod in ("scripts._topic", "scripts.prompt_submit_load", "scripts._fine_flow"):
        m = importlib.import_module(mod)
        if hasattr(m, "spawn_fine"):
            monkeypatch.setattr(m, "spawn_fine", _fake)
    return calls


def _seed(cwd: Path, **fields) -> None:
    """首轮点名已成功（topics state=ok ⇒ 本轮走逐轮补充），fine 状态按参数预置。"""
    from scripts import _fine_state, _state
    from scripts._topic import save_session_topic
    _state.configure_context("claude")
    save_session_topic(cwd, SID, ["配额"], pinned=[], accounted=False, state="ok")
    assert _fine_state.update_fine(SID, lambda st: dataclasses.replace(st, **fields)) is not None


def _turn(monkeypatch, capsys, cwd: Path) -> None:
    import scripts.prompt_submit_load as P
    payload = {"cwd": str(cwd), "prompt": "先修 fulltext 配额 的实现",
               "session_id": SID, "prompt_id": "pid-F"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert P.main() == 0
    capsys.readouterr()


def _after():
    from scripts import _fine_state
    return _fine_state.load_fine(SID)


def test_cooldown_constant_is_the_spec_value():
    from scripts._fine_state import DISABLE_COOLDOWN_SEC, STREAK_LIMIT
    assert (STREAK_LIMIT, DISABLE_COOLDOWN_SEC) == (2, 1800)     # spec §6.2【初值】


def test_positive_control_spawns_when_healthy(cwd, monkeypatch, capsys):
    """阳性对照：没有失败记录时这一轮必须发起——否则下面几条「不发起」对任何实现都恒真。"""
    _seed(cwd)
    calls = _patch_spawn(monkeypatch)
    _turn(monkeypatch, capsys, cwd)
    assert len(calls) == 1


def test_single_failure_still_retries(cwd, monkeypatch, capsys):
    _seed(cwd, fail_streak=1, last_fail="timeout", last_ts=time.time() - 30)
    calls = _patch_spawn(monkeypatch)
    _turn(monkeypatch, capsys, cwd)
    assert len(calls) == 1, "失败 1 次不应停用：2.4.0 起不再有 TTL 负缓存"


def test_two_consecutive_failures_disable_supplement(cwd, monkeypatch, capsys):
    _seed(cwd, fail_streak=2, disabled_reason="streak", disabled_at=time.time() - 60)
    calls = _patch_spawn(monkeypatch)
    _turn(monkeypatch, capsys, cwd)
    assert calls == []


def test_streak_disable_recovers_after_cooldown(cwd, monkeypatch, capsys):
    # 字面量 1800 而不是 import DISABLE_COOLDOWN_SEC：引用常量会让「把冷却改成永不恢复」的变异自洽、不转红
    _seed(cwd, fail_streak=2, disabled_reason="streak", disabled_at=time.time() - 1800 - 5)
    calls = _patch_spawn(monkeypatch)
    _turn(monkeypatch, capsys, cwd)
    assert len(calls) == 1
    st = _after()
    assert (st.disabled_reason, st.fail_streak) == (None, 0)


def test_no_claude_disable_waits_for_ttl(cwd, monkeypatch, capsys):
    _seed(cwd, disabled_reason="no_claude", disabled_at=time.time() - 3600)
    calls = _patch_spawn(monkeypatch)
    _turn(monkeypatch, capsys, cwd)
    assert calls == []


def test_no_claude_disable_reprobes_after_ttl(cwd, monkeypatch, capsys):
    _seed(cwd, disabled_reason="no_claude", disabled_at=time.time() - 25 * 3600)
    calls = _patch_spawn(monkeypatch)
    _turn(monkeypatch, capsys, cwd)
    assert len(calls) == 1


@pytest.mark.parametrize("reason,extra", [("call_cap", {"calls": 20}),
                                          ("bytes_cap", {"bytes_sent": 1_000_000})])
def test_caps_never_recover(cwd, monkeypatch, capsys, reason, extra):
    """钉的是「上限停用后，即使恢复判据放行，上限仍拦住发起」，不是「`_recovery` 不处理上限」。

    预置同时带了 disabled_reason 和对应的计数（calls / bytes_sent）：停用原因门与计数门各拦一道，
    所以把 `_recovery` 末尾改成恒 `return True` 是等价变异，本用例不会转红——要验这条用例的判别力，
    得把 `spawn_blocked` 里的几道门一起拆（见变异 t22x-all-gates-open）。"""
    _seed(cwd, disabled_reason=reason, disabled_at=time.time() - 7 * 24 * 3600, **extra)
    calls = _patch_spawn(monkeypatch)
    _turn(monkeypatch, capsys, cwd)
    assert calls == []
