# -*- coding: utf-8 -*-
"""UPS 侧精排编排 `_fine_flow`（spec 2026-10-13 §4、§6.1、§6.4、§7）的判定与读写。

用例直接读写 fine 状态文件（conftest 已把 HOME 隔离到临时目录、命名空间复位到 legacy），
所以 begin_turn「只读不写」、finish_turn「一次条件写」都按落盘结果断言，不看内部变量。
"""
from __future__ import annotations

import copy
import os
import time

import pytest

from scripts import _fine_config, _fine_flow, _fine_state
from scripts._config_loader import DEFAULT_CONFIG
from scripts._fine_state import FineState
from scripts._frontmatter_reader import Entry

SID = "sess-flow"
NOW = 1_800_000_000.0


def _cfg(**rel):
    c = copy.deepcopy(DEFAULT_CONFIG)
    c["relevance"].update(rel)
    return c


def _seed(**fields):
    """整份替换成给定字段的新状态（其余字段取 FineState 默认值）。"""
    _fine_state.update_fine(SID, lambda _st: FineState(**fields))


def _begin(cfg=None, request=None, now=NOW):
    return _fine_flow.begin_turn(SID, cfg or _cfg(), request, now=now)


def _blocked(tf, **kw):
    args = dict(gate_passed=True, dry_run=False, agent_type=None, n_candidates=3, now=NOW)
    args.update(kw)
    return _fine_flow.spawn_blocked(tf, **args)


def _supp(src, paths=("a.md",), ts=NOW - 5, gen="g1"):
    return {"gen_id": gen, "src_seq": src, "paths": list(paths), "ts": ts}


def _file_bytes(p):
    return p.read_bytes() if p.exists() else None


def _corrupt_marker(mtime):
    """空 .inflight：内容读不出，`_marker_read` 按 mtime + DEFAULT_MARKER_TIMEOUT 合成一份（corrupt）。"""
    p = _fine_state.inflight_path(SID)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    os.utime(p, (mtime, mtime))
    return p


def _boom(*_a, **_k):
    raise RuntimeError("boom")


def _e(path, tags=(), summary="", keywords=()):
    return Entry(path=path, tags=tuple(tags), summary=summary, keywords=tuple(keywords))


# ── begin_turn：只读、序号、会话设置 ─────────────────────────────────────────

def test_begin_turn_increments_seq_without_writing():
    _seed(turn_seq=4)
    tf = _begin()
    assert tf.turn_seq == 5 and tf.turn_ts == NOW
    assert _fine_state.load_fine(SID).turn_seq == 4          # begin_turn 不写盘


def test_begin_turn_empty_session_is_inert():
    tf = _fine_flow.begin_turn("", _cfg(), None, now=NOW)
    assert tf.turn_seq == 1 and tf.supp_candidates == [] and tf.session_id == ""
    _fine_flow.finish_turn(tf)
    d = _fine_state.fine_dir()
    assert not d.exists() or not any(d.glob("*.json"))      # 空会话不落任何 fine 文件


def test_begin_turn_read_error_degrades(monkeypatch):
    def boom(_sid):
        raise OSError("disk")

    monkeypatch.setattr(_fine_state, "load_fine", boom)
    tf = _begin()
    assert tf.turn_seq == 1 and "fine_read" in tf.degraded


def test_settings_request_overrides_stored_and_absent_falls_back():
    _seed(session_settings={"coarse_inject": False})
    assert _begin(request=None).view.session == {"coarse_inject": False}     # mod 没发：沿用已存
    assert _begin(request={}).view.session == {}                              # mod 发了空：清空
    assert _begin(request=None).view.config["relevance"]["coarse_inject"] is False


def test_begin_turn_leaves_fine_file_and_marker_untouched():
    """§7：begin_turn 只读。本轮要清的补充、要放的过期标记都留给 finish_turn，begin 前后两个文件逐字节相同。"""
    _seed(turn_seq=3, supp=_supp(3), session_settings={"coarse_inject": False})
    assert _fine_state.claim_inflight(SID, "gx", 10, NOW - 1000)
    fine_before = _file_bytes(_fine_state.fine_path(SID))
    marker_before = _file_bytes(_fine_state.inflight_path(SID))
    assert fine_before and marker_before                          # 探针：两个文件确实在
    tf = _begin()
    assert tf.supp_gen == "g1" and tf.lost_gen == "gx"           # 两条会触发清理的路径都走到了
    assert _file_bytes(_fine_state.fine_path(SID)) == fine_before
    assert _file_bytes(_fine_state.inflight_path(SID)) == marker_before


