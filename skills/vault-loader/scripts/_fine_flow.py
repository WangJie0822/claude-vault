# -*- coding: utf-8 -*-
"""UPS 侧每轮的精排编排（spec 2026-10-13 §4、§6.1、§6.4、§7）。

热路径约束（§7）：每轮对 per-session fine 状态**至多一次读**（`begin_turn`）、**一次条件写**
（`finish_turn`，由 `prompt_submit_load._finish_with_metrics` 在 emit 之后、metrics annotate 与 flush
之前调用：写失败记的 `degraded=lock` 随本轮记录落盘）。
在飞标记与机器槽位是独立文件（`O_EXCL`），由调用方在发起时直接读写，不经这里。

`begin_turn` 只读：本轮序号、补充时效、会话设置、失联与停用恢复都在内存里算好，挂在
`TurnFine` 上交给 `finish_turn` 一次落盘。`recheck_supp` / `spawn_mode` 是纯函数；
`spawn_blocked` 只额外读一次在飞标记。

写回是**条件写**：`finish_turn` 在锁内重读，只清本轮读到的那一份补充（按 gen_id）、只移除本轮
读到的子进程统计（按 gen_id），所以子进程在本轮期间写回的新结果不会被覆盖。
"""
from __future__ import annotations

import copy
import math
import sys
import time
from dataclasses import dataclass, field

from . import _fine_config, _fine_state
from ._fine_state import FineState
from ._scorer import (_keyword_hits_keywords, _keyword_hits_summary, _keyword_hits_tags,
                      _kw_in_text)


@dataclass
class TurnFine:
    session_id: str
    view: "_fine_config.SettingsView"
    state: FineState                  # begin_turn 读到的快照（不在本轮内修改）
    turn_seq: int                     # 本轮序号（已 +1）
    turn_ts: float
    supp_candidates: list = field(default_factory=list)   # 时效合格的补充 path（复核前）
    supp_gen: "str | None" = None     # 本轮读到并要清掉的那份补充（含时效丢弃）
    supp_src_seq: "int | None" = None
    late: bool = False                # 是否走 D10 放宽档
    stale_dropped: int = 0
    recheck_dropped: int = 0          # 本轮提问词复核丢弃的篇数（UPS 打分轮填，`_annotate_fine` 落 n_supp_drop_recheck）
    recovered: bool = False           # 本轮清除 streak / no_claude 停用
    lost_gen: "str | None" = None     # 已过期的在飞标记
    stats: list = field(default_factory=list)              # 读出的子进程统计（finish 按 gen_id 移除）
    degraded: list = field(default_factory=list)
    injected_add: list = field(default_factory=list)       # 本轮渲染进上下文的篇（finish 追加进 injected）
    delivered: list = field(default_factory=list)          # 本轮实际投递的补充 path
    spawned_gen: "str | None" = None
    spawned_bytes: int = 0            # 保留字段：外发字节由子进程经 stats_q 回报，见 finish_turn
    spawn_result: "str | None" = None  # spawn_fine 的返回值（no_claude 时据此停用）


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _int(v) -> "int | None":
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _nonneg(v) -> int:
    n = _int(v)
    return n if n is not None and n > 0 else 0


def _fallback_view(config: dict) -> "_fine_config.SettingsView":
    return _fine_config.SettingsView(config=copy.deepcopy(config), effective={}, session={},
                                     locked=[], source={}, rejected=[])


def _ttl_hours(config) -> float:
    ups = config.get("user_prompt_submit") if isinstance(config, dict) else None
    v = ups.get("state_ttl_hours", 24) if isinstance(ups, dict) else 24
    return float(v) if _num(v) and v > 0 else 24.0


def _recovery(state: FineState, stored: dict, session: dict, config: dict, now: float) -> bool:
    """F66：streak 停用满冷却期、或面板把精排 / 逐轮补充从关重新打开 → 清除；no_claude 过
    `state_ttl_hours` 重新探测一次；call_cap / bytes_cap 本会话内不恢复。"""
    reason, at = state.disabled_reason, state.disabled_at
    streak = reason == "streak" or (reason is None
                                    and (_int(state.fail_streak) or 0) >= _fine_state.STREAK_LIMIT)
    if streak:
        if _num(at) and now - at >= _fine_state.DISABLE_COOLDOWN_SEC:
            return True
        return any(stored.get(k) is False and session.get(k) is True
                   for k in ("fine_rank", "fine_supplement"))
    if reason == "no_claude":
        return _num(at) and now - at >= _ttl_hours(config) * 3600
    return False


