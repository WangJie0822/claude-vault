# -*- coding: utf-8 -*-
"""精排子进程主体（spec 2026-10-13 §6.2–§6.4 / §7）：首轮 / 补充两种模式的写回、补充队列条件写（F06）、
失败只写 fine（F01）、失败计数与停用（F03/F07）、统计回传、在飞标记与槽位释放、模型名回落（§5.2）、
payload 形状（§6.3）、fine 状态不落 prompt 与 K 轮文本（F16）。"""
from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from scripts import _fine_state, _topic
from scripts._config_loader import DEFAULT_CONFIG
from scripts._state import session_topics_path
from scripts._topic import (_KEEP, build_fine_payload, load_pinned_accounted, load_session_pinned,
                            load_session_topic, run_extraction_child, save_session_topic)

GEN = "0123456789abcdef"
# 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id
SID = "-".join(["11111111", "2222", "3333", "4444", "555555555555"])
ENTRIES = {
    "笔记/召回闸门.md": {"tags": ["召回"], "summary": "召回闸门与打分的设计记录", "mtime": 300, "keywords": ["召回"]},
    "笔记/打分权重.md": {"tags": ["打分"], "summary": "打分权重的取值与理由", "mtime": 200, "keywords": ["打分"]},
    "笔记/无关.md": {"tags": ["其它"], "summary": "与本次话题无关的笔记", "mtime": 100, "keywords": []},
}
STATS_KEYS = {"gen_id", "mode", "ok", "fail", "none", "n_sel", "n_echo_mismatch", "n_catalog",
              "n_catalog_dropped", "n_summary_layer", "bytes", "degraded"}