def test_begin_turn_survives_non_dict_user_prompt_submit():
    """config 的 user_prompt_submit 是字符串：`_ttl_hours` 回落默认 24 小时，不抛。"""
    _seed(disabled_reason="no_claude", disabled_at=NOW - 10 ** 6)
    cfg = _cfg()
    cfg["user_prompt_submit"] = "oops"
    tf = _begin(cfg)
    assert isinstance(tf, _fine_flow.TurnFine) and tf.recovered is True


def test_begin_turn_recovery_failure_degrades(monkeypatch):
    _seed(fail_streak=2, disabled_reason="streak", disabled_at=NOW - 3600)
    monkeypatch.setattr(_fine_flow, "_recovery", _boom)
    tf = _begin()
    assert tf.recovered is False and "recovery" in tf.degraded


def test_begin_turn_supp_judge_failure_degrades_without_half_judgement(monkeypatch):
    """`_judge_supp` 已经记下 supp_gen 之后才抛：不能把半截判定留下来，否则 finish 会把没判完的补充清掉。"""
    _seed(turn_seq=4, turn_started={"4": NOW - 30}, supp=_supp(3, ts=NOW - 10))
    monkeypatch.setattr(_fine_flow, "_arrived_late", _boom)
    tf = _begin()
    assert tf.supp_gen is None and tf.supp_src_seq is None
    assert tf.supp_candidates == [] and tf.late is False and tf.stale_dropped == 0
    assert "supp" in tf.degraded


def test_settings_failure_keeps_stored_session_settings(monkeypatch):
    """解析会话设置抛了：视图里 session 是空的，finish 不能拿它把盘上已存的设置抹掉。"""
    _seed(session_settings={"coarse_inject": False})
    monkeypatch.setattr(_fine_config, "apply_session_settings", _boom)
    tf = _begin(request={"coarse_inject": True})
    assert "settings" in tf.degraded and tf.view.session == {}
    _fine_flow.finish_turn(tf)
    assert _fine_state.load_fine(SID).session_settings == {"coarse_inject": False}


def test_settings_internal_failure_view_keeps_stored_session_settings(monkeypatch):
    """真实的失败路径：`apply_session_settings` 自己 fail-open、返回空 session 的视图而不是抛。
    请求里的键不是被接受就是被拒收，两个都空只可能是内部失败。"""
    _seed(session_settings={"coarse_inject": False})
    monkeypatch.setattr(_fine_config, "validate_value", _boom)
    tf = _begin(request=None)
    assert tf.view.session == {} and tf.view.rejected == []
    assert "settings" in tf.degraded
    _fine_flow.finish_turn(tf)
    assert _fine_state.load_fine(SID).session_settings == {"coarse_inject": False}


def test_settings_cleared_or_all_rejected_still_overwrites_stored():
    """对照：mod 发了空设置、或发的键全被拒收，都是正常结果，finish 照常覆盖（不是失败）。"""
    _seed(session_settings={"coarse_inject": False})
    tf = _begin(request={})
    assert "settings" not in tf.degraded
    _fine_flow.finish_turn(tf)
    assert _fine_state.load_fine(SID).session_settings == {}
    _seed(session_settings={"coarse_inject": False})
    tf = _begin(request={"no_such_key": 1})
    assert tf.view.rejected and "settings" not in tf.degraded
    _fine_flow.finish_turn(tf)
    assert _fine_state.load_fine(SID).session_settings == {}


# ── 补充时效（D1 / D10） ─────────────────────────────────────────────────────

def test_supp_fresh_on_next_turn():
    _seed(turn_seq=3, supp=_supp(3))
    tf = _begin()
    assert tf.supp_candidates == ["a.md"] and tf.late is False
    assert (tf.supp_gen, tf.supp_src_seq, tf.stale_dropped) == ("g1", 3, 0)


