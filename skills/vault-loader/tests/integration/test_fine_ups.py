# -*- coding: utf-8 -*-
"""UPS 接线一（spec 2026-10-13 §4、§8、§9）：轮次、补充投递与闸门轮不认领补充、粗排开关、工作日志排除、
统一行与快照。真入口 `prompt_submit_load.main()`，进程内跑以便拦住精排发起。"""
from __future__ import annotations

import io
import itertools
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import _fine_state, _metrics, _state
from scripts import prompt_submit_load as psl
from scripts._fine_state import FineState
from scripts._inject_format import PATH_NOTE, SUPP_HEADER
from scripts._output import INJECTION_NOTICE, reset_emit_guard

NOTICE = INJECTION_NOTICE.rstrip("\n")

# 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id（test_no_secrets_in_tree）
SID = "-".join(("11111111", "2222", "3333", "4444", "555555555555"))
_seq = itertools.count(1)
# mod 契约夹具（Task 13）：快照 `supps[]` 条目的键集合以它为准
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mod_contract" / "snapshot_v1.json"


def _pid() -> str:
    return "-".join(("aaaaaaaa", "bbbb", "cccc", "dddd", f"{next(_seq):012d}"))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.delenv("VAULT_LOADER_DISABLE", raising=False)


@pytest.fixture(autouse=True)
def spawned(monkeypatch):
    """本文件绝不真起精排子进程（detached 的付费 `claude -p`）。"""
    calls: list = []
    monkeypatch.setattr(psl, "spawn_fine",
                        lambda cwd, sid, payload: (calls.append(payload), "spawned")[1])
    return calls


NOTES = {
    "笔记/alpha.md": {"tags": ["hook"], "keywords": ["注入设计"],
                      "summary": "alpha 讲 hook 注入设计的取舍与实现细节，摘要足够长"},
    "笔记/beta.md": {"tags": ["hook"], "keywords": ["注入设计"],
                     "summary": "beta 讲 hook 注入设计的另一种做法，摘要也足够长"},
    "笔记/注入设计复盘.md": {"tags": [], "keywords": [],
                            "summary": "一次只在文件名里写着主题的复盘，摘要足够长但不含别的提问词"},
    "笔记/红烧肉.md": {"tags": ["烹饪"], "keywords": [],
                      "summary": "红烧肉的做法与火候控制要点，摘要足够长足够长"},
    "工作日志/2026年/10月/2026-10-01.md": {"tags": ["hook", "工作日志"], "category": "工作日志",
                                         "keywords": ["注入设计"],
                                         "summary": "hook 注入设计 工作日志模板摘要足够长足够长"},
}


def _setup(home: Path, vault: Path, wfc, extra_notes=None, **cfg_extra) -> None:
    entries = {}
    for rel, meta in {**NOTES, **(extra_notes or {})}.items():
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {Path(rel).stem}\n\n全文：{Path(rel).stem}\n", encoding="utf-8")
        entries[rel] = {"category": "笔记", "mtime": 1900000000, **meta}
    wfc(entries)
    cfg = {"vault_path": str(vault), "metrics": {"enabled": True}}
    for k, v in cfg_extra.items():
        if isinstance(v, dict):
            cfg.setdefault(k, {}).update(v)
        else:
            cfg[k] = v
    d = home / ".context-vault"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def _live(home: Path) -> Path:
    return home / ".context-vault" / "state" / "live"


def _req(home: Path, pid: str, obj=None) -> None:
    _live(home).mkdir(parents=True, exist_ok=True)
    (_live(home) / f"{_fine_state.sid_hash(SID)}-{pid}.req.json").write_text(
        json.dumps(obj if obj is not None else {"v": 1}), encoding="utf-8")


def _snap(home: Path) -> dict:
    return json.loads((_live(home) / f"{_fine_state.sid_hash(SID)}.json").read_text(encoding="utf-8"))


def _run(monkeypatch, cwd: Path, prompt: str, *, home: "Path | None" = None, request=None,
         extra: "dict | None" = None):
    pid = _pid()
    if request is not None and home is not None:
        _req(home, pid, request)
    payload = {"cwd": str(cwd), "prompt": prompt, "session_id": SID, "prompt_id": pid,
               "hook_event_name": "UserPromptSubmit", "transcript_path": ""}
    payload.update(extra or {})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload, ensure_ascii=False)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    # emit 的单次写出守卫是进程级的，conftest 只在每条用例开始时复位；同一用例里跑第二轮要自己复位
    reset_emit_guard()
    assert psl.main() == 0
    raw = out.getvalue().strip()
    return (json.loads(raw) if raw else {}), pid