@pytest.fixture
def env(tmp_home, tmp_vault, write_frontmatter_cache, tmp_path):
    write_frontmatter_cache(ENTRIES)
    cwd = tmp_path / "proj"
    (cwd / ".git").mkdir(parents=True)
    (cwd / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    cfg = deepcopy(DEFAULT_CONFIG)
    cfg["vault_path"] = str(tmp_vault)
    return SimpleNamespace(cwd=cwd, cfg=cfg, vault=tmp_vault)


def _payload(env, mode="pin", src_seq=1, gen=GEN, slot=None, **over):
    p = build_fine_payload(mode, prompt="召回 打分 怎么定", config=env.cfg, gen_id=gen, src_seq=src_seq,
                           slot=slot, transcript_path="", injected=[], excluded=[], prev_words=[],
                           extra_paths=[])
    p.update(over)
    return p


def _num(text, name):
    m = re.search(r"(\d+) - «" + re.escape(name) + "»", text)
    assert m, f"候选里没有 {name}"
    return int(m.group(1))


def _pick(label, *names, words="召回, 闸门"):
    return lambda t: f"{label}: " + ", ".join(f"{_num(t, n)} «{n}»" for n in names) + f"\nWORDS: {words}"


def _run(monkeypatch, env, payload, reply, fail=None):
    seen = {"calls": 0}

    def fake(text):
        seen["calls"] += 1
        seen["prompt"], seen["model"] = text, _topic._MODEL
        if fail:
            _topic._LAST_FAIL = fail
            return None
        return reply(text) if callable(reply) else reply

    monkeypatch.setattr(_topic, "_call_model", fake)
    assert run_extraction_child([str(env.cwd), SID], stdin_text=json.dumps(payload, ensure_ascii=False)) == 0
    return seen


def _entry(env):
    return json.loads(session_topics_path(env.cwd).read_text(encoding="utf-8"))["topics"][SID]


# ---------------- 首轮模式 ----------------

def test_pin_mode_saves_pinned_words_and_ok(monkeypatch, env):
    _run(monkeypatch, env, _payload(env), _pick("PINNED", "召回闸门"))
    assert load_session_pinned(env.cwd, SID, 24) == ["笔记/召回闸门.md"]
    assert load_session_topic(env.cwd, SID, 24) == ["召回", "闸门"]
    assert load_pinned_accounted(env.cwd, SID, 24) is False
    assert _entry(env)["state"] == "ok"
    st = _fine_state.load_fine(SID)
    assert st.fail_streak == 0 and st.stats_q[-1]["ok"] is True
    assert set(st.stats_q[-1]) == STATS_KEYS


def test_pin_mode_respects_pin_max(monkeypatch, env):
    env.cfg["relevance"]["fine_pin_max"] = 2
    seen = _run(monkeypatch, env, _payload(env), _pick("PINNED", "召回闸门", "打分权重", "无关"))
    assert "最多 2 篇" in seen["prompt"]
    assert load_session_pinned(env.cwd, SID, 24, pin_max=8) == ["笔记/召回闸门.md", "笔记/打分权重.md"]


def test_pin_mode_without_pinning_uses_words_only_template(monkeypatch, env):
    env.cfg["relevance"]["session_pin_enabled"] = False
    seen = _run(monkeypatch, env, _payload(env), "WORDS: 召回, 打分")
    assert "PINNED" not in seen["prompt"]
    assert load_session_pinned(env.cwd, SID, 24) == []
    assert load_session_topic(env.cwd, SID, 24) == ["召回", "打分"]


def test_pin_failure_touches_only_fine(monkeypatch, env):
    _run(monkeypatch, env, _payload(env), None, fail="timeout")
    assert not session_topics_path(env.cwd).exists()                    # F01：失败不碰 topics
    st = _fine_state.load_fine(SID)
    assert (st.last_fail, st.fail_streak, st.disabled_reason) == ("timeout", 1, None)
    assert st.stats_q[-1]["ok"] is False and st.stats_q[-1]["fail"] == "timeout"


def test_two_failures_disable_streak(monkeypatch, env):
    for _ in range(2):
        _run(monkeypatch, env, _payload(env), None, fail="exit_nonzero")
    st = _fine_state.load_fine(SID)
    assert (st.fail_streak, st.disabled_reason) == (2, "streak")
    assert isinstance(st.disabled_at, float)


def test_no_claude_disables(monkeypatch, env):
    _run(monkeypatch, env, _payload(env), None, fail="no_claude")
    st = _fine_state.load_fine(SID)
    assert (st.disabled_reason, st.last_fail) == ("no_claude", "no_claude")


def test_success_resets_streak(monkeypatch, env):
    _run(monkeypatch, env, _payload(env), None, fail="timeout")
    _run(monkeypatch, env, _payload(env), _pick("PINNED", "召回闸门"))
    st = _fine_state.load_fine(SID)
    assert (st.fail_streak, st.last_fail) == (0, None)


def test_child_survives_model_exception(monkeypatch, env):
    def boom(_t):
        raise RuntimeError("model error")
    monkeypatch.setattr(_topic, "_call_model", boom)
    assert run_extraction_child([str(env.cwd), SID], stdin_text=json.dumps(_payload(env))) == 0
    assert _fine_state.load_fine(SID).last_fail == "error"


# ---------------- 补充模式 ----------------

def _seed_pinned(env):
    save_session_topic(env.cwd, SID, ["旧词"], pinned=["笔记/无关.md"], accounted=False, state="ok")


def test_supp_mode_writes_supp_and_keeps_pins(monkeypatch, env):
    _seed_pinned(env)
    p = _payload(env, mode="supp", src_seq=4, injected=["笔记/无关.md"])
    _run(monkeypatch, env, p, _pick("SUPPLEMENT", "打分权重", words="打分, 权重"))
    st = _fine_state.load_fine(SID)
    assert (st.supp["gen_id"], st.supp["src_seq"], st.supp["paths"]) == (GEN, 4, ["笔记/打分权重.md"])
    assert load_session_topic(env.cwd, SID, 24) == ["打分", "权重"]
    assert load_session_pinned(env.cwd, SID, 24) == ["笔记/无关.md"]
    assert _entry(env)["state"] == "ok"


def test_supp_empty_words_keep_previous(monkeypatch, env):
    _seed_pinned(env)
    _run(monkeypatch, env, _payload(env, mode="supp"), "SUPPLEMENT: NONE")
    assert load_session_topic(env.cwd, SID, 24) == ["旧词"]
    st = _fine_state.load_fine(SID)
    assert st.supp is None and st.stats_q[-1]["none"] is True       # NONE 不写空补充（批次 3c）


@pytest.mark.parametrize("reply,none", [("SUPPLEMENT: NONE\nWORDS: 打分, 权重", True),
                                        (lambda t: f"SUPPLEMENT: {_num(t, '召回闸门')} «打分权重»\nWORDS: 打分, 权重",
                                         False)],
                         ids=["none", "zero_selected"])
def test_supp_none_or_zero_selected_keeps_existing_supp(monkeypatch, env, reply, none):
    """批次 3c（评审 R6 / R8 / F3）：补充模式判 NONE、或回显全对不上而选中 0 篇时不写 `supp`——盘上另一份
    （别的 gen、较早的 src_seq）原样保留，不被空补充顶掉；本次调用的统计照常追加，主题词照常刷新。"""
    _seed_pinned(env)
    old = {"gen_id": "fedcba9876543210", "src_seq": 3, "paths": ["笔记/无关.md"], "ts": 1.0}

    def seed(st):
        st.supp = dict(old)
        return st

    assert _fine_state.update_fine(SID, seed) is not None
    seen = _run(monkeypatch, env, _payload(env, mode="supp", src_seq=4), reply)
    assert seen["calls"] == 1
    st = _fine_state.load_fine(SID)
    assert st.supp == old
    assert len(st.stats_q) == 1
    assert (st.stats_q[-1]["ok"], st.stats_q[-1]["none"], st.stats_q[-1]["n_sel"]) == (True, none, 0)
    assert st.stats_q[-1]["bytes"] > 0
    assert load_session_topic(env.cwd, SID, 24) == ["打分", "权重"]


def test_supp_write_is_conditional_on_src_seq(monkeypatch, env):
    _seed_pinned(env)

    def seed(st):
        st.supp = {"gen_id": "fedcba9876543210", "src_seq": 9, "paths": ["笔记/无关.md"], "ts": 1.0}
        return st

    assert _fine_state.update_fine(SID, seed) is not None
    _run(monkeypatch, env, _payload(env, mode="supp", src_seq=7), _pick("SUPPLEMENT", "打分权重"))
    assert _fine_state.load_fine(SID).supp["gen_id"] == "fedcba9876543210"    # 旧轮的结果不得覆盖新轮（F06）
    _run(monkeypatch, env, _payload(env, mode="supp", src_seq=9), _pick("SUPPLEMENT", "打分权重"))
    assert _fine_state.load_fine(SID).supp["gen_id"] == GEN                   # 同一轮（≥）可以覆盖


def test_echo_mismatch_is_counted_and_dropped(monkeypatch, env):
    _run(monkeypatch, env, _payload(env), lambda t: f"PINNED: {_num(t, '召回闸门')} «打分权重»\nWORDS: 召回")
    assert load_session_pinned(env.cwd, SID, 24) == []
    assert _fine_state.load_fine(SID).stats_q[-1]["n_echo_mismatch"] == 1


# ---------------- 标记、槽位、回落、payload ----------------

def test_child_releases_inflight_and_slot(monkeypatch, env):
    now = time.time()
    assert _fine_state.claim_inflight(SID, GEN, 120, now) is True
    slot = _fine_state.claim_slot(GEN, 120, now)
    assert slot is not None
    _run(monkeypatch, env, _payload(env, slot=slot), _pick("PINNED", "召回闸门"))
    assert _fine_state.inflight_active(SID, time.time()) is None
    assert not (_fine_state.slots_dir() / slot).exists()


def test_child_releases_inflight_even_when_main_raises(monkeypatch, env):
    now = time.time()
    assert _fine_state.claim_inflight(SID, GEN, 120, now) is True
    slot = _fine_state.claim_slot(GEN, 120, now)
    other = _fine_state.claim_slot("fedcba9876543210", 120, now)     # 别的子进程占着的槽位
    assert slot is not None and other is not None and slot != other

    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(_topic, "_child_main", boom)
    p = _payload(env, slot=slot)
    assert run_extraction_child([str(env.cwd), SID], stdin_text=json.dumps(p)) == 0
    assert _fine_state.inflight_active(SID, time.time()) is None
    assert not (_fine_state.slots_dir() / slot).exists()
    assert (_fine_state.slots_dir() / other).exists()                # 只删 payload 指定的那个，不清整个目录（gen_id 核对见 test_fine_state 的 release_slot 用例）
    assert _fine_state.load_fine(SID).last_fail == "error"


def test_bad_model_falls_back_to_haiku(monkeypatch, env):
    p = _payload(env)
    p["cfg"]["model"] = "-x"
    seen = _run(monkeypatch, env, p, _pick("PINNED", "召回闸门"))
    assert seen["model"] == "haiku"
    assert "bad_model" in _fine_state.load_fine(SID).stats_q[-1]["degraded"]


@pytest.mark.parametrize("field,value", [
    ("v", 2), ("mode", "other"), ("gen_id", "../x"), ("src_seq", -1), ("vault_path", ""),
])
def test_invalid_payload_records_error_without_call(monkeypatch, env, field, value):
    seen = _run(monkeypatch, env, _payload(env, **{field: value}), _pick("PINNED", "召回闸门"))
    assert seen["calls"] == 0
    st = _fine_state.load_fine(SID)
    assert st.last_fail == "error" and "bad_payload" in st.stats_q[-1]["degraded"]


def test_cache_empty_records_error_without_call(monkeypatch, env):
    (env.vault / ".meta" / "frontmatter-cache.json").unlink()
    seen = _run(monkeypatch, env, _payload(env), "PINNED: NONE")
    assert seen["calls"] == 0
    st = _fine_state.load_fine(SID)
    assert st.last_fail == "error" and "cache_empty" in st.stats_q[-1]["degraded"]


def test_bytes_and_stats_queue(monkeypatch, env):
    seen = _run(monkeypatch, env, _payload(env), _pick("PINNED", "召回闸门"))
    st = _fine_state.load_fine(SID)
    # 子进程只回报字节，不自己累加（父进程 finish_turn 并账，见 Task 15）
    assert st.stats_q[-1]["bytes"] == len(seen["prompt"].encode("utf-8")) and st.bytes_sent == 0
    for _ in range(_fine_state.STATS_Q_MAX + 2):
        _run(monkeypatch, env, _payload(env), _pick("PINNED", "召回闸门"))
    assert len(_fine_state.load_fine(SID).stats_q) == _fine_state.STATS_Q_MAX


@pytest.mark.parametrize("fail", [None, "timeout"])
def test_each_child_run_appends_exactly_one_stats_entry(monkeypatch, env, fail):
    """成功与失败路径各自只追加 1 条：多追加会让父进程并账时把同一次调用的字节与次数算两遍。"""
    assert _fine_state.load_fine(SID).stats_q == []
    for n in (1, 2):
        _run(monkeypatch, env, _payload(env), _pick("PINNED", "召回闸门"), fail=fail)
        q = _fine_state.load_fine(SID).stats_q
        assert len(q) == n and q[-1]["ok"] is (fail is None)


def test_k_turns_injected_and_excluded_reach_the_prompt_but_not_fine_state(monkeypatch, env, tmp_path):
    cfgdir = tmp_path / "cc"
    tx = cfgdir / "projects" / "p" / f"{SID}.jsonl"
    tx.parent.mkdir(parents=True)
    tx.write_text(json.dumps({"type": "user", "promptSource": "typed",
                              "message": {"content": "更早的那个问题"}}, ensure_ascii=False) + "\n",
                  encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfgdir))
    p = _payload(env, transcript_path=str(tx), injected=["笔记/无关.md"], excluded=["笔记/打分权重.md"])
    seen = _run(monkeypatch, env, p, "PINNED: NONE\nWORDS: 召回")
    assert "- «更早的那个问题»" in seen["prompt"]
    assert "\n- «无关»" in seen["prompt"]                                  # 「已在上下文」一节
    assert " - «无关»" not in seen["prompt"]                               # 已注入的不进候选
    assert "打分权重" not in seen["prompt"]                                # 本会话排除的不进候选
    raw = _fine_state.fine_path(SID).read_text(encoding="utf-8")
    assert "更早的那个问题" not in raw and "召回 打分 怎么定" not in raw     # F16