def _arrived_late(state: FineState, src: int, ts) -> bool:
    """D10：补充的写回时刻晚于第 src+1 轮开始（那一轮读状态时它还不在）。缺项不放宽。"""
    started = (state.turn_started or {}).get(str(src + 1))
    return _num(ts) and _num(started) and ts > started


def _judge_supp(tf: TurnFine, state: FineState) -> None:
    s = state.supp
    if not isinstance(s, dict):
        return
    gen, src = s.get("gen_id"), _int(s.get("src_seq"))
    if not isinstance(gen, str) or not gen or src is None:
        return
    raw = s.get("paths")
    paths = [p for p in raw if isinstance(p, str) and p] if isinstance(raw, list) else []
    if tf.turn_seq <= src:
        return                        # 还没到投递轮（clear/compact 重置后序号落后）：不动它
    tf.supp_gen, tf.supp_src_seq = gen, src
    if tf.turn_seq == src + 1:
        tf.supp_candidates = paths
    elif tf.turn_seq == src + 2 and _arrived_late(state, src, s.get("ts")):
        tf.supp_candidates, tf.late = paths, True
    else:
        tf.stale_dropped = len(paths)


def release_supp_claim(tf: TurnFine) -> None:
    """闸门轮（指代型 / 关键词不足）不认领补充：把 `_judge_supp` 写下的认领复位为 `begin_turn` 读状态之前的初值。

    这样 `finish_turn` 不按 gen_id 清掉盘上那份补充，它留到下一个过闸轮由 `_judge_supp` 照时效判：写回早于
    闸门轮开始的，在那一轮按时效丢弃、计进它完整记录的 `n_supp_gen` 与 `n_supp_drop_stale`；写回晚于闸门轮
    开始的，在第 src+2 轮走 D10 放宽档投递。

    不「认领后消耗」：闸门记录只有五个键，在闸门轮消耗等于让这份补充从漏斗里消失，「产生」少计
    （`_metrics.GATE_RECORD_KEYS` 上方注释的原委）；不认领则由下一个过闸轮的完整记录照常计数，投递结果相同。
    """
    tf.supp_candidates, tf.supp_gen, tf.supp_src_seq = [], None, None
    tf.late, tf.stale_dropped = False, 0


def begin_turn(session_id: str, config: dict, request_settings, now: "float | None" = None) -> TurnFine:
    """读一次 fine 状态，算出本轮的全部精排判定。只读、不抛。

    `request_settings`：本轮 mod 请求里的 `settings`（dict）；None 表示本轮没有 mod 请求或请求里
    没有该键，此时沿用已存的会话设置（spec §10.3）。
    """
    now = time.time() if now is None else now
    degraded: list = []
    state = FineState()
    if session_id:
        try:
            state = _fine_state.load_fine(session_id)
        except Exception:                                # noqa: BLE001 — load_fine 本应不抛，兜底
            degraded.append("fine_read")
            state = FineState()
    stored = state.session_settings if isinstance(state.session_settings, dict) else {}
    chosen = request_settings if isinstance(request_settings, dict) else stored
    try:
        view = _fine_config.apply_session_settings(config, chosen)
    except Exception:                                    # noqa: BLE001
        degraded.append("settings")
        view = _fallback_view(config)
    else:
        # `apply_session_settings` 自己 fail-open（返回空 session 的视图、不抛）。请求里的每个键不是被接受就是
        # 被拒收，两边都空只可能是它内部失败了：同样按「设置解析失败」处理，别让空 session 抹掉已存的设置
        if chosen and not view.session and not view.rejected:
            degraded.append("settings")
    seq = _int(state.turn_seq) or 0
    tf = TurnFine(session_id=session_id or "", view=view, state=state,
                  turn_seq=max(0, seq) + 1, turn_ts=now, degraded=degraded)
    tf.stats = [s for s in (state.stats_q or []) if isinstance(s, dict)]
    try:
        tf.recovered = _recovery(state, stored, view.session, config, now)
    except Exception:                                    # noqa: BLE001 — 恢复只是增强：失败按「不恢复」
        tf.recovered = False
        degraded.append("recovery")
    if session_id:
        try:
            exp = _fine_state.inflight_expired(session_id, now)
        except Exception:                                # noqa: BLE001
            exp = None
            degraded.append("inflight")
        if isinstance(exp, dict):
            gen = exp.get("gen_id")
            tf.lost_gen = gen if isinstance(gen, str) and gen else "?"
    try:
        _judge_supp(tf, state)
    except Exception:                                    # noqa: BLE001 — 判到一半抛了：不留半截判定，本轮不判补充
        tf.supp_candidates, tf.supp_gen, tf.supp_src_seq = [], None, None
        tf.late, tf.stale_dropped = False, 0
        degraded.append("supp")
    return tf