def _ctx(out: dict) -> str:
    return (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""


def _in_ns(fn):
    """在与 hook 相同的命名空间里读写（configure_context 的判据与 main() 一致）。"""
    _state.configure_context("claude", SID)
    try:
        return fn()
    finally:
        _state.configure_context("unknown")


def _seed(**fields) -> None:
    _in_ns(lambda: _fine_state.update_fine(SID, lambda st: replace(st, **fields)))


def _fine() -> FineState:
    return _in_ns(lambda: _fine_state.load_fine(SID))


def _records(home: Path, pid: str) -> list:
    out = []
    for f in home.rglob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("prompt_id") == pid:
                    out.append(r)
    return out


def _full(home: Path, pid: str) -> dict:
    recs = [r for r in _records(home, pid) if "admitted" in r]
    assert recs, "本轮应有一条完整决策记录"
    return recs[-1]


def _supp(src: int, paths, ts=None, gen="g1") -> dict:
    return {"gen_id": gen, "src_seq": src, "paths": list(paths),
            "ts": time.time() - 1 if ts is None else ts}


ASK = "hook 注入设计 怎么做"
ASK_SUPP = "注入设计 复盘 怎么做"
SUPP_NOTE = "笔记/注入设计复盘.md"
# 补充那篇的正文（`_setup` 写入的全文）：补充小节只出摘要行，正文不得出现（auto_fulltext 默认关）
SUPP_BODY = "全文：注入设计复盘"


def test_turn_counted_and_injected_recorded(tmp_home, tmp_vault, tmp_git_repo,
                                            write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    out, _ = _run(monkeypatch, tmp_git_repo, ASK)
    ctx = _ctx(out)
    assert "「alpha」" in ctx and "「beta」" in ctx
    st = _fine()
    assert st.turn_seq == 1 and "1" in st.turn_started
    assert {"笔记/alpha.md", "笔记/beta.md"} <= set(st.injected)


def test_unified_line_carries_absolute_path_and_path_note(tmp_home, tmp_vault, tmp_git_repo,
                                                          write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    out, _ = _run(monkeypatch, tmp_git_repo, ASK)
    ctx = _ctx(out)
    abs_alpha = str((tmp_vault / "笔记" / "alpha.md").resolve())
    assert f"‖ 路径：{abs_alpha}" in ctx
    assert PATH_NOTE in ctx and "[[笔记/alpha.md]]" not in ctx


def test_worklog_never_in_coarse_list(tmp_home, tmp_vault, tmp_git_repo,
                                      write_frontmatter_cache, monkeypatch):
    """W1：工作日志按 tag 与 keywords 都会命中本轮提问，仍不得进粗排。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    out, _ = _run(monkeypatch, tmp_git_repo, ASK)
    assert "2026-10-01" not in _ctx(out)


def test_supp_delivered_next_turn_and_cleared(tmp_home, tmp_vault, tmp_git_repo,
                                              write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE]))
    out, pid = _run(monkeypatch, tmp_git_repo, ASK_SUPP)
    ctx = _ctx(out)
    assert SUPP_HEADER in ctx and "「注入设计复盘」" in ctx
    assert ctx.index(SUPP_HEADER) > ctx.index("📚 vault-loader 候选")
    assert f"‖ 路径：{(tmp_vault / SUPP_NOTE).resolve()}" in ctx
    assert SUPP_BODY not in ctx                          # 补充段只出摘要行，不走全文回退
    st = _fine()
    assert st.supp is None and st.turn_seq == 2 and SUPP_NOTE in st.injected
    rec = _full(tmp_home, pid)
    assert rec["supp_shown"] == [SUPP_NOTE] and rec["n_supp_delivered"] == 1
    assert rec["n_supp_gen"] == 1 and rec["n_supp_late_delivered"] == 0
    assert "🔎" in out.get("systemMessage", "")


def test_supp_recheck_drops_unrelated_note(tmp_home, tmp_vault, tmp_git_repo,
                                           write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(turn_seq=1, supp=_supp(1, ["笔记/红烧肉.md"]))
    out, pid = _run(monkeypatch, tmp_git_repo, ASK)
    assert SUPP_HEADER not in _ctx(out) and "「红烧肉」" not in _ctx(out)
    assert _fine().supp is None
    rec = _full(tmp_home, pid)
    assert rec["n_supp_drop_recheck"] == 1 and rec["supp_shown"] == []


def test_topic_words_do_not_count_for_recheck(tmp_home, tmp_vault, tmp_git_repo,
                                              write_frontmatter_cache, monkeypatch):
    """D1-b：复核只用本轮提问词；会话主题词（信号 K）命中不算。"""
    from scripts import _topic
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _in_ns(lambda: _topic.save_session_topic(tmp_git_repo, SID, ["烹饪"], pinned=[],
                                             accounted=False, state="ok"))
    _seed(turn_seq=1, supp=_supp(1, ["笔记/红烧肉.md"]))
    out, _ = _run(monkeypatch, tmp_git_repo, ASK)
    assert "「红烧肉」" not in _ctx(out)


GATE_PROMPTS = pytest.mark.parametrize("prompt,gate", [("ok", "deictic_only"), ("a", "too_few_keywords")],
                                        ids=["deictic", "too_few"])


@GATE_PROMPTS
def test_gate_round_neither_claims_nor_delivers_supp(prompt, gate, tmp_home, tmp_vault, tmp_git_repo,
                                                     write_frontmatter_cache, monkeypatch):
    """2.4.0 批次 3c：指代型与关键词不足的闸门轮不投递补充，也不认领它——盘上那份原样留给下一个过闸轮。
    记录仍是 gate 五键（集合相等，不是子集）。"""
    from scripts import _topic
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _in_ns(lambda: _topic.save_session_topic(tmp_git_repo, SID, ["注入设计"],
                                             pinned=["笔记/alpha.md"], accounted=False, state="ok"))
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE]))
    out, pid = _run(monkeypatch, tmp_git_repo, prompt, home=tmp_home, request={"v": 1})
    ctx = _ctx(out)
    assert SUPP_HEADER not in ctx and "「注入设计复盘」" not in ctx
    assert psl.PIN_HEADER not in ctx and "「alpha」" not in ctx
    snap = _snap(tmp_home)
    assert snap["status"] == f"gate:{gate}" and not snap["supps"]
    recs = _records(tmp_home, pid)
    assert len(recs) == 1 and recs[0]["gate"] == gate
    keys = set(recs[0]) - {"runtime", "event_id"}       # canonical 命名空间字段不在键集合里（Task 8）
    assert keys == set(_metrics.GATE_RECORD_KEYS), keys ^ set(_metrics.GATE_RECORD_KEYS)
    st = _fine()
    assert st.turn_seq == 2                                              # 闸门轮照常计轮次（D1）
    assert st.supp is not None and (st.supp["gen_id"], st.supp["paths"]) == ("g1", [SUPP_NOTE])
    assert SUPP_NOTE not in st.injected


@GATE_PROMPTS
def test_supp_left_by_gate_round_is_stale_dropped_and_counted_next_round(
        prompt, gate, tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache, monkeypatch):
    """评审 R2：补充写回早于闸门轮开始。闸门轮不认领，下一个过闸轮是第 src+2 轮、不在放宽档，按时效丢弃，
    并计进那一轮完整记录的 `n_supp_gen` 与 `n_supp_drop_stale`——漏斗的「产生」不因闸门轮少计。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE]))
    out1, _ = _run(monkeypatch, tmp_git_repo, prompt)
    out2, pid2 = _run(monkeypatch, tmp_git_repo, ASK_SUPP)
    for ctx in (_ctx(out1), _ctx(out2)):
        assert SUPP_HEADER not in ctx and "「注入设计复盘」" not in ctx
    rec = _full(tmp_home, pid2)
    assert (rec["n_supp_gen"], rec["n_supp_drop_stale"], rec["n_supp_delivered"]) == (1, 1, 0)
    assert _fine().supp is None                                          # 过闸轮判过了，照清