def test_supp_late_band_requires_arrival_after_next_turn_started():
    """D10：写回晚于第 src+1 轮开始（那一轮没有机会投递它）→ 第 src+2 轮放宽一档。"""
    _seed(turn_seq=4, turn_started={"4": NOW - 30}, supp=_supp(3, ts=NOW - 10))
    tf = _begin()
    assert tf.supp_candidates == ["a.md"] and tf.late is True


def test_supp_late_band_rejected_when_arrived_before_next_turn():
    _seed(turn_seq=4, turn_started={"4": NOW - 10}, supp=_supp(3, ts=NOW - 30))
    tf = _begin()
    assert tf.supp_candidates == [] and tf.stale_dropped == 1 and tf.supp_gen == "g1"


def test_late_delivery_requires_turn_started():
    """Review Focus 5：turn_started 缺 src+1 那一项（旧文件、被裁剪）时不放宽。"""
    _seed(turn_seq=4, turn_started={}, supp=_supp(3, ts=NOW - 10))
    tf = _begin()
    assert tf.supp_candidates == [] and tf.late is False and tf.stale_dropped == 1


def test_supp_stale_beyond_late_band():
    _seed(turn_seq=6, turn_started={"4": NOW - 300, "5": NOW - 200, "6": NOW - 100},
          supp=_supp(3, ts=NOW - 50))
    tf = _begin()
    assert tf.supp_candidates == [] and tf.stale_dropped == 1 and tf.supp_gen == "g1"


def test_supp_not_yet_due_is_kept():
    """会话重置后 turn_seq 落在 src_seq 之前：既不投递也不清（supp_gen 为 None）。"""
    _seed(turn_seq=1, supp=_supp(5))
    tf = _begin()
    assert tf.supp_candidates == [] and tf.supp_gen is None and tf.stale_dropped == 0


# ── 失联与停用恢复（F05 / F66） ──────────────────────────────────────────────

def test_expired_inflight_counts_lost_and_is_released():
    assert _fine_state.claim_inflight(SID, "gx", 10, NOW - 1000)
    tf = _begin()
    assert tf.lost_gen == "gx"
    _fine_flow.finish_turn(tf)
    st = _fine_state.load_fine(SID)
    assert st.fail_streak == 1 and st.last_fail == "lost"
    assert _fine_state.inflight_expired(SID, NOW) is None          # 标记已删


def test_live_inflight_is_not_counted_lost():
    """在飞标记还没过期：begin_turn 不记 lost，finish_turn 也不放掉标记（子进程还在跑）。
    时间用真实时间：标记的 started 比文件 mtime 超前 300 秒以上会被夹成 mtime，固定的 NOW 会让它按已过期读出。"""
    t = time.time()
    assert _fine_state.claim_inflight(SID, "live", 120, t - 1)
    tf = _begin(now=t)
    assert tf.lost_gen is None
    _fine_flow.finish_turn(tf)
    st = _fine_state.load_fine(SID)
    assert st.fail_streak == 0 and st.last_fail != "lost"
    assert _fine_state.inflight_path(SID).exists()                 # 标记文件还在，没被 release
    assert _fine_state.inflight_active(SID, t)["gen_id"] == "live"


def test_two_losses_disable_with_streak():
    _seed(fail_streak=1)
    assert _fine_state.claim_inflight(SID, "gy", 10, NOW - 1000)
    tf = _begin()
    _fine_flow.finish_turn(tf)
    st = _fine_state.load_fine(SID)
    assert st.fail_streak == _fine_state.STREAK_LIMIT == 2 and st.disabled_reason == "streak"


def test_corrupt_marker_is_counted_once_and_released():
    """读不出内容的过期标记 gen_id 是合成的（`?`），`release_inflight` 按 gen 对不上、永远不删。
    不放掉的话每轮都重复记一次 lost，第二轮起 streak 停用。"""
    p = _corrupt_marker(NOW - 1000)
    for _ in range(2):
        tf = _begin()
        _fine_flow.finish_turn(tf)
    st = _fine_state.load_fine(SID)
    assert st.fail_streak == 1 and st.last_fail == "lost" and st.disabled_reason is None
    assert not p.exists()


