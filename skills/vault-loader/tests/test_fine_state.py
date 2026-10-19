# -*- coding: utf-8 -*-
"""per-session 精排状态（spec 2026-10-13 §7）：落点、字段校验、条件写、在飞标记、机器槽位、清理与 purge。"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import _fine_state as fs
from scripts._state import configure_context

SID = "sess-fine-A"


@pytest.fixture
def canon(tmp_home):
    configure_context("claude", SID)
    return tmp_home


def test_paths_follow_runtime_at_call_time(tmp_home):
    configure_context("claude", SID)
    h = hashlib.sha256(SID.encode("utf-8")).hexdigest()[:24]
    assert fs.sid_hash(SID) == h
    assert fs.fine_path(SID) == tmp_home / ".context-vault" / "state" / "claude" / "fine" / f"{h}.json"
    assert fs.inflight_path(SID) == fs.fine_dir() / f"{h}.inflight"
    assert fs.slots_dir() == fs.fine_dir() / "slots"
    configure_context("legacy", SID)
    assert fs.fine_path(SID) == tmp_home / ".claude" / "vault-loader-fine" / f"{h}.json"
    assert fs.fine_path("") is None and fs.inflight_path("") is None


def test_sid_hash_matches_session_ops():
    from scripts import _session_ops
    assert fs.sid_hash(SID) == _session_ops.session_hash(SID)


def test_pending_grace_matches_topic():
    from scripts import _topic
    assert fs.PENDING_GRACE_SEC == _topic.PENDING_GRACE_SEC


def test_missing_file_loads_default(canon):
    assert fs.load_fine(SID) == fs.FineState()


def test_update_roundtrip(canon):
    def mut(st):
        st.turn_seq = 3
        st.turn_started = {"3": 100.0}
        st.injected = ["a.md", "b.md"]
        st.supp = {"gen_id": "g1", "src_seq": 3, "paths": ["c.md"], "ts": 101.0}
        return st

    out = fs.update_fine(SID, mut)
    assert out is not None and out.turn_seq == 3
    st = fs.load_fine(SID)
    assert st.turn_seq == 3 and st.injected == ["a.md", "b.md"] and st.turn_started == {"3": 100.0}
    assert st.supp == {"gen_id": "g1", "src_seq": 3, "paths": ["c.md"], "ts": 101.0}


def test_to_json_keys_never_carry_prompt_text():
    """F16：fine 状态只有这些键——不存 prompt、K 轮文本、目录。"""
    assert set(fs.FineState().to_json()) == {
        "v", "turn_seq", "turn_started", "supp", "injected", "session_settings", "fail_streak",
        "disabled_reason", "disabled_at", "calls", "bytes_sent", "last_fail", "last_ts", "stats_q"}


@pytest.mark.parametrize("raw,attr,expected", [
    ({"turn_seq": True}, "turn_seq", 0),
    ({"turn_seq": -1}, "turn_seq", 0),
    ({"turn_seq": "3"}, "turn_seq", 0),
    ({"supp": {"gen_id": "g", "src_seq": "1", "paths": [], "ts": 1.0}}, "supp", None),
    ({"supp": {"gen_id": "g", "src_seq": 1, "paths": ["a.md"], "ts": float("inf")}}, "supp", None),
    ({"injected": "a.md"}, "injected", []),
    ({"injected": ["a.md", 1, "a.md", "b.md"]}, "injected", ["a.md", "b.md"]),
    ({"disabled_reason": "evil"}, "disabled_reason", None),
    ({"fail_streak": 2.5}, "fail_streak", 0),
    ({"session_settings": {"fine_rank": False, "x": {"nested": 1}}}, "session_settings", {"fine_rank": False}),
    ({"last_fail": "x" * 100}, "last_fail", None),
])
def test_from_json_drops_bad_fields(raw, attr, expected):
    assert getattr(fs.FineState.from_json(raw), attr) == expected


def test_supp_paths_filtered_and_capped():
    st = fs.FineState.from_json({"supp": {"gen_id": "g", "src_seq": 2, "ts": 5.0,
                                          "paths": ["a.md", 3, ""] + [f"n{i}.md" for i in range(10)]}})
    assert st.supp["paths"] == ["a.md"] + [f"n{i}.md" for i in range(7)]


def test_turn_started_keeps_last_three_numeric_keys():
    st = fs.FineState.from_json({"turn_started": {"1": 1.0, "2": 2.0, "10": 10.0, "3": 3.0,
                                                  "x": 9.0, "²": 1.0}})
    assert st.turn_started == {"2": 2.0, "3": 3.0, "10": 10.0}


def test_stats_q_keeps_only_known_scalar_keys_and_last_five():
    q = [{"gen_id": f"g{i}", "mode": "supp", "ok": True, "n_sel": 2, "prompt": "不该留", "deep": {"a": 1}}
         for i in range(7)] + [{"mode": "supp"}]
    st = fs.FineState.from_json({"stats_q": q})
    assert [x["gen_id"] for x in st.stats_q] == ["g2", "g3", "g4", "g5", "g6"]
    assert all(set(x) <= set(fs.STATS_KEYS) for x in st.stats_q)


def test_stats_q_keeps_degraded_as_short_string_list():
    """子进程回报的降级码列表（Task 12 裁定）：只留非空、≤ 32 字的字符串，至多 8 个；其余元素丢掉，
    整个值不是列表时不留这个键。漏掉它时子进程写入的 degraded 会在 `update_fine` 归一时被静默丢弃。"""
    assert "degraded" in fs.STATS_KEYS
    junk = ["bad_model", 3, "", "x" * 33, None, {"a": 1}, ["n"]] + [f"c{i}" for i in range(10)]
    q = [{"gen_id": "g1", "degraded": junk},
         {"gen_id": "g2", "degraded": "bad_model"},
         {"gen_id": "g3", "degraded": []}]
    st = fs.FineState.from_json({"stats_q": q})
    assert st.stats_q[0]["degraded"] == ["bad_model"] + [f"c{i}" for i in range(7)]
    assert "degraded" not in st.stats_q[1]
    assert st.stats_q[2]["degraded"] == []


def test_stats_q_keeps_null_values(canon):
    """成功那条的 `fail` 是 None：键要留着（子进程统计每条键恒定，Task 12），且经 update_fine 往返不丢。"""
    rec = {"gen_id": "g1", "mode": "pin", "ok": True, "fail": None, "degraded": ["bad_model"]}

    def mut(st):
        st.stats_q = [rec]
        return st

    assert fs.update_fine(SID, mut) is not None
    assert fs.load_fine(SID).stats_q == [rec]


def test_injected_capped_keeps_most_recent(canon):
    paths = [f"n/{i}.md" for i in range(fs.MAX_INJECTED + 50)]

    def mut(st):
        st.injected = paths
        return st

    fs.update_fine(SID, mut)
    st = fs.load_fine(SID)
    assert len(st.injected) == fs.MAX_INJECTED
    assert st.injected[0] == "n/50.md" and st.injected[-1] == paths[-1]


def test_corrupt_or_oversize_file_loads_default(canon):
    p = fs.fine_path(SID)
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert fs.load_fine(SID) == fs.FineState()
    p.write_text(json.dumps({"turn_seq": 5, "pad": "x" * (fs.MAX_FINE_BYTES + 10)}), encoding="utf-8")
    assert fs.load_fine(SID) == fs.FineState()


def test_mutate_none_means_no_write(canon):
    assert fs.update_fine(SID, lambda st: None) == fs.FineState()
    assert not fs.fine_path(SID).exists()


def test_update_returns_none_on_lock_timeout(canon, monkeypatch):
    @contextmanager
    def busy(_target, **_kw):
        raise TimeoutError("busy")
        yield

    monkeypatch.setattr(fs, "lease_lock", busy)
    assert fs.update_fine(SID, lambda st: st) is None
    assert not fs.fine_path(SID).exists()


def test_mutate_exception_is_fail_open(canon):
    def boom(_st):
        raise RuntimeError("boom")

    assert fs.update_fine(SID, boom) is None


def test_update_uses_short_lock_timeout(canon, monkeypatch):
    seen = {}
    real = fs.lease_lock

    def spy(target, **kw):
        seen.update(kw)
        return real(target, **kw)

    monkeypatch.setattr(fs, "lease_lock", spy)
    fs.update_fine(SID, lambda st: st)
    assert seen.get("timeout") == fs.LOCK_TIMEOUT_SEC == 0.25


def test_inflight_claim_is_exclusive_and_released_by_owner_only(canon):
    now = 1_000_000.0
    assert fs.claim_inflight(SID, "g1", 120, now) is True
    assert fs.claim_inflight(SID, "g2", 120, now + 1) is False
    assert fs.inflight_active(SID, now + 1)["gen_id"] == "g1"
    assert fs.inflight_expired(SID, now + 1) is None
    fs.release_inflight(SID, "g2")
    assert fs.inflight_path(SID).exists()
    fs.release_inflight(SID, "g1")
    assert not fs.inflight_path(SID).exists()


def test_expired_inflight_is_reported_then_reclaimed(canon):
    now = 1_000_000.0
    assert fs.claim_inflight(SID, "old", 10, now)
    later = now + 10 + fs.PENDING_GRACE_SEC + 1
    assert fs.inflight_active(SID, later) is None
    assert fs.inflight_expired(SID, later)["gen_id"] == "old"
    assert fs.claim_inflight(SID, "new", 120, later) is True
    assert fs.inflight_active(SID, later)["gen_id"] == "new"


def test_release_expired_inflight_only_drops_expired_markers(canon):
    now = 1_000_000.0
    assert fs.release_expired_inflight(SID, now) is False          # 没有标记：什么都不做
    assert fs.claim_inflight(SID, "live", 120, now)
    assert fs.release_expired_inflight(SID, now + 1) is False      # 未过期：不动，也不看 gen
    assert fs.inflight_path(SID).exists()
    later = now + 120 + fs.PENDING_GRACE_SEC + 1
    assert fs.release_expired_inflight(SID, later) is True
    assert not fs.inflight_path(SID).exists()


def test_release_expired_inflight_drops_corrupt_marker_by_mtime(canon):
    """读不出内容的标记 gen_id 合成为空串，`release_inflight` 按 gen 永远对不上；这里只看是否过期。"""
    p = fs.inflight_path(SID)
    p.parent.mkdir(parents=True)
    p.write_text("garbage", encoding="utf-8")
    mt = p.stat().st_mtime
    assert fs.release_expired_inflight(SID, mt + 1) is False
    assert p.exists()
    assert fs.release_expired_inflight(SID, mt + fs.DEFAULT_MARKER_TIMEOUT + fs.PENDING_GRACE_SEC + 1) is True
    assert not p.exists()


def test_corrupt_marker_expires_by_mtime(canon):
    p = fs.inflight_path(SID)
    p.parent.mkdir(parents=True)
    p.write_text("garbage", encoding="utf-8")
    mt = p.stat().st_mtime
    assert fs.inflight_active(SID, mt + 1) is not None          # 读不出内容也先当在飞
    far = mt + fs.DEFAULT_MARKER_TIMEOUT + fs.PENDING_GRACE_SEC + 1
    assert fs.inflight_active(SID, far) is None
    assert fs.claim_inflight(SID, "g", 120, far) is True


def test_future_started_cannot_pin_marker_forever(canon):
    p = fs.inflight_path(SID)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"gen_id": "x", "started": 9e18, "timeout": 10}), encoding="utf-8")
    mt = p.stat().st_mtime
    assert fs.inflight_expired(SID, mt + 10 + fs.PENDING_GRACE_SEC + 1) is not None


def test_slots_capped_and_reclaimed(canon):
    now = 1_000_000.0
    names = [fs.claim_slot(f"g{i}", 120, now) for i in range(fs.MAX_FINE_SLOTS)]
    assert None not in names and len(set(names)) == fs.MAX_FINE_SLOTS
    assert fs.claim_slot("extra", 120, now) is None
    fs.release_slot(names[0], "wrong")
    assert fs.claim_slot("extra", 120, now) is None
    fs.release_slot(names[0], "g0")
    assert fs.claim_slot("extra", 120, now) == names[0]
    later = now + 120 + fs.PENDING_GRACE_SEC + 1
    assert fs.claim_slot("late", 120, later) is not None      # 过期槽位可被回收
    fs.release_slot("../evil.json", "g1")                      # 非法槽位名静默忽略


def test_cleanup_removes_only_own_stale_files(canon):
    d = fs.fine_dir()
    (d / "slots").mkdir(parents=True)
    now = time.time()
    old = now - 48 * 3600
    stale = d / f"{'a' * 24}.json"
    fresh = d / f"{'b' * 24}.json"
    foreign = d / "notes.json"
    stale_tmp = d / f".{'c' * 24}.json.123.deadbeef.tmp"
    for p in (stale, fresh, foreign, stale_tmp):
        p.write_text("{}", encoding="utf-8")
    for p in (stale, foreign, stale_tmp):
        os.utime(p, (old, old))
    assert fs.claim_inflight(SID, "g", 10, now - 3600)        # 已过期的在飞标记
    assert fs.claim_slot("g", 10, now - 3600)                  # 已过期的槽位
    assert fs.cleanup_fine(24, now) == 4
    assert not stale.exists() and not stale_tmp.exists()
    assert fresh.exists() and foreign.exists()
    assert not fs.inflight_path(SID).exists()
    assert list((d / "slots").iterdir()) == []


def test_cleanup_skips_link_like_dir(canon, monkeypatch):
    d = fs.fine_dir()
    d.mkdir(parents=True)
    p = d / f"{'a' * 24}.json"
    p.write_text("{}", encoding="utf-8")
    old = time.time() - 48 * 3600
    os.utime(p, (old, old))
    monkeypatch.setattr(fs, "_is_link_like", lambda _p: True)
    assert fs.cleanup_fine(24) == 0
    assert p.exists()


def test_purge_fine_clears_both_namespaces(tmp_home):
    canon_dir = tmp_home / ".context-vault" / "state" / "claude" / "fine"
    legacy_dir = tmp_home / ".claude" / "vault-loader-fine"
    for d in (canon_dir, legacy_dir):
        (d / "slots").mkdir(parents=True)
        (d / f"{'a' * 24}.json").write_text("{}", encoding="utf-8")
        (d / f"{'a' * 24}.inflight").write_text("{}", encoding="utf-8")
        (d / "slots" / "slot-0.json").write_text("{}", encoding="utf-8")
    keep = canon_dir / "README.txt"
    keep.write_text("not ours", encoding="utf-8")
    assert fs.purge_fine(tmp_home) == 6
    assert keep.exists()
    assert not legacy_dir.exists()


# ---- 以下 4 条为变异验证补充：计划原文的 34 条没有守住这些已写进需求的行为 ----

def test_turn_started_orders_numerically_not_lexically():
    """第 10 轮起字符串序会把 "8" "9" 排到 "10" "11" 之后，留下的就不是最近 3 轮。"""
    st = fs.FineState.from_json({"turn_started": {"8": 8.0, "9": 9.0, "10": 10.0, "11": 11.0}})
    assert st.turn_started == {"9": 9.0, "10": 10.0, "11": 11.0}


def test_marker_timeout_is_capped_so_it_cannot_pin_forever(canon):
    p = fs.inflight_path(SID)
    p.parent.mkdir(parents=True)
    started = time.time()
    p.write_text(json.dumps({"gen_id": "x", "started": started, "timeout": 10 ** 9}), encoding="utf-8")
    assert fs.inflight_active(SID, started)["timeout"] == fs.DEFAULT_MARKER_TIMEOUT
    later = started + fs.DEFAULT_MARKER_TIMEOUT + fs.PENDING_GRACE_SEC + 1
    assert fs.inflight_expired(SID, later) is not None
    assert fs.claim_inflight(SID, "new", 120, later) is True


def test_update_writes_normalized_state_to_disk(canon):
    """读端本身也会归一，所以必须看**落盘内容**：未归一的超限文件会越过 MAX_FINE_BYTES、被读端整份当损坏。"""
    def mut(st):
        st.injected = [f"n/{i}.md" for i in range(fs.MAX_INJECTED + 50)]
        return st

    out = fs.update_fine(SID, mut)
    raw = json.loads(fs.fine_path(SID).read_text(encoding="utf-8"))
    assert len(raw["injected"]) == fs.MAX_INJECTED and len(out.injected) == fs.MAX_INJECTED


def test_link_like_dir_is_refused_by_writers(canon, monkeypatch):
    monkeypatch.setattr(fs, "_is_link_like", lambda _p: True)
    assert fs.update_fine(SID, lambda st: st) is None
    assert fs.claim_inflight(SID, "g", 120, 1_000_000.0) is False
    assert fs.claim_slot("g", 120, 1_000_000.0) is None
    assert not fs.fine_dir().exists()


# ---- 修复轮 1：坏文件 / 恶意内容不得逃出 fail-open；清理与锁内读改写的安全性质补守卫 ----

def test_huge_int_marker_started_does_not_raise_and_expires_by_mtime(canon):
    """`math.isfinite(10**400)` 会抛 OverflowError：标记里一个超大整数曾让 4 个入口永久抛异常，而清理也删不掉它。"""
    p = fs.inflight_path(SID)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"gen_id": "x", "started": 10 ** 400, "timeout": 10}), encoding="utf-8")
    mt = p.stat().st_mtime
    assert fs.inflight_active(SID, mt + 1) is not None            # 读不出的字段按 corrupt 处理，先当在飞
    fs.release_inflight(SID, "x")                                  # 不抛
    far = mt + fs.DEFAULT_MARKER_TIMEOUT + fs.PENDING_GRACE_SEC + 1
    assert fs.inflight_expired(SID, far) is not None
    assert fs.cleanup_fine(24, far) == 1                           # 清理能回收它
    assert not p.exists()
    assert fs.claim_inflight(SID, "n", 120, far) is True


def test_huge_int_field_falls_back_per_field():
    """一个字段的超大整数只让该字段回落，不得把整份状态重置成默认。"""
    big = 10 ** 400
    st = fs.FineState.from_json({
        "turn_seq": 5, "calls": 7, "last_ts": big,
        "disabled_reason": "streak", "disabled_at": big,
        "supp": {"gen_id": "g", "src_seq": 1, "paths": [], "ts": big},
        "turn_started": {"1": big, "2": 2.0}})
    assert st.turn_seq == 5 and st.calls == 7 and st.last_ts is None
    assert st.disabled_reason == "streak" and st.disabled_at is None
    assert st.supp is None and st.turn_started == {"2": 2.0}


def test_deep_nest_state_file_loads_default_and_is_overwritable(canon):
    """10 万个 `[` 让 `json.loads` 抛 RecursionError（不是 ValueError）：文件曾永久读不了、也永远写不进去。"""
    p = fs.fine_path(SID)
    p.parent.mkdir(parents=True)
    p.write_text("[" * (100 * 1024), encoding="utf-8")
    assert fs.load_fine(SID) == fs.FineState()

    def mut(st):
        st.turn_seq = 7
        return st

    out = fs.update_fine(SID, mut)
    assert out is not None and out.turn_seq == 7
    assert fs.load_fine(SID).turn_seq == 7
    assert json.loads(p.read_text(encoding="utf-8"))["turn_seq"] == 7


def test_deep_nest_marker_is_treated_as_corrupt_and_expires_by_mtime(canon, monkeypatch):
    """标记上限 4096 字节：本机 3.14 的 `json.loads` 在 4096 层 `[` 时只抛 JSONDecodeError，守卫等于天然零判别力；
    而 3.9–3.13 递归上限约 1000 层、4000 字节就抛 RecursionError。故注入假 `loads` 还原旧版本行为。"""
    p = fs.inflight_path(SID)
    p.parent.mkdir(parents=True)
    p.write_text("[" * 4000, encoding="utf-8")                     # 小于标记的 4096 字节上限
    mt = p.stat().st_mtime
    real_loads = json.loads

    def old_python_loads(s, *a, **kw):
        if "[" * 900 in s:
            raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")
        return real_loads(s, *a, **kw)

    with monkeypatch.context() as m:
        m.setattr(fs.json, "loads", old_python_loads)
        assert fs.inflight_active(SID, mt + 1)["corrupt"] is True
        far = mt + fs.DEFAULT_MARKER_TIMEOUT + fs.PENDING_GRACE_SEC + 1
        assert fs.inflight_active(SID, far) is None
        assert fs.claim_inflight(SID, "g", 120, far) is True


def test_cleanup_spares_live_markers_slots_and_fresh_tmp_and_drops_stale_lock(canon):
    now = time.time()
    old = now - 48 * 3600
    assert fs.claim_inflight(SID, "live", 120, now)               # 未过期的在飞标记
    slot = fs.claim_slot("live", 120, now)                         # 未过期的槽位
    d = fs.fine_dir()
    fresh_tmp = d / f".{'c' * 24}.json.123.deadbeef.tmp"           # 别的进程正在写的临时文件
    fresh_lock = d / f"{'d' * 24}.json.lock"
    stale_lock = d / f"{'e' * 24}.json.lock"
    for p in (fresh_tmp, fresh_lock, stale_lock):
        p.write_text("x", encoding="utf-8")
    os.utime(stale_lock, (old, old))
    assert fs.cleanup_fine(24, now) == 1
    assert not stale_lock.exists()
    assert fs.inflight_path(SID).exists() and (fs.slots_dir() / slot).exists()
    assert fresh_tmp.exists() and fresh_lock.exists()


def test_update_reads_and_mutates_inside_the_lock(canon, monkeypatch):
    held = {"on": False}
    seen = {}
    real_lock = fs.lease_lock
    real_read = fs._read

    @contextmanager
    def spy_lock(target, **kw):
        with real_lock(target, **kw):
            held["on"] = True
            try:
                yield
            finally:
                held["on"] = False

    def spy_read(p):
        seen["read"] = held["on"]
        return real_read(p)

    def mut(st):
        seen["mutate"] = held["on"]
        st.turn_seq = 1
        return st

    monkeypatch.setattr(fs, "lease_lock", spy_lock)
    monkeypatch.setattr(fs, "_read", spy_read)
    assert fs.update_fine(SID, mut) is not None
    assert seen == {"read": True, "mutate": True}


def test_disabled_at_requires_a_valid_reason():
    assert fs.FineState.from_json({"disabled_at": 5.0}).disabled_at is None
    assert fs.FineState.from_json({"disabled_reason": "evil", "disabled_at": 5.0}).disabled_at is None
    assert fs.FineState.from_json({"disabled_reason": "streak", "disabled_at": 5.0}).disabled_at == 5.0


@pytest.mark.parametrize("bad", [None, float("inf"), float("nan"), "x"])
def test_bad_timeout_sec_fails_open_without_leaving_a_marker(canon, bad):
    """`int(timeout_sec)` 在 try 外：None / inf / "x" 曾直接抛出，而 claim_* 的契约是失败返回 False / None。"""
    assert fs.claim_inflight(SID, "g", bad, 1_000_000.0) is False
    assert fs.claim_slot("g", bad, 1_000_000.0) is None
    assert not fs.inflight_path(SID).exists()
    assert not fs.slots_dir().exists() or list(fs.slots_dir().iterdir()) == []


def test_failed_marker_write_leaves_no_empty_marker(canon, monkeypatch):
    """`os.write` 失败（磁盘满等）时，已 O_EXCL 建出的空标记要删掉，否则它会挡住后续 claim 约 330 s。"""
    now = 1_000_000.0

    def boom(_fd, _data):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as m:
        m.setattr(fs.os, "write", boom)
        assert fs.claim_inflight(SID, "g1", 120, now) is False
        assert fs.claim_slot("g1", 120, now) is None
    assert not fs.inflight_path(SID).exists()
    assert list(fs.slots_dir().iterdir()) == []
    assert fs.claim_inflight(SID, "g2", 120, now + 1) is True
    assert fs.claim_slot("g2", 120, now + 1) == "slot-0.json"