def test_supp_written_after_gate_round_start_delivered_on_d10_band(tmp_home, tmp_vault, tmp_git_repo,
                                                                   write_frontmatter_cache, monkeypatch):
    """评审 R2 的另一半：补充写回晚于闸门轮（第 src+1 轮）开始，第 src+2 轮按 D10 放宽档投递，与改动前相同。
    `turn_started` 由真实闸门轮写入（预置会被它的 finish_turn 覆盖），补充随后以更晚的时刻写回。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(turn_seq=1)
    _run(monkeypatch, tmp_git_repo, "ok")
    started = _fine().turn_started["2"]
    _seed(supp=_supp(1, [SUPP_NOTE], ts=started + 1.0))
    out, pid = _run(monkeypatch, tmp_git_repo, ASK_SUPP)
    assert SUPP_HEADER in _ctx(out) and "「注入设计复盘」" in _ctx(out)
    rec = _full(tmp_home, pid)
    assert (rec["n_supp_delivered"], rec["n_supp_late_delivered"]) == (1, 1)


def test_gate_round_without_supp_keeps_five_key_record_and_counts(tmp_home, tmp_vault, tmp_git_repo,
                                                                  write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _, pid = _run(monkeypatch, tmp_git_repo, "a")             # too_few_keywords
    recs = _records(tmp_home, pid)
    # canonical 命名空间下 `runtime_record_fields()` 带 runtime 与 event_id 两键（同 `test_gate_round_neither_claims_nor_delivers_supp` 的剥法）
    assert recs and set(recs[-1]) - {"runtime", "event_id"} == {"_schema", "ts", "session", "prompt_id",
                                                                "gate"}
    assert _fine().turn_seq == 1                               # 关键词不足的轮次也计轮次（D1）


def test_slash_skipped_and_system_rounds_do_not_count(tmp_home, tmp_vault, tmp_git_repo,
                                                      write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache,
           relevance={"skip_slash_commands": ["bug-batch"]})
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE]))
    _run(monkeypatch, tmp_git_repo, "/bug-batch 21292")
    _run(monkeypatch, tmp_git_repo, "<task-notification>\n<task-id>x</task-id>")
    st = _fine()
    assert st.turn_seq == 1 and st.supp is not None            # 两轮都不读不写
    out, _ = _run(monkeypatch, tmp_git_repo, ASK_SUPP)
    assert "「注入设计复盘」" in _ctx(out)                      # 下一轮仍是 src+1，照常投递


def test_coarse_off_first_turn_injects_nothing(tmp_home, tmp_vault, tmp_git_repo,
                                               write_frontmatter_cache, monkeypatch):
    """F31 / Q28：粗排关时打分照跑、不渲染清单、不写 per-cwd 去重集，首轮没有点名与补充 ⇒ 空。"""
    from scripts._state import load_already_injected
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, relevance={"coarse_inject": False})
    out, pid = _run(monkeypatch, tmp_git_repo, ASK)
    assert _ctx(out) == ""
    rec = _full(tmp_home, pid)
    assert rec["shown"] == [] and rec["n_admitted"] >= 2 and rec["coarse_on"] is False
    assert _fine().turn_seq == 1
    assert _in_ns(lambda: load_already_injected(tmp_git_repo, 24)) == set()


def test_rapid_prompts_drop_stale_supp(tmp_home, tmp_vault, tmp_git_repo,
                                       write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(turn_seq=3, supp=_supp(1, [SUPP_NOTE]))
    out, pid = _run(monkeypatch, tmp_git_repo, ASK_SUPP)
    assert "「注入设计复盘」" not in _ctx(out)
    rec = _full(tmp_home, pid)
    assert rec["n_supp_drop_stale"] == 1 and rec["n_supp_gen"] == 1
    assert _fine().supp is None


def test_late_supp_delivered_once_on_d10_band(tmp_home, tmp_vault, tmp_git_repo,
                                              write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    t0 = time.time() - 60
    _seed(turn_seq=2, turn_started={"2": t0}, supp=_supp(1, [SUPP_NOTE], ts=t0 + 30))
    out, pid = _run(monkeypatch, tmp_git_repo, ASK_SUPP)
    assert "「注入设计复盘」" in _ctx(out)
    assert _full(tmp_home, pid)["n_supp_late_delivered"] == 1


def test_auto_fulltext_is_opt_in(tmp_home, tmp_vault, tmp_git_repo,
                                 write_frontmatter_cache, monkeypatch):
    notes = {"笔记/全文.md": {"tags": ["gradle", "构建"], "keywords": ["gradle", "构建"],
                              "summary": "gradle 构建 调优 的完整记录，摘要足够长足够长"}}
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, extra_notes=notes)
    out, _ = _run(monkeypatch, tmp_git_repo, "gradle 构建 调优")
    assert "强命中" not in _ctx(out) and "「全文」" in _ctx(out)
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, extra_notes=notes,
           relevance={"auto_fulltext": True})
    out2, _ = _run(monkeypatch, tmp_git_repo, "gradle 构建 调优")
    assert "强命中" in _ctx(out2) and "全文：全文" in _ctx(out2)


def test_vault_root_and_supps_in_snapshot(tmp_home, tmp_vault, tmp_git_repo,
                                          write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE, "笔记/红烧肉.md"]))
    _run(monkeypatch, tmp_git_repo, ASK_SUPP, home=tmp_home, request={"v": 1})
    snap = _snap(tmp_home)
    assert snap["vault_root"] == str(tmp_vault.resolve())
    by = {s["path"]: s for s in snap["supps"]}
    assert (by[SUPP_NOTE]["recheck"], by[SUPP_NOTE]["rendered"], by[SUPP_NOTE]["src_seq"]) == ("passed", True, 1)
    # 复核未命中记 `failed`（契约值域 `_session_ops.SUPP_RECHECKS`，mod 按它取文案）
    assert (by["笔记/红烧肉.md"]["recheck"], by["笔记/红烧肉.md"]["rendered"]) == ("failed", False)


def test_supps_item_shape_matches_contract_fixture(tmp_home, tmp_vault, tmp_git_repo,
                                                   write_frontmatter_cache, monkeypatch):
    """真实入口产出的 `supps[]` 条目与契约夹具同键：`test_snapshot_shape_matches_contract_fixture` 那一轮
    没有补充，`supps` 是空列表、它的同形比较对条目级键是空网。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE]))
    _run(monkeypatch, tmp_git_repo, ASK_SUPP, home=tmp_home, request={"v": 1})
    snap = _snap(tmp_home)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert snap["supps"] and fixture["supps"]                 # 两边都有条目，比较才不是空网
    for item in snap["supps"]:
        assert set(item) == set(fixture["supps"][0]), sorted(set(item) ^ set(fixture["supps"][0]))


def test_finish_write_failure_records_lock_degraded(tmp_home, tmp_vault, tmp_git_repo,
                                                    write_frontmatter_cache, monkeypatch):
    """fine 写回在 metrics annotate 之前做：条件写失败（锁超时 / IO）记的 `degraded=lock` 随本轮记录落盘
    （spec §7）。写回放在 flush 之后时这个码没有消费者。注入照常。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    monkeypatch.setattr(_fine_state, "update_fine", lambda *a, **k: None)
    out, pid = _run(monkeypatch, tmp_git_repo, ASK)
    assert "「alpha」" in _ctx(out)
    assert "lock" in _full(tmp_home, pid)["degraded"]


# ── F25 投递前重核与 fine_max_notes 上限（三个出口）────────────────────────────────

# 第二篇补充：摘要里有「复盘」，过本轮提问词复核；只有这一个摘要命中，够不上粗排放行
SUPP_NOTE2 = "笔记/复盘二.md"
SUPP2 = {SUPP_NOTE2: {"tags": [], "keywords": [],
                      "summary": "另一份只在摘要里写着复盘二字的记录，摘要足够长足够长"}}


def test_supp_not_delivered_when_excluded_on_scored_round(tmp_home, tmp_vault, tmp_git_repo,
                                                          write_frontmatter_cache, monkeypatch):
    """F25：本会话排除了那篇，打分轮就不投递它（提问能让它过复核，排除是唯一拦它的理由）。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE]))
    out, _ = _run(monkeypatch, tmp_git_repo, ASK_SUPP, home=tmp_home,
                  request={"v": 1, "exclude": [SUPP_NOTE]})
    ctx = _ctx(out)
    assert "📚 vault-loader 候选" in ctx                            # 确是有正文的打分轮
    assert SUPP_HEADER not in ctx and "「注入设计复盘」" not in ctx
    assert _fine().supp is None                                     # 判过了，照清


def test_supp_not_delivered_when_already_injected_this_session(tmp_home, tmp_vault, tmp_git_repo,
                                                               write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE]), injected=[SUPP_NOTE])
    out, _ = _run(monkeypatch, tmp_git_repo, ASK_SUPP)
    ctx = _ctx(out)
    assert "📚 vault-loader 候选" in ctx
    assert SUPP_HEADER not in ctx and "「注入设计复盘」" not in ctx


@pytest.mark.parametrize("rel", [{"fine_supplement": False}, {"fine_rank": False}],
                         ids=["supplement_off", "rank_off"])
def test_supp_not_delivered_when_switch_off(rel, tmp_home, tmp_vault, tmp_git_repo,
                                            write_frontmatter_cache, monkeypatch):
    """F25：补充在开关打开时生成，投递时开关已关（逐轮补充关，或精排总开关关）就不投。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, relevance=rel)
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE]))
    out, _ = _run(monkeypatch, tmp_git_repo, ASK_SUPP)
    ctx = _ctx(out)
    assert "📚 vault-loader 候选" in ctx
    assert SUPP_HEADER not in ctx and "「注入设计复盘」" not in ctx


@pytest.mark.parametrize("prompt,rel", [(ASK_SUPP, {}), (ASK_SUPP, {"coarse_inject": False})],
                         ids=["scored", "coarse_off"])
def test_supp_capped_by_fine_max_notes_on_every_exit(prompt, rel, tmp_home, tmp_vault, tmp_git_repo,
                                                     write_frontmatter_cache, monkeypatch):
    """两个渲染出口各自传上限（打分轮、粗排关 / 单独成篇）：两篇都合格、上限 1 时只出第一篇
    （按模型给的顺序截）。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, extra_notes=SUPP2,
           relevance={"fine_max_notes": 1, **rel})
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE, SUPP_NOTE2]))
    out, pid = _run(monkeypatch, tmp_git_repo, prompt)
    ctx = _ctx(out)
    assert SUPP_HEADER in ctx and "「注入设计复盘」" in ctx and "「复盘二」" not in ctx
    assert [r["supp_shown"] for r in _records(tmp_home, pid)] == [[SUPP_NOTE]]