def recheck_supp(paths, entries: dict, prompt_keywords) -> "tuple[list, list]":
    """D1-b：本轮提问关键词（信号 J，**不含**信号 K 主题词）对该篇 tag / 摘要 / keywords / 文件名
    任一命中即通过。口径与打分一致：ASCII 词走词边界、含 CJK 走子串（`_scorer._kw_in_text`）。
    不在 `entries` 里的篇（已删除、归档、工作日志）两边都不进，调用方按「跳过」处理。"""
    kws = [k for k in (prompt_keywords or ()) if isinstance(k, str) and k]
    kept: list = []
    dropped: list = []
    for p in paths or []:
        e = entries.get(p) if isinstance(p, str) else None
        if e is None:
            continue
        name = p.rsplit("/", 1)[-1]
        name = name[:-3] if name.endswith(".md") else name
        hit = any(_keyword_hits_tags(k, e) or _keyword_hits_summary(k, e)
                  or _keyword_hits_keywords(k, e) or _kw_in_text(k, name) for k in kws)
        (kept if hit else dropped).append(p)
    return kept, dropped


def spawn_mode(tf: TurnFine, *, topics_ok: bool) -> "str | None":
    """首轮点名尚未成功 → pin；已成功且逐轮补充开 → supp；否则不发起（F35）。"""
    cfg = tf.view.config
    if not _fine_config.fine_rank_on(cfg):
        return None
    if not topics_ok:
        return "pin"
    return "supp" if _fine_config.fine_supplement_on(cfg) else None


def spawn_blocked(tf: TurnFine, *, gate_passed: bool, dry_run: bool, agent_type,
                  n_candidates: int, now: float) -> "str | None":
    """§6.1 门禁里与模式无关的部分；None = 可以发起（槽位与在飞标记由调用方随后抢）。"""
    if tf is None or not tf.session_id:
        return "no_session"
    if not gate_passed:
        return "gate"
    if dry_run:
        return "dry_run"
    if agent_type:
        return "subagent"
    st = tf.state
    if not tf.recovered and st.disabled_reason in _fine_state.DISABLED_REASONS:
        return f"disabled:{st.disabled_reason}"
    # 有效 streak = finish_turn 落盘后的值：恢复先清零、本轮失联再 +1。只看 begin 时读到的计数的话，
    # 第二次 lost 把计数推到上限的那一轮，门禁仍会放行一次付费发起
    streak = (0 if tf.recovered else (_int(st.fail_streak) or 0)) + (1 if tf.lost_gen else 0)
    if streak >= _fine_state.STREAK_LIMIT:
        return "disabled:streak"
    cfg = tf.view.config
    if (_int(st.calls) or 0) >= int(_fine_config.get_value(cfg, "fine_max_calls")):
        return "call_cap"
    pending = sum(_nonneg(s.get("bytes")) for s in tf.stats)
    if (_nonneg(st.bytes_sent) + pending
            >= int(_fine_config.get_value(cfg, "fine_session_max_bytes"))):
        return "bytes_cap"
    if n_candidates <= 0:
        return "no_candidates"
    try:
        if _fine_state.inflight_active(tf.session_id, now):
            return "inflight"
    except Exception:                                    # noqa: BLE001 — 读不了标记按「有在飞」保守处理
        return "inflight"
    return None