def test_save_keep_state_preserves_ok_and_fail(env):
    save_session_topic(env.cwd, SID, ["a词"], pinned=["笔记/无关.md"], accounted=True, state="ok")
    save_session_topic(env.cwd, SID, ["b词"], state=_KEEP)
    e = _entry(env)
    assert (e["state"], e["pinned"], e["accounted"], e["words"]) == ("ok", ["笔记/无关.md"], True, ["b词"])
    save_session_topic(env.cwd, SID, [], state="failed", fail="timeout")
    save_session_topic(env.cwd, SID, ["c词"], state=_KEEP)
    e = _entry(env)
    assert (e["state"], e["fail"]) == ("failed", "timeout")


def test_build_fine_payload_shape(env):
    p = build_fine_payload("supp", prompt="q", config=env.cfg, gen_id=GEN, src_seq=3, slot="slot-1",
                           transcript_path="t", injected=["a.md"] * 400, excluded=[],
                           prev_words=[f"w{i}" for i in range(20)], extra_paths=["x"])
    assert set(p) == {"v", "mode", "gen_id", "src_seq", "slot", "prompt", "transcript_path", "vault_path",
                      "cfg", "injected", "excluded", "prev_words", "extra_paths"}
    assert set(p["cfg"]) == {"model", "timeout", "pin_max", "supp_max", "recent_prompts", "catalog",
                             "catalog_max_chars", "fine_exclude_tags", "exclude_note_tags", "top_n",
                             "project_top_k", "pool_max", "recent_worklog_days", "max_recent_worklogs",
                             "use_keywords", "use_tag_idf", "tag_idf_floor", "weights", "jcfg"}
    assert (p["cfg"]["model"], p["cfg"]["supp_max"], p["cfg"]["timeout"]) == ("haiku", 3, 120)
    assert len(p["injected"]) == _fine_state.MAX_INJECTED
    assert len(p["prev_words"]) == _topic.MAX_TOPIC_WORDS
    assert p["vault_path"] == str(env.vault) and p["slot"] == "slot-1"
    json.dumps(p)