# ── 补充小节的隔离声明接线 ─────────────────────────────────────────────────────

def test_supp_section_alone_carries_notice_when_coarse_off(tmp_home, tmp_vault, tmp_git_repo,
                                                           write_frontmatter_cache, monkeypatch):
    """粗排关、没有点名：补充小节单独成篇，开头必须是隔离声明。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, relevance={"coarse_inject": False})
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE]))
    out, _ = _run(monkeypatch, tmp_git_repo, ASK_SUPP)
    ctx = _ctx(out)
    assert SUPP_HEADER in ctx and psl.PIN_HEADER not in ctx
    assert ctx.startswith(NOTICE)


def test_supp_section_after_fulltext_fence_carries_notice(tmp_home, tmp_vault, tmp_git_repo,
                                                          write_frontmatter_cache, monkeypatch):
    """全文分支：正文里唯一的隔离声明在 nonce fence 之内（属于全文正文），补充小节追加在 fence 外，必须
    自带一份——最后一次出现在 fence 闭合之后、补充小节头之前。"""
    notes = {"笔记/全文.md": {"tags": ["gradle", "构建"], "keywords": ["gradle", "构建"],
                              "summary": "gradle 构建 调优 的完整记录，摘要足够长足够长"},
             "笔记/调优复盘.md": {"tags": [], "keywords": [],
                                 "summary": "一份只在文件名里写着主题的记录，摘要足够长足够长"}}
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, extra_notes=notes,
           relevance={"auto_fulltext": True})
    _seed(turn_seq=1, supp=_supp(1, ["笔记/调优复盘.md"]))
    out, _ = _run(monkeypatch, tmp_git_repo, "gradle 构建 调优")
    ctx = _ctx(out)
    assert "强命中" in ctx and SUPP_HEADER in ctx and psl.PIN_HEADER not in ctx
    assert ctx.rindex(NOTICE) > ctx.rindex(">>>")                # fence 闭合之后另有一份
    assert ctx.index(SUPP_HEADER) > ctx.rindex(NOTICE)


def test_fine_flow_unavailable_keeps_recall(tmp_home, tmp_vault, tmp_git_repo,
                                            write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    monkeypatch.setattr(psl, "_fine_flow", None)
    out, _ = _run(monkeypatch, tmp_git_repo, ASK)
    assert "「alpha」" in _ctx(out)


# 一条已写回的子进程统计（键取 `_fine_state.STATS_KEYS`）：数值互不相同，记错字段能看出来
STAT = {"gen_id": "s1", "mode": "supp", "ok": True, "fail": None, "none": False, "n_sel": 1,
        "n_echo_mismatch": 2, "n_catalog": 5, "n_catalog_dropped": 1, "n_summary_layer": 3,
        "bytes": 1234, "degraded": ["transcript_none"]}


def _assert_stat_counted(rec: dict) -> None:
    assert (rec["n_fine_done"], rec["bytes_sent"], rec["n_echo_mismatch"], rec["n_catalog"],
            rec["n_catalog_dropped"], rec["n_summary_layer"], rec["llm_none"]) == (1, 1234, 2, 5, 1, 3, 0)
    assert rec["n_fine_fail"] == 0              # STAT 是成功的调用：n_fine_done 含失败，失败数单独落
    assert "transcript_none" in rec["degraded"]


@pytest.mark.parametrize("prompt,with_supp", [("a", False), ("ok", True)], ids=["gate", "gate_with_supp"])
def test_stats_stay_queued_on_non_full_rounds_then_count_on_full_round(
        prompt, with_supp, tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache, monkeypatch):
    """metrics 开时统计只能记在完整决策记录上：闸门轮不出队（出队就再也计不上），到下一个过闸的完整轮
    计一次、出队并账。盘上有到期补充时闸门轮同样不投递、不认领。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    seed: dict = {"stats_q": [dict(STAT)]}
    if with_supp:
        seed.update(turn_seq=1, supp=_supp(1, [SUPP_NOTE]))
    _seed(**seed)
    _, pid1 = _run(monkeypatch, tmp_git_repo, prompt)
    recs1 = _records(tmp_home, pid1)
    assert recs1 and all("admitted" not in r and "n_fine_done" not in r for r in recs1)
    if with_supp:
        assert "supp_shown" not in recs1[-1]                         # 闸门轮不投递补充
        assert _fine().supp is not None                              # 也不认领：盘上那份还在
    st = _fine()
    assert [s["gen_id"] for s in st.stats_q] == ["s1"] and st.bytes_sent == 0
    assert st.turn_seq == (2 if with_supp else 1)                    # 其余合并照常
    _, pid2 = _run(monkeypatch, tmp_git_repo, ASK)
    _assert_stat_counted(_full(tmp_home, pid2))
    st = _fine()
    assert st.stats_q == [] and st.bytes_sent == 1234


def test_stats_consumed_on_gate_round_when_metrics_off(tmp_home, tmp_vault, tmp_git_repo,
                                                       write_frontmatter_cache, monkeypatch):
    """metrics 关时外发字节照常并账：记账不依赖 metrics，闸门轮也出队。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, metrics={"enabled": False})
    _seed(stats_q=[dict(STAT)])
    _, pid = _run(monkeypatch, tmp_git_repo, "a")
    assert _records(tmp_home, pid) == []
    st = _fine()
    assert st.stats_q == [] and st.bytes_sent == 1234 and st.turn_seq == 1


def test_stats_consumed_on_gated_round_even_if_record_build_fails(
        tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache, monkeypatch):
    """出队判据是「本轮过闸」而不是「本轮有完整记录」：metrics 开、过闸，但记录构造失败（metrics 模块
    不可用或 build_record 抛异常）时照样出队并账——外发字节记账优先于 metrics 完整，否则统计永远出不了
    队、超过 STATS_Q_MAX 的被截掉，外发上限被低估。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(stats_q=[dict(STAT)])

    def boom(*_a, **_k):
        raise RuntimeError("build_record boom")

    monkeypatch.setattr(_metrics, "build_record", boom)
    out, pid = _run(monkeypatch, tmp_git_repo, ASK)
    assert "「alpha」" in _ctx(out)                                  # 召回照常（fail-open）
    assert not [r for r in _records(tmp_home, pid) if "admitted" in r]   # 确实没有完整记录
    st = _fine()
    assert st.stats_q == [] and st.bytes_sent == 1234 and st.turn_seq == 1