def test_corrupt_marker_release_spares_marker_claimed_after_begin():
    """begin 之后本轮发起了新子进程（claim 先回收过期标记再写新的）：finish 只放仍然过期的，新标记不动。"""
    t = time.time()
    _corrupt_marker(t - 1000)
    tf = _begin(now=t)
    assert tf.lost_gen == "?"
    assert _fine_state.claim_inflight(SID, "fresh", 120, t)
    _fine_flow.finish_turn(tf)
    assert _fine_state.load_fine(SID).fail_streak == 1
    active = _fine_state.inflight_active(SID, t)
    assert active is not None and active["gen_id"] == "fresh"


def test_recovery_and_loss_in_same_turn_count_one_failure():
    """停用恢复先清零、失联再记一次：本轮 lost 不能被恢复抹掉。"""
    _seed(fail_streak=2, disabled_reason="streak", disabled_at=NOW - 3600)
    assert _fine_state.claim_inflight(SID, "gx", 10, NOW - 1000)
    tf = _begin()
    assert tf.recovered is True and tf.lost_gen == "gx"
    _fine_flow.finish_turn(tf)
    st = _fine_state.load_fine(SID)
    assert st.fail_streak == 1 and st.last_fail == "lost"
    assert (st.disabled_reason, st.disabled_at) == (None, None)


def test_second_loss_blocks_spawn_in_same_turn():
    """第二次 lost 把 streak 推到上限的那一轮：finish 时才 +1，门禁不能只看 begin 时读到的计数，
    否则这一轮还会放行一次付费发起。"""
    _seed(fail_streak=1)
    assert _fine_state.claim_inflight(SID, "gy", 10, NOW - 1000)
    tf = _begin()
    assert tf.lost_gen == "gy" and tf.recovered is False
    assert _blocked(tf) == "disabled:streak"


def test_expired_inflight_marker_does_not_block_spawn():
    """lost 只是第一次（streak 0 → 1，没到上限），门禁放行；过期标记不算「有在飞」，否则它会永远挡住发起。"""
    assert _fine_state.claim_inflight(SID, "gx", 10, NOW - 1000)
    tf = _begin()
    assert tf.lost_gen == "gx"
    assert _blocked(tf) is None


def test_recovered_turn_with_loss_does_not_block_spawn():
    """恢复先清零，本轮的 lost 只算 0 + 1：不能把恢复前的旧计数算进去。"""
    _seed(fail_streak=2, disabled_reason="streak", disabled_at=NOW - 3600)
    assert _fine_state.claim_inflight(SID, "gx", 10, NOW - 1000)
    tf = _begin()
    assert tf.recovered is True and tf.lost_gen == "gx"
    assert _blocked(tf) is None


def test_streak_recovers_after_cooldown():
    _seed(fail_streak=2, disabled_reason="streak",
          disabled_at=NOW - _fine_state.DISABLE_COOLDOWN_SEC - 1)
    tf = _begin()
    assert tf.recovered is True and _blocked(tf) is None
    _fine_flow.finish_turn(tf)
    st = _fine_state.load_fine(SID)
    assert (st.fail_streak, st.disabled_reason, st.disabled_at) == (0, None, None)


def test_streak_not_recovered_inside_cooldown():
    _seed(fail_streak=2, disabled_reason="streak", disabled_at=NOW - 60)
    tf = _begin()
    assert tf.recovered is False and _blocked(tf) == "disabled:streak"


def test_panel_reenable_recovers_streak():
    _seed(fail_streak=2, disabled_reason="streak", disabled_at=NOW - 60,
          session_settings={"fine_supplement": False})
    assert _begin(request={"fine_supplement": True}).recovered is True


def test_no_claude_reprobed_after_ttl():
    ttl = DEFAULT_CONFIG["user_prompt_submit"]["state_ttl_hours"]
    _seed(disabled_reason="no_claude", disabled_at=NOW - ttl * 3600 - 1)
    assert _begin().recovered is True
    _seed(disabled_reason="no_claude", disabled_at=NOW - 60)
    assert _begin().recovered is False


def test_call_cap_never_recovers():
    _seed(disabled_reason="call_cap", disabled_at=NOW - 10 ** 6, calls=20)
    tf = _begin()
    assert tf.recovered is False and _blocked(tf) == "disabled:call_cap"