def test_build_fine_payload_survives_huge_and_nonfinite_numbers(env):
    """config 是用户可写的 JSON：超大整数（转不成 float）与非有限数回落默认，不抛（否则 spawn 整轮失败）。"""
    env.cfg["relevance"]["tag_idf_floor"] = 10 ** 400
    env.cfg["scoring"]["prompt_tag_hit"] = float("inf")
    p = build_fine_payload("pin", prompt="q", config=env.cfg, gen_id=GEN, src_seq=1, slot=None,
                           transcript_path="", injected=[], excluded=[], prev_words=[], extra_paths=[])
    assert p["cfg"]["tag_idf_floor"] == 0.5 and p["cfg"]["weights"]["prompt_tag_hit"] == 4
    json.dumps(p, allow_nan=False)


# ---------------- 排除 tag 的外发接缝（2.4.0 full-review T-H1） ----------------
# 链路：`build_fine_payload` 写 cfg 的两组排除 tag → stdin → `_child_main` → `exclusion_set`（候选）与
# `build_fine_input`（「已在上下文」）→ 外发 prompt。`tests/test_private_tag_outbound_e2e.py` 的读端脚本自己拼 cfg
# 直接调后两者，不经过这一段；下面几条用例从真实 payload 与真实子进程入口跑起。