def test_stats_not_counted_when_write_back_fails_then_counted_once(
        tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache, monkeypatch):
    """写回失败（锁超时 / IO）时统计没出队：本轮记录不计它（下一轮还会读到），只记 lock；下一轮写成功时
    计且只计一次。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(stats_q=[dict(STAT)])
    real_update = _fine_state.update_fine
    monkeypatch.setattr(_fine_state, "update_fine", lambda *a, **k: None)
    out, pid1 = _run(monkeypatch, tmp_git_repo, ASK)
    assert "「alpha」" in _ctx(out)
    rec1 = _full(tmp_home, pid1)
    assert "lock" in rec1["degraded"] and "transcript_none" not in rec1["degraded"]
    assert (rec1["n_fine_done"], rec1["bytes_sent"], rec1["n_catalog"], rec1["n_echo_mismatch"]) == (0, 0, 0, 0)
    assert [s["gen_id"] for s in _fine().stats_q] == ["s1"]
    monkeypatch.setattr(_fine_state, "update_fine", real_update)
    _, pid2 = _run(monkeypatch, tmp_git_repo, ASK_SUPP)
    rec2 = _full(tmp_home, pid2)
    _assert_stat_counted(rec2)
    assert "lock" not in rec2["degraded"]
    st = _fine()
    assert st.stats_q == [] and st.bytes_sent == 1234


def test_dry_run_skips_fine_entirely(tmp_home, tmp_vault, tmp_git_repo,
                                     write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, dry_run=True)
    _seed(turn_seq=1, supp=_supp(1, [SUPP_NOTE]))
    out, _ = _run(monkeypatch, tmp_git_repo, ASK_SUPP)
    assert _ctx(out) == "" and "[DRY-RUN]" in out.get("systemMessage", "")
    st = _fine()
    assert st.turn_seq == 1 and st.supp is not None


# ── Task 17：精排发起、会话设置、persist、Read 记录 ──────────────────────────

CFG_KEYS = {"model", "timeout", "pin_max", "supp_max", "recent_prompts", "catalog",
            "catalog_max_chars", "fine_exclude_tags", "exclude_note_tags", "top_n",
            "project_top_k", "pool_max", "recent_worklog_days", "max_recent_worklogs",
            "use_keywords", "use_tag_idf", "tag_idf_floor", "weights", "jcfg"}


def test_first_turn_spawns_pin_mode_with_payload(tmp_home, tmp_vault, tmp_git_repo,
                                                 write_frontmatter_cache, monkeypatch, spawned):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _run(monkeypatch, tmp_git_repo, ASK, extra={"transcript_path": "T.jsonl"})
    assert len(spawned) == 1
    p = spawned[0]
    assert (p["v"], p["mode"], p["src_seq"]) == (1, "pin", 1)
    assert len(p["gen_id"]) == 16 and isinstance(p["slot"], str) and p["slot"]
    assert p["prompt"] == ASK and p["transcript_path"] == "T.jsonl"
    assert p["vault_path"] == str(tmp_vault)
    assert set(p["cfg"]) == CFG_KEYS
    assert p["cfg"]["model"] == "haiku" and p["cfg"]["pin_max"] == 1 and p["cfg"]["supp_max"] == 3
    assert {"笔记/alpha.md", "笔记/beta.md"} <= set(p["injected"])   # 本轮粗排展示篇也交给子进程排除
    assert _fine().calls == 1
    marker = _in_ns(lambda: _fine_state.inflight_active(SID, time.time()))
    assert marker and marker["gen_id"] == p["gen_id"]


def test_supp_mode_after_first_round_succeeded(tmp_home, tmp_vault, tmp_git_repo,
                                               write_frontmatter_cache, monkeypatch, spawned):
    from scripts import _topic
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _in_ns(lambda: _topic.save_session_topic(tmp_git_repo, SID, ["注入设计"], pinned=[],
                                             accounted=False, state="ok"))
    _run(monkeypatch, tmp_git_repo, ASK)
    assert [p["mode"] for p in spawned] == ["supp"]
    assert spawned[0]["prev_words"] == ["注入设计"]


@pytest.mark.parametrize("rel", [{"fine_rank": False}, {"session_topic": False}])
def test_fine_rank_off_blocks_spawn(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache,
                                    monkeypatch, spawned, rel):
    """新键与旧键任一为 false 即关（spec §5.3）；记录里 topic_on 与 fine_on 同取单点的值（F36）。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, relevance=rel)
    _, pid = _run(monkeypatch, tmp_git_repo, ASK)
    assert spawned == []
    rec = _full(tmp_home, pid)
    assert rec["topic_on"] is False and rec["fine_on"] is False


@pytest.mark.parametrize("bad", [
    pytest.param(None, id="control-valid"),
    pytest.param('{"relevance": {"fine_rank": false},}', id="trailing-comma"),
    pytest.param("[1, 2, 3]", id="array"),
])
def test_corrupt_config_does_not_spawn(tmp_home, tmp_git_repo, monkeypatch, spawned, bad):
    """config 读坏、整份回落默认值的一轮（默认值里精排是开的）不发起精排子进程，粗排照常（2.4.0 full-review DO-M2）。

    vault 放在默认位置：config 读坏时 vault_path 也回落默认，放在别处这一轮会在「vault 不可达」早退、走不到发起，
    用例恒绿。对照组是同一布局下可解析的 config，必须发起一次。"""
    from context_vault.paths import default_vault
    vault = default_vault()

    def wfc(entries):
        (vault / ".meta").mkdir(parents=True, exist_ok=True)
        (vault / ".meta" / "frontmatter-cache.json").write_text(
            json.dumps({"_version": 1, "entries": entries}, ensure_ascii=False), encoding="utf-8")

    _setup(tmp_home, vault, wfc)
    if bad is not None:
        (tmp_home / ".context-vault" / "config.json").write_text(bad, encoding="utf-8")
    out, _ = _run(monkeypatch, tmp_git_repo, ASK)
    assert "「alpha」" in _ctx(out), "粗排召回应照常"
    assert len(spawned) == (1 if bad is None else 0)