# ── 复核（D1-b） ─────────────────────────────────────────────────────────────

def test_recheck_hits_tag_summary_keywords_or_filename():
    ents = {"n/t.md": _e("n/t.md", tags=("gradle",)),
            "n/s.md": _e("n/s.md", summary="讲 gradle 的配置"),
            "n/k.md": _e("n/k.md", keywords=("gradle",)),
            "n/gradle 调优.md": _e("n/gradle 调优.md"),
            "n/gradle-tips.md": _e("n/gradle-tips.md"),      # 与打分同口径：`-` 是词内字符，不算命中
            "n/none.md": _e("n/none.md", summary="无关内容")}
    kept, dropped = _fine_flow.recheck_supp(list(ents) + ["gone.md"], ents, {"gradle"})
    assert kept == ["n/t.md", "n/s.md", "n/k.md", "n/gradle 调优.md"]
    assert dropped == ["n/gradle-tips.md", "n/none.md"]           # gone.md 两边都不进


def test_recheck_with_no_keywords_drops_everything_present():
    ents = {"n/t.md": _e("n/t.md", tags=("gradle",))}
    assert _fine_flow.recheck_supp(["n/t.md"], ents, set()) == ([], ["n/t.md"])


# ── 发起模式与门禁（§6.1） ───────────────────────────────────────────────────

def test_spawn_mode():
    tf = _begin()
    assert _fine_flow.spawn_mode(tf, topics_ok=False) == "pin"
    assert _fine_flow.spawn_mode(tf, topics_ok=True) == "supp"
    tf2 = _begin(_cfg(fine_supplement=False))
    assert _fine_flow.spawn_mode(tf2, topics_ok=True) is None
    assert _fine_flow.spawn_mode(tf2, topics_ok=False) == "pin"         # F35：关补充 = 只首轮
    assert _fine_flow.spawn_mode(_begin(_cfg(fine_rank=False)), topics_ok=False) is None
    assert _fine_flow.spawn_mode(_begin(_cfg(session_topic=False)), topics_ok=False) is None


@pytest.mark.parametrize("kw,expected", [
    (dict(gate_passed=False), "gate"),
    (dict(dry_run=True), "dry_run"),
    (dict(agent_type="general-purpose"), "subagent"),
    (dict(n_candidates=0), "no_candidates"),
])
def test_spawn_blocked_reasons(kw, expected):
    assert _blocked(_begin(), **kw) == expected


def test_agent_type_judged_by_truthiness():
    """UPS payload 恒带 agent_type 键、主会话值为 null（2.1.296 探针实测）：判真值，不判键在不在。"""
    tf = _begin()
    assert _blocked(tf, agent_type=None) is None
    assert _blocked(tf, agent_type="") is None


def test_spawn_blocked_by_caps_streak_inflight_and_session():
    _seed(calls=20)
    assert _blocked(_begin()) == "call_cap"
    _seed(bytes_sent=1_000_000)
    assert _blocked(_begin()) == "bytes_cap"
    _seed(bytes_sent=999_000, stats_q=[{"gen_id": "s1", "bytes": 2000}])   # 尚未并账的子进程字节也算
    assert _blocked(_begin()) == "bytes_cap"
    _seed(fail_streak=2)
    assert _blocked(_begin()) == "disabled:streak"
    _seed()
    # 标记里的 started 比文件 mtime 超前 300 秒以上时 `_marker_read` 改用 mtime（防伪造），而 mtime 是真实时间：
    # 用固定的 NOW（写下时比真实时间晚几个月）会让这个标记按「早已过期」读出来，所以这一段用真实时间
    t = time.time()
    assert _fine_state.claim_inflight(SID, "gz", 120, t - 1)
    assert _blocked(_begin(now=t), now=t) == "inflight"
    tf = _fine_flow.begin_turn("", _cfg(), None, now=NOW)
    assert _blocked(tf) == "no_session"


# ── finish_turn：一次条件写 ─────────────────────────────────────────────────