# 带排除 tag 的各种写法（cache 里的原始 tags 值）：列表、带 `#` 且大小写不同、嵌套、标量字符串、映射，
# 另有 `Archived`（只有 `exclude_note_tags` 那一组认它，`fine_exclude_tags` 那一组不认）。
_TAGGED_TAGS = {
    "flow": ["private"], "hash": ["#Private"], "nested": ["private/journal"],
    "scalar": "private", "dict": {"private": True}, "archived": ["Archived"],
}


def _tagged_entries():
    """每种写法两篇：摘要含「召回」的进摘要层（附摘要），不含任何话题词的进目录层（只有文件名）。
    文件名一律带 PRIV_，摘要一律带 SECRET-，prompt 里出现任一子串即外发。"""
    out = {}
    for kind, tags in _TAGGED_TAGS.items():
        out[f"笔记/PRIV_{kind}.md"] = {"tags": tags, "summary": f"召回 SECRET-{kind}", "mtime": 400,
                                       "keywords": []}
        out[f"笔记/PRIV_cat_{kind}.md"] = {"tags": tags, "summary": f"SECRET-cat-{kind}", "mtime": 400,
                                           "keywords": []}
    return out


def _stems(rels):
    return [PurePosixPath(r).stem for r in rels]


@pytest.mark.parametrize("mode", ["pin", "supp"])
@pytest.mark.parametrize("placement", ["candidate", "in_context"])
def test_child_prompt_excludes_tagged_notes_from_real_payload(monkeypatch, env, write_frontmatter_cache,
                                                              mode, placement):
    """排除 tag 经真实 payload 与子进程入口传到排除判定：任何写法的带 tag 笔记，文件名与摘要都不进外发 prompt。
    candidate：笔记是候选（摘要层与目录层各一篇）；in_context：笔记作为已注入路径交给子进程。"""
    tagged = _tagged_entries()
    write_frontmatter_cache({**ENTRIES, **tagged})
    injected = sorted(tagged) + ["笔记/无关.md"] if placement == "in_context" else []
    p = _payload(env, mode=mode, injected=injected)
    seen = _run(monkeypatch, env, p, "PINNED: NONE\nSUPPLEMENT: NONE\nWORDS: 召回")
    prompt = seen["prompt"]
    leaked = sorted(rel for rel, stem in zip(tagged, _stems(tagged)) if stem in prompt)
    assert leaked == [], f"带排除 tag 的笔记进了外发 prompt（{mode}/{placement}）：{leaked}"
    assert "SECRET-" not in prompt and "PRIV_" not in prompt
    # 对照：不带排除 tag 的笔记照常外发，否则「什么都没发」也会让上面的断言全绿
    assert "召回闸门与打分的设计记录" in prompt and _num(prompt, "召回闸门") >= 1       # 摘要层：文件名 + 摘要
    if placement == "candidate":
        assert _num(prompt, "无关") >= 1                                           # 目录层：只有文件名
    else:
        assert "\n- «无关»" in prompt                                              # 「已在上下文」一节