def test_subagent_payload_blocks_spawn(tmp_home, tmp_vault, tmp_git_repo,
                                       write_frontmatter_cache, monkeypatch, spawned):
    """UPS payload 恒带 agent_type 键（主会话为 null，2.1.296 探针实测）：判真值。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _run(monkeypatch, tmp_git_repo, ASK, extra={"agent_type": "general-purpose"})
    assert spawned == []
    _run(monkeypatch, tmp_git_repo, ASK, extra={"agent_type": None})
    assert len(spawned) == 1


def test_inflight_blocks_second_spawn(tmp_home, tmp_vault, tmp_git_repo,
                                      write_frontmatter_cache, monkeypatch, spawned):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _run(monkeypatch, tmp_git_repo, ASK)
    _run(monkeypatch, tmp_git_repo, ASK)
    assert len(spawned) == 1
    assert _fine().calls == 1


def test_spawn_failure_rolls_back_markers(tmp_home, tmp_vault, tmp_git_repo,
                                          write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    monkeypatch.setattr(psl, "spawn_fine", lambda cwd, sid, payload: "spawn_error")
    _run(monkeypatch, tmp_git_repo, ASK)
    assert _in_ns(lambda: _fine_state.inflight_active(SID, time.time())) is None
    slots = _in_ns(_fine_state.slots_dir)
    assert not slots.exists() or not any(slots.iterdir())
    assert _fine().calls == 0


def test_no_claude_disables_until_reprobe(tmp_home, tmp_vault, tmp_git_repo,
                                          write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    monkeypatch.setattr(psl, "spawn_fine", lambda cwd, sid, payload: "no_claude")
    _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home, request={"v": 1})
    assert _fine().disabled_reason == "no_claude"
    snap = _snap(tmp_home)
    assert (snap["llm"]["state"], snap["llm"]["fail"]) == ("unavailable", "no_claude")
    calls: list = []
    monkeypatch.setattr(psl, "spawn_fine",
                        lambda cwd, sid, payload: (calls.append(payload), "spawned")[1])
    _run(monkeypatch, tmp_git_repo, ASK)
    assert calls == []


def test_streak_disabled_blocks_spawn(tmp_home, tmp_vault, tmp_git_repo,
                                      write_frontmatter_cache, monkeypatch, spawned):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(fail_streak=2, disabled_reason="streak", disabled_at=time.time())
    _run(monkeypatch, tmp_git_repo, ASK)
    assert spawned == []


def test_call_cap_reached_blocks_spawn(tmp_home, tmp_vault, tmp_git_repo,
                                       write_frontmatter_cache, monkeypatch, spawned):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(calls=20)
    _run(monkeypatch, tmp_git_repo, ASK)
    assert spawned == [] and _fine().disabled_reason == "call_cap"


def test_session_settings_apply_and_survive_requestless_turn(tmp_home, tmp_vault, tmp_git_repo,
                                                             write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    out, _ = _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home,
                  request={"v": 1, "settings": {"coarse_inject": False}})
    assert "「alpha」" not in _ctx(out)
    s = _snap(tmp_home)["settings"]
    assert s["effective"]["coarse_inject"] is False and s["session"] == {"coarse_inject": False}
    assert s["source"]["coarse_inject"] == "session"
    assert _fine().session_settings == {"coarse_inject": False}
    out2, _ = _run(monkeypatch, tmp_git_repo, ASK)            # 没有 mod 请求：沿用已存的会话设置
    assert "「alpha」" not in _ctx(out2)


def test_config_false_locks_fine_rank(tmp_home, tmp_vault, tmp_git_repo,
                                      write_frontmatter_cache, monkeypatch, spawned):
    """D5：config 显式关掉的精排，会话设置打不开。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, relevance={"fine_rank": False})
    _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home,
         request={"v": 1, "settings": {"fine_rank": True}})
    snap = _snap(tmp_home)
    assert "fine_rank" in snap["settings"]["locked"]
    assert snap["settings"]["effective"]["fine_rank"] is False
    assert {"what": "settings", "key": "fine_rank", "reason": "locked"} in snap["rejected"]
    assert spawned == []


def _cfg_file(home: Path) -> Path:
    return home / ".context-vault" / "config.json"


def test_persist_only_narrows(tmp_home, tmp_vault, tmp_git_repo,
                              write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    out, _ = _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home,
                  request={"v": 1, "settings": {"coarse_inject": False, "fine_max_notes": 5},
                           "persist": ["coarse_inject", "fine_max_notes"]})
    cfg = json.loads(_cfg_file(tmp_home).read_text(encoding="utf-8"))
    assert cfg["relevance"]["coarse_inject"] is False
    assert "fine_max_notes" not in cfg["relevance"]                     # 放宽方向一律拒
    snap = _snap(tmp_home)
    # 快照 `persisted[]` 是面板键名字符串（Task 13 接口）：写成 {key, value} 对象时 mod 的 strings() 把它们
    # 全部滤掉，页脚永远不出「已设为默认」。与契约夹具比元素类型，夹具里的 persisted 必须非空才不是空网
    assert snap["persisted"] == ["coarse_inject"]
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["persisted"], "夹具 persisted 为空，形状对拍是空网"
    assert {type(x) for x in snap["persisted"]} == {type(x) for x in fixture["persisted"]} == {str}
    assert {"what": "persist", "key": "fine_max_notes", "reason": "widen"} in snap["rejected"]
    assert "relevance.coarse_inject" in out.get("systemMessage", "")


def test_persist_skipped_when_config_fell_back(tmp_home, tmp_git_repo, monkeypatch):
    """config 读坏回落默认值的那一轮不执行 persist，也不拒收（与标注「不 ack 也不拒收」同口径，spec §11）。
    默认 vault 预先建好，让本轮走到读会话设置那一步——否则在 vault 不可达处就早退，守不住这条。"""
    kv = tmp_home / ".context-vault" / "knowledge-vault"
    (kv / ".meta").mkdir(parents=True)
    (kv / "笔记").mkdir()
    (kv / "笔记" / "alpha.md").write_text("# alpha\n", encoding="utf-8")
    (kv / ".meta" / "frontmatter-cache.json").write_text(json.dumps({"_version": 1, "entries": {
        "笔记/alpha.md": {"tags": ["hook"], "keywords": ["注入设计"], "category": "笔记",
                          "summary": "alpha 讲 hook 注入设计的取舍与实现细节，摘要足够长",
                          "mtime": 1900000000}}}, ensure_ascii=False), encoding="utf-8")
    _cfg_file(tmp_home).write_text("{坏", encoding="utf-8")
    _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home,
         request={"v": 1, "settings": {"coarse_inject": False}, "persist": ["coarse_inject"]})
    assert _cfg_file(tmp_home).read_text(encoding="utf-8") == "{坏"
    snap = _snap(tmp_home)
    assert snap["status"] in ("scored", "dry_run")                     # 确实走到了打分轮
    assert snap["persisted"] == []
    assert not any(r.get("what") == "persist" for r in snap["rejected"])


def test_persist_keeps_bom_config_contents(tmp_home, tmp_vault, tmp_git_repo,
                                           write_frontmatter_cache, monkeypatch):
    import codecs
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    p = _cfg_file(tmp_home)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["display"] = {"verbosity": "compact"}
    p.write_bytes(codecs.BOM_UTF8 + json.dumps(data, ensure_ascii=False).encode("utf-8"))
    _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home,
         request={"v": 1, "settings": {"coarse_inject": False}, "persist": ["coarse_inject"]})
    after = json.loads(p.read_text(encoding="utf-8-sig"))
    assert after["display"] == {"verbosity": "compact"} and after["vault_path"] == str(tmp_vault)
    assert after["relevance"]["coarse_inject"] is False


def test_reads_recorded_against_index(tmp_home, tmp_vault, tmp_git_repo,
                                      write_frontmatter_cache, monkeypatch):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(injected=["笔记/alpha.md"])
    _, pid = _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home,
                  request={"v": 1, "reads": ["笔记/alpha.md", "笔记/beta.md", "不存在.md"]})
    rec = _full(tmp_home, pid)
    assert rec["vault_reads"] == ["笔记/alpha.md", "笔记/beta.md"]
    assert rec["n_vault_reads"] == 2 and rec["n_vault_reads_injected"] == 1


def test_bad_model_reported_and_falls_back(tmp_home, tmp_vault, tmp_git_repo,
                                           write_frontmatter_cache, monkeypatch, spawned):
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, relevance={"fine_model": "-x"})
    _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home, request={"v": 1})
    assert _snap(tmp_home)["llm"]["fail"] == "bad_model"
    assert spawned and spawned[0]["cfg"]["model"] == "haiku"


# 本轮粗排会展示的三篇；剩下的 `笔记/红烧肉.md`（tag「烹饪」）是唯一还能交给精排的候选
_SHOWN = ["笔记/alpha.md", "笔记/beta.md", "笔记/注入设计复盘.md"]