def test_finish_turn_merges_one_conditional_write():
    _seed(turn_seq=7, turn_started={"5": 1.0, "6": 2.0, "7": 3.0}, supp=_supp(7, paths=("x.md",)),
          injected=["old.md"], stats_q=[{"gen_id": "s1", "bytes": 120}, {"gen_id": "s2", "bytes": 5}])
    tf = _begin(request={"coarse_inject": False})
    tf.injected_add.extend(["new.md", "old.md", "new.md"])
    tf.spawned_gen = "g9"

    def child_write(st):          # 子进程在本轮 begin 与 finish 之间写回了一份更新的补充与统计
        st.supp = _supp(8, paths=("y.md",), gen="g2")
        st.stats_q = list(st.stats_q) + [{"gen_id": "s3", "bytes": 7}]
        return st

    _fine_state.update_fine(SID, child_write)
    _fine_flow.finish_turn(tf)
    st = _fine_state.load_fine(SID)
    assert st.turn_seq == 8
    assert sorted(st.turn_started, key=int) == ["6", "7", "8"]
    assert st.injected == ["old.md", "new.md"]
    assert st.supp["gen_id"] == "g2"                 # gen 不同：不清
    assert [s["gen_id"] for s in st.stats_q] == ["s3"]
    assert st.bytes_sent == 125 and st.calls == 1
    assert st.session_settings == {"coarse_inject": False}


def test_finish_turn_clears_delivered_and_stale_supp():
    _seed(turn_seq=3, supp=_supp(3))
    _fine_flow.finish_turn(_begin())
    assert _fine_state.load_fine(SID).supp is None
    _seed(turn_seq=9, supp=_supp(3))
    _fine_flow.finish_turn(_begin())
    assert _fine_state.load_fine(SID).supp is None


def test_injected_capped_keeps_latest():
    cap = _fine_state.MAX_INJECTED
    assert cap == 300
    _seed(injected=[f"n{i}.md" for i in range(cap - 1)])
    tf = _begin()
    tf.injected_add.extend(["a.md", "b.md"])
    _fine_flow.finish_turn(tf)
    inj = _fine_state.load_fine(SID).injected
    assert len(inj) == cap and inj[-2:] == ["a.md", "b.md"] and inj[0] == "n1.md"


def test_reaching_call_cap_disables():
    _seed(calls=19)
    tf = _begin()
    tf.spawned_gen = "g"
    _fine_flow.finish_turn(tf)
    st = _fine_state.load_fine(SID)
    assert st.calls == 20 and st.disabled_reason == "call_cap"


def test_reaching_bytes_cap_through_stats_merge_disables():
    """外发字节在 finish 并账时才累计到 bytes_sent：并账后恰好到上限就记 bytes_cap（`>=`）；差 1 字节不记。"""
    assert _fine_config.default_of("fine_session_max_bytes") == 1_000_000
    _seed(bytes_sent=998_000, stats_q=[{"gen_id": "s1", "bytes": 2000}])
    _fine_flow.finish_turn(_begin())
    st = _fine_state.load_fine(SID)
    assert st.bytes_sent == 1_000_000 and st.stats_q == []
    assert st.disabled_reason == "bytes_cap" and st.disabled_at is not None
    _seed(bytes_sent=997_999, stats_q=[{"gen_id": "s1", "bytes": 2000}])
    _fine_flow.finish_turn(_begin())
    st = _fine_state.load_fine(SID)
    assert st.bytes_sent == 999_999 and st.disabled_reason is None


def test_finish_merges_on_top_of_values_the_child_wrote_meanwhile():
    """begin 与 finish 之间子进程直接改了盘上的 fail_streak / disabled_reason：finish 在锁内重读、
    在其上合并，不用 begin 读到的旧值回写。"""
    _seed()
    assert _fine_state.claim_inflight(SID, "gx", 10, NOW - 1000)
    tf = _begin()
    assert tf.lost_gen == "gx" and tf.state.fail_streak == 0 and tf.state.disabled_reason is None

    def child(st):
        st.fail_streak = 1
        st.disabled_reason, st.disabled_at = "call_cap", NOW - 5
        return st

    _fine_state.update_fine(SID, child)
    _fine_flow.finish_turn(tf)
    st = _fine_state.load_fine(SID)
    assert st.fail_streak == 2                                    # 子进程写的 1 + 本轮失联 1，不是旧的 0 + 1
    assert (st.disabled_reason, st.disabled_at) == ("call_cap", NOW - 5)   # 子进程记下的停用原因保留