def finish_turn(tf: "TurnFine | None", *, consume_stats: bool = True) -> None:
    """本轮唯一一次条件写。fail-open：锁超时或任何异常不抛（异常打一行 stderr），记 `degraded=lock`，
    不放过期的在飞标记，下一轮照常。

    `consume_stats=False`：本轮读到的子进程统计**留在队列里**——不按 gen_id 出队、不把它们的字节并进
    `bytes_sent`、也不从它们里认 `no_claude`；其余合并照常（turn_seq、injected、补充、会话设置、失联、
    calls、停用派生）。UPS 只在 metrics 开着、且本轮没过闸（闸门轮）时这样传：闸门记录放不下统计，出队了
    就再也计不上，留到下一个过闸轮再计。过闸轮一律消费，记录构造失败也一样（外发字节记账优先于 metrics
    完整）。

    不变量：精排只在过闸轮发起，过闸轮必定出队，所以两次过闸之间队列至多新增 1 条，留队不会触到
    `STATS_Q_MAX`（5）的截断；这个上界不依赖 metrics 是否健康。「只在过闸轮发起」由 UPS 的
    `prompt_submit_load._Turn.past_gate` 保证：`_maybe_spawn` 在它为假时拒绝发起，`consume_stats` 也按它算。
    留队期间外发上限照样生效：`spawn_blocked` 本来就把未并账的队列字节算进去。
    """
    if tf is None or not tf.session_id:
        return
    now = time.time()
    cfg = tf.view.config
    try:
        max_calls = int(_fine_config.get_value(cfg, "fine_max_calls"))
        max_bytes = int(_fine_config.get_value(cfg, "fine_session_max_bytes"))
    except Exception:                                    # noqa: BLE001
        max_calls = int(_fine_config.default_of("fine_max_calls"))
        max_bytes = int(_fine_config.default_of("fine_session_max_bytes"))
    # 不消费统计时 gens 为空：下面的出队、字节并账与 no_claude 判定都只看出队条目，于是一并跳过
    gens = ({s.get("gen_id") for s in tf.stats if isinstance(s.get("gen_id"), str)}
            if consume_stats else set())

    def mutate(st: FineState) -> FineState:
        st.turn_seq = max(_int(st.turn_seq) or 0, tf.turn_seq)
        started = {k: v for k, v in (st.turn_started or {}).items()
                   if isinstance(k, str) and k.isdigit() and _num(v)}
        started[str(tf.turn_seq)] = tf.turn_ts
        keep = sorted(started, key=int)[-_fine_state.TURN_STARTED_KEEP:]
        st.turn_started = {k: started[k] for k in keep}
        injected = [p for p in (st.injected or []) if isinstance(p, str)]
        for p in tf.injected_add:
            if isinstance(p, str) and p and p not in injected:
                injected.append(p)
        st.injected = injected[-_fine_state.MAX_INJECTED:]
        if tf.supp_gen and isinstance(st.supp, dict) and st.supp.get("gen_id") == tf.supp_gen:
            st.supp = None
        if "settings" not in tf.degraded:       # 设置解析失败时 view.session 是空的：保留盘上已存的，不覆盖
            st.session_settings = dict(tf.view.session)
        q = [s for s in (st.stats_q or []) if isinstance(s, dict)]
        popped = [s for s in q if s.get("gen_id") in gens]
        st.stats_q = [s for s in q if s.get("gen_id") not in gens]
        # D11：外发字节以子进程实际送出的为准（stats_q 里的 bytes），在这里并账、只并一次
        st.bytes_sent = _nonneg(st.bytes_sent) + sum(_nonneg(s.get("bytes")) for s in popped)
        if tf.recovered:
            st.fail_streak, st.disabled_reason, st.disabled_at = 0, None, None
        if tf.lost_gen:
            st.fail_streak = (_int(st.fail_streak) or 0) + 1
            st.last_fail, st.last_ts = "lost", now
        if tf.spawned_gen:
            st.calls = (_int(st.calls) or 0) + 1
        if tf.spawn_result == "no_claude" or any(s.get("fail") == "no_claude" for s in popped):
            st.disabled_reason, st.disabled_at = "no_claude", now
            st.last_fail, st.last_ts = "no_claude", now
        if st.disabled_reason not in _fine_state.DISABLED_REASONS:
            st.disabled_reason = None
            reason = None
            if (_int(st.fail_streak) or 0) >= _fine_state.STREAK_LIMIT:
                reason = "streak"
            elif (_int(st.calls) or 0) >= max_calls:
                reason = "call_cap"
            elif st.bytes_sent >= max_bytes:
                reason = "bytes_cap"
            if reason:
                st.disabled_reason, st.disabled_at = reason, now
        return st

    try:
        written = _fine_state.update_fine(tf.session_id, mutate)
    except Exception as exc:                             # noqa: BLE001 — fail-open
        print(f"[vault-loader] 精排状态写回失败：{exc}", file=sys.stderr)
        written = None
    if written is None:
        # 条件写没落盘（锁超时 / IO 失败）：本轮的 lost 没记上，过期标记就不能放——放了这次失联永远丢了。
        # 留着它，下一轮 begin_turn 会再读到、再记
        if "lock" not in tf.degraded:
            tf.degraded.append("lock")
        return
    if tf.lost_gen:
        # 放掉「仍然过期」的标记（含 gen 合成的损坏标记）。按 begin_turn 的时刻判，与它记 lost 用的是同一个
        # 谓词：本轮 begin 之后才发起的子进程写的新标记，started 不早于 turn_ts，不会被当成过期删掉
        try:
            _fine_state.release_expired_inflight(tf.session_id, tf.turn_ts)
        except Exception:                                # noqa: BLE001
            pass