@pytest.mark.parametrize("injected,rel,n_spawn", [
    (_SHOWN, {}, 1),
    (_SHOWN + ["笔记/红烧肉.md"], {}, 0),
    (["笔记/注入设计复盘.md", "笔记/红烧肉.md"], {}, 0),     # 剩下的 alpha / beta 正是本轮粗排展示的两篇
    (_SHOWN, {"fine_exclude_tags": ["烹饪"]}, 0),
], ids=["one_left", "all_injected", "rest_shown_this_turn", "rest_excluded_by_tag"])
def test_spawn_requires_candidates_after_exclusion(injected, rel, n_spawn, tmp_home, tmp_vault,
                                                   tmp_git_repo, write_frontmatter_cache,
                                                   monkeypatch, spawned):
    """F62 / F67：本会话已注入、本轮展示与 `fine_exclude_tags` 一并排除之后还有候选才发起——子进程拿到的
    候选为空时只会白付一次调用。第一档是阳性对照：只剩一篇也照发。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, relevance=rel)
    _seed(injected=injected)
    _run(monkeypatch, tmp_git_repo, ASK)
    assert len(spawned) == n_spawn


def test_payload_failure_rolls_back_markers(tmp_home, tmp_vault, tmp_git_repo,
                                            write_frontmatter_cache, monkeypatch):
    """抢到在飞标记与机器槽位之后、子进程起来之前任何一步抛异常都要回滚：标记留着会挡住发起直到超时，
    过期后还会被下一轮记成一次 lost（fail_streak +1）。召回照常。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)

    def boom(*_a, **_k):
        raise RuntimeError("payload boom")

    monkeypatch.setattr(psl, "_fine_payload", boom)
    out, _ = _run(monkeypatch, tmp_git_repo, ASK)
    assert "「alpha」" in _ctx(out)
    assert _in_ns(lambda: _fine_state.inflight_active(SID, time.time())) is None
    slots = _in_ns(_fine_state.slots_dir)
    assert not slots.exists() or not any(slots.iterdir())
    assert _fine().calls == 0


def test_llm_snapshot_pending_while_spawned_child_in_flight(tmp_home, tmp_vault, tmp_git_repo,
                                                           write_frontmatter_cache, monkeypatch, spawned):
    """发起之后快照读本轮的在飞标记：面板显示「提炼中」，而不是「尚未发起」、过一会儿又跳成 lost。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home, request={"v": 1})
    assert len(spawned) == 1
    llm = _snap(tmp_home)["llm"]
    assert (llm["state"], llm["mode"]) == ("pending", "pin")


def test_llm_snapshot_reports_fine_state_when_disabled(tmp_home, tmp_vault, tmp_git_repo,
                                                      write_frontmatter_cache, monkeypatch, spawned):
    """被停用的会话：快照按 fine 状态如实报出停用原因与最近一次失败，而不是「尚未发起」。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    now = time.time()
    _seed(disabled_reason="no_claude", disabled_at=now, last_fail="no_claude", last_ts=now, calls=1)
    _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home, request={"v": 1})
    assert spawned == []
    llm = _snap(tmp_home)["llm"]
    assert (llm["state"], llm["fail"], llm["disabled"], llm["calls"]) == ("failed", "no_claude",
                                                                          "no_claude", 1)


def test_request_without_settings_key_keeps_stored_session_settings(tmp_home, tmp_vault, tmp_git_repo,
                                                                   write_frontmatter_cache, monkeypatch):
    """2.3.0 形态的 mod 请求（只有 exclude、没有 settings 键）不得清空已存的会话设置：请求缺这个键 = 沿用
    已存（`_fine_flow.begin_turn` 的契约）；只有显式的 `settings: {}` 才清空（见下一条）。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(session_settings={"coarse_inject": False})
    out, _ = _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home,
                  request={"v": 1, "exclude": ["笔记/红烧肉.md"]})
    assert "「alpha」" not in _ctx(out)
    assert _fine().session_settings == {"coarse_inject": False}


def test_request_with_empty_settings_clears_session_settings(tmp_home, tmp_vault, tmp_git_repo,
                                                             write_frontmatter_cache, monkeypatch):
    """对照：显式给出 `settings: {}` 是「用户把会话设置全部撤销」，清空已存的那份。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(session_settings={"coarse_inject": False})
    out, _ = _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home, request={"v": 1, "settings": {}})
    assert "「alpha」" in _ctx(out)
    assert _fine().session_settings == {}


def test_bad_request_keeps_stored_session_settings(tmp_home, tmp_vault, tmp_git_repo,
                                                   write_frontmatter_cache, monkeypatch):
    """整份作废的请求（bad_request）不当成「用户清空了会话设置」：沿用已存的那份。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home,
         request={"v": 1, "settings": {"coarse_inject": False}})
    out, _ = _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home, request={"v": 2})
    assert {"what": "request", "key": "", "reason": "bad_request"} in _snap(tmp_home)["rejected"]
    assert "「alpha」" not in _ctx(out)
    assert _fine().session_settings == {"coarse_inject": False}


# ── Task 17 修复轮 1 ──────────────────────────────────────────────────────────────

def test_gate_turn_llm_pending_while_in_flight(tmp_home, tmp_vault, tmp_git_repo,
                                               write_frontmatter_cache, monkeypatch, spawned):
    """I-1：闸门轮只有召回闸门之前那一次快照（早于 begin_turn）。2.4.0 起 topics 不写 pending 占位，「提炼中」
    只来自在飞标记，那一次也得读它。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _in_ns(lambda: _fine_state.claim_inflight(SID, "0123456789abcdef", 120, time.time()))
    _run(monkeypatch, tmp_git_repo, "继续", home=tmp_home, request={"v": 1})
    snap = _snap(tmp_home)
    assert snap["status"] == "gate:deictic_only"
    assert snap["llm"]["state"] == "pending"