def test_no_claude_from_spawn_or_child_disables():
    tf = _begin()
    tf.spawn_result = "no_claude"
    _fine_flow.finish_turn(tf)
    assert _fine_state.load_fine(SID).disabled_reason == "no_claude"
    _seed(stats_q=[{"gen_id": "s", "fail": "no_claude", "bytes": 0}])
    _fine_flow.finish_turn(_begin())
    assert _fine_state.load_fine(SID).disabled_reason == "no_claude"


def test_finish_turn_fail_open(monkeypatch, capsys):
    tf = _begin()
    monkeypatch.setattr(_fine_state, "update_fine", lambda *a, **k: None)      # 锁超时
    _fine_flow.finish_turn(tf)

    def boom(*a, **k):
        raise TimeoutError("lock")

    monkeypatch.setattr(_fine_state, "update_fine", boom)
    _fine_flow.finish_turn(tf)
    assert "精排状态写回失败" in capsys.readouterr().err
    assert "lock" in tf.degraded
    _fine_flow.finish_turn(None)                                               # 无本轮状态：什么都不做


def test_finish_turn_write_failure_keeps_marker_and_records_lock(monkeypatch):
    """条件写没落盘：本轮的 lost 没记上，过期标记就不能放（放了这次失联永远丢了）；记 degraded=lock。
    下一轮写得进去时，同一个标记会被再读到、记一次、放掉。"""
    assert _fine_state.claim_inflight(SID, "gx", 10, NOW - 1000)
    tf = _begin()
    assert tf.lost_gen == "gx"
    real_update = _fine_state.update_fine
    monkeypatch.setattr(_fine_state, "update_fine", lambda *a, **k: None)     # 锁超时
    _fine_flow.finish_turn(tf)
    assert "lock" in tf.degraded
    assert _fine_state.inflight_path(SID).exists()
    assert _fine_state.load_fine(SID).fail_streak == 0
    monkeypatch.setattr(_fine_state, "update_fine", real_update)
    tf2 = _begin()
    assert tf2.lost_gen == "gx"
    _fine_flow.finish_turn(tf2)
    assert _fine_state.load_fine(SID).fail_streak == 1
    assert not _fine_state.inflight_path(SID).exists()


def test_finish_turn_without_consuming_stats_keeps_queue_and_bytes():
    """`consume_stats=False`（UPS：metrics 开、本轮没有完整决策记录）：子进程统计留在队列，不出队、不并账
    字节、不从中认 no_claude；其余字段照常合并。末段同一份状态按默认消费作对照，证明这份统计确实被读到了。"""
    stats = [{"gen_id": "s1", "bytes": 50}, {"gen_id": "s2", "fail": "no_claude", "bytes": 7}]
    _seed(turn_seq=4, supp=_supp(4), injected=["old.md"], bytes_sent=100, stats_q=stats)
    assert _fine_state.claim_inflight(SID, "gx", 10, NOW - 1000)
    tf = _begin(request={"coarse_inject": False})
    assert [s["gen_id"] for s in tf.stats] == ["s1", "s2"]
    tf.injected_add.append("new.md")
    tf.spawned_gen = "g9"
    _fine_flow.finish_turn(tf, consume_stats=False)
    st = _fine_state.load_fine(SID)
    assert [s["gen_id"] for s in st.stats_q] == ["s1", "s2"]         # 不出队
    assert st.bytes_sent == 100                                      # 不并账
    assert st.disabled_reason is None                                # 留队条目里的 no_claude 不认
    assert st.turn_seq == 5 and st.injected == ["old.md", "new.md"] and st.supp is None
    assert st.session_settings == {"coarse_inject": False}
    assert st.calls == 1 and st.fail_streak == 1 and st.last_fail == "lost"
    assert not _fine_state.inflight_path(SID).exists()              # 失联照记、过期标记照放

    _seed(bytes_sent=100, stats_q=stats)                             # 对照：默认消费
    _fine_flow.finish_turn(_begin())
    st = _fine_state.load_fine(SID)
    assert st.stats_q == [] and st.bytes_sent == 157 and st.disabled_reason == "no_claude"