@pytest.mark.parametrize("mode", ["pin", "supp"])
@pytest.mark.parametrize("placement", ["candidate", "in_context"])
def test_child_prompt_sends_tagged_notes_once_both_exclusion_tag_lists_are_cleared(
        monkeypatch, env, write_frontmatter_cache, mode, placement):
    """上一条的阳性对照：同一批笔记、同一条通路，payload 里两组排除 tag 都清空时，文件名（摘要层还有摘要）
    全部进了 prompt。证明上一条里的「没外发」不是因为这批笔记压根进不了 prompt（路径、数量、层级的问题）。"""
    tagged = _tagged_entries()
    write_frontmatter_cache({**ENTRIES, **tagged})
    injected = sorted(tagged) if placement == "in_context" else []
    p = _payload(env, mode=mode, injected=injected)
    p["cfg"]["fine_exclude_tags"] = []
    p["cfg"]["exclude_note_tags"] = []
    prompt = _run(monkeypatch, env, p, "PINNED: NONE\nSUPPLEMENT: NONE\nWORDS: 召回")["prompt"]
    missing = sorted(rel for rel, stem in zip(tagged, _stems(tagged)) if stem not in prompt)
    assert missing == [], f"排除 tag 清空后这些笔记仍不在 prompt 里（{mode}/{placement}）：{missing}"
    if placement == "candidate":
        no_summary = [k for k in _TAGGED_TAGS if f"SECRET-{k}" not in prompt]
        assert no_summary == [], f"摘要层的笔记缺摘要：{no_summary}"


def test_build_fine_payload_carries_configured_exclude_tag_values(env):
    """payload 的 cfg 里两组排除 tag 的**值**等于 config 里设的值（只核对键集合挡不住 `[]`）。"""
    env.cfg["relevance"]["fine_exclude_tags"] = ["秘密"]
    env.cfg["relevance"]["exclude_note_tags"] = ["归档", "草稿"]
    p = _payload(env)
    assert p["cfg"]["fine_exclude_tags"] == ["秘密"]
    assert p["cfg"]["exclude_note_tags"] == ["归档", "草稿"]


def test_build_fine_payload_default_exclude_tag_values(env):
    """默认配置下 payload 的两组排除 tag 是 `["private"]` 与 `["archived"]`（字面量，不从常量反算）。"""
    p = _payload(env)
    assert p["cfg"]["fine_exclude_tags"] == ["private"]
    assert p["cfg"]["exclude_note_tags"] == ["archived"]