def test_gate_turn_llm_reports_disabled_state(tmp_home, tmp_vault, tmp_git_repo,
                                              write_frontmatter_cache, monkeypatch, spawned):
    """I-1：被停用的会话在闸门轮同样报出停用原因、最近一次失败与调用次数（读本会话 fine 状态）。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    now = time.time()
    _seed(disabled_reason="no_claude", disabled_at=now, last_fail="no_claude", last_ts=now, calls=1)
    _run(monkeypatch, tmp_git_repo, "继续", home=tmp_home, request={"v": 1})
    snap = _snap(tmp_home)
    assert snap["status"] == "gate:deictic_only"
    llm = snap["llm"]
    assert (llm["state"], llm["fail"], llm["disabled"], llm["calls"]) == ("failed", "no_claude",
                                                                          "no_claude", 1)


@pytest.mark.parametrize("supp_on,modes", [(True, ["supp"]), (False, [])], ids=["supp_on", "supp_off"])
def test_ok_entry_without_words_counts_as_first_round_done(supp_on, modes, tmp_home, tmp_vault,
                                                           tmp_git_repo, write_frontmatter_cache,
                                                           monkeypatch, spawned):
    """I-2：子进程在「排除后无候选」时写 `[]/[]/state=ok` 占住首轮结论（模型只回 PINNED、不回 WORDS 时同样
    没有词）。「首轮点名已成功」与快照同一判据（`state == ok`）：不能因为没有词就当成还没成功、每轮付费
    重发首轮直到调用上限（F35）。"""
    from scripts import _topic
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, relevance={"fine_supplement": supp_on})
    _in_ns(lambda: _topic.save_session_topic(tmp_git_repo, SID, [], pinned=[], accounted=False,
                                             state="ok"))
    _run(monkeypatch, tmp_git_repo, ASK)
    assert [p["mode"] for p in spawned] == modes


def test_spawn_candidates_skip_system_index_and_unsafe_keys(tmp_home, tmp_vault, tmp_git_repo,
                                                            write_frontmatter_cache, monkeypatch,
                                                            spawned):
    """I-2 第二半：父进程数候选与子进程 `exclusion_set` 同口径——系统索引与不安全的 cache 键子进程不当候选，
    父进程也不能因为它们还在就发起一次注定无候选的调用。"""
    notes = {"笔记/INDEX.md": {"tags": [], "keywords": [], "summary": "索引页，摘要足够长足够长足够长"},
             "../外面.md": {"tags": [], "keywords": [], "summary": "不安全的 cache 键，摘要足够长足够长"}}
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, extra_notes=notes)
    _seed(injected=_SHOWN + ["笔记/红烧肉.md"])
    _run(monkeypatch, tmp_git_repo, ASK)
    assert spawned == []


def _persist_req() -> dict:
    # 收窄方向（默认 3 → 2），check_persist 放行；它不影响粗排清单，注入照常可见
    return {"v": 1, "settings": {"fine_max_notes": 2}, "persist": ["fine_max_notes"]}


@pytest.mark.parametrize("mode,reason", [("import_error", "unavailable"), ("not_ok", "write_failed"),
                                         ("raises", "write_failed")])
def test_persist_failure_reasons(mode, reason, tmp_home, tmp_vault, tmp_git_repo,
                                 write_frontmatter_cache, monkeypatch):
    """Minor 2（spec §11）：`unavailable` 只留给 config skill 加载不了（legacy 布局）；`set_key` 返回失败或
    抛异常都是 `write_failed`。三种情况下 UPS 都不抛、注入照常、会话设置照常生效。"""
    from types import SimpleNamespace
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)

    def set_key(key, raw_value, **_k):
        if mode == "raises":
            raise OSError("disk full")
        return {"ok": False, "error": "写后回读核验失败"}

    def load():
        if mode == "import_error":
            raise ImportError("config_set 不在")
        return SimpleNamespace(set_key=set_key)

    monkeypatch.setattr(psl, "_load_config_set", load)
    out, _ = _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home, request=_persist_req())
    assert "「alpha」" in _ctx(out)
    snap = _snap(tmp_home)
    assert {"what": "persist", "key": "fine_max_notes", "reason": reason} in snap["rejected"]
    assert snap["persisted"] == []
    assert _fine().session_settings == {"fine_max_notes": 2}


@pytest.mark.parametrize("bad", ["x", None, [], 3], ids=["str", "null", "list", "int"])
def test_non_object_settings_keep_stored_session_settings(bad, tmp_home, tmp_vault, tmp_git_repo,
                                                          write_frontmatter_cache, monkeypatch):
    """Minor 3：`settings` 不是对象（null / 字符串 / 列表）按「没带」处理、沿用已存；只有显式 `{}` 才清空。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _seed(session_settings={"coarse_inject": False})
    out, _ = _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home, request={"v": 1, "settings": bad})
    assert "「alpha」" not in _ctx(out)
    assert _fine().session_settings == {"coarse_inject": False}


def test_session_model_overrides_bad_config_model(tmp_home, tmp_vault, tmp_git_repo,
                                                  write_frontmatter_cache, monkeypatch, spawned):
    """Minor 4：config 里的模型名不合法、会话设置选了合法模型——本轮实际用的是会话模型，快照不报 bad_model。"""
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, relevance={"fine_model": "-x"})
    _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home,
         request={"v": 1, "settings": {"fine_model": "sonnet"}})
    assert spawned and spawned[0]["cfg"]["model"] == "sonnet"
    assert _snap(tmp_home)["llm"]["fail"] is None


# ── 本轮状态收口（2.4.0 full-review Q-H1）：`_Turn` 每轮一个，不变量由数据表达 ─────────────

def test_maybe_spawn_refuses_before_gate(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache,
                                         monkeypatch, spawned, capsys):
    """`_maybe_spawn` 在 `turn.past_gate` 为假时拒绝发起：精排只在过闸轮发起是 fine 统计队列上界的前提
    （`finish_turn` 的 docstring），这里把它从语句位置变成数据约束。同一个 turn 过闸之后再调一次，必须真的
    发起——证明前一次没发起是因为没过闸，而不是别的门禁挡下了。"""
    from scripts import _fine_flow
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    cfg, _fallback, _detail = psl.load_config_ex()
    entries, _status = psl.load_cache_status(tmp_vault)
    active = psl._active_entries(entries, cfg["relevance"])
    turn = psl._Turn()
    turn.fine = _in_ns(lambda: _fine_flow.begin_turn(SID, cfg, None))

    def _spawn():
        return _in_ns(lambda: psl._maybe_spawn(
            turn, cwd=tmp_git_repo, hook_input={"session_id": SID}, prompt=ASK, vault_path=tmp_vault,
            config=cfg, active_entries=active, excluded_paths=frozenset(), topics_ok=False,
            turn_injected=[], prev_words=[], extra_paths=[]))

    assert turn.past_gate is False
    assert _spawn() is None
    assert spawned == [], "没过闸的 turn 也发起了精排"
    assert "精排发起被拒" in capsys.readouterr().err
    turn.mark_scored()
    assert _spawn() == "spawned"
    assert len(spawned) == 1 and spawned[0]["mode"] == "pin"


def test_main_twice_in_one_process_does_not_leak_turn_state(tmp_home, tmp_vault, tmp_git_repo,
                                                            write_frontmatter_cache, monkeypatch):
    """同进程连续两次 `main()` 不串味：第一轮的「设为默认」说明、补充复核丢弃数、待对账的点名，都不得出现在
    第二轮的输出或记录里。生产每轮是新进程，这类串味只会在测试里出现；此前靠入口、出口各复位一次的全局变量
    挡着，现在靠每轮一个新 `_Turn`。先证明第一轮三样都产生了，再看第二轮。"""
    from types import SimpleNamespace

    from scripts import _topic
    _setup(tmp_home, tmp_vault, write_frontmatter_cache)
    _in_ns(lambda: _topic.save_session_topic(tmp_git_repo, SID, ["注入设计"], pinned=["笔记/beta.md"],
                                             accounted=False, state="ok"))
    _seed(turn_seq=1, supp=_supp(1, ["笔记/红烧肉.md"]))            # 本轮提问词复核会丢掉它
    monkeypatch.setattr(psl, "_load_config_set",
                        lambda: SimpleNamespace(set_key=lambda key, raw, **_k: {"ok": True}))
    accounted: list = []
    real_mark = psl.mark_pinned_accounted
    monkeypatch.setattr(psl, "mark_pinned_accounted",
                        lambda cwd, sid: (accounted.append(sid), real_mark(cwd, sid))[1])
    note = "已把 relevance.fine_max_notes 设为默认"

    out1, pid1 = _run(monkeypatch, tmp_git_repo, ASK, home=tmp_home,
                      request={"v": 1, "settings": {"fine_max_notes": 2}, "persist": ["fine_max_notes"]})
    rec1 = _full(tmp_home, pid1)
    assert note in out1.get("systemMessage", "")                    # 第一轮：persist 说明
    assert rec1["n_supp_drop_recheck"] == 1                         # 第一轮：复核丢弃 1 篇
    assert rec1.get("llm_pinned") == ["笔记/beta.md"] and accounted == [SID]   # 第一轮：对账登记并翻转

    out2, pid2 = _run(monkeypatch, tmp_git_repo, ASK)
    rec2 = _full(tmp_home, pid2)                                    # 第二轮确是过闸的打分轮（有完整决策记录）
    assert out2.get("systemMessage")                                # 第二轮也有 systemMessage，下一条不是空网
    assert note not in out2.get("systemMessage", "")
    assert rec2["n_supp_drop_recheck"] == 0
    assert "llm_pinned" not in rec2 and accounted == [SID], "第二轮重复执行了第一轮的对账登记"
