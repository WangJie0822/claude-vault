# -*- coding: utf-8 -*-
"""`relevance.skip_slash_commands`：点名的 slash 命令整轮不注入。

判据分三层，缺任何一层都会留下一个「全绿但没接上」的洞：
1. `extract_slash_command_name` —— 解析（两种形态）；
2. `_is_skipped_slash_command` —— 匹配语义；
3. **接线** —— `main()` 里那道闸门真的被调了。第 3 层是重点：只测前两层时，
   把闸门整段从 `main()` 删掉，前两层用例照样全绿（CLAUDE.md「守卫钉住定义、
   钉不住接线」）。故端到端用例成对出现，且**带阳性对照** —— 同一个 prompt 在
   空名单下必须注入，否则「没注入」可能来自别的闸门，本用例对本功能零判别力。

实测背景（2026-09-16，本机 metrics 加盐 hash × transcript 对拍 335 个命令轮）：
UPS 对 skill 型命令收到 `/cmd args` 原文（形态 A 命中 47、`<command-name>` 包裹
形态命中 0）；`/bug-batch` 12/12 原文形态，gate 全空、确实注入 —— 即本功能的
目标场景成立。另有一类命令（`/context-vault:summarize-session` 等，涉 83 个
session）UPS 有记录、确实注入，但 prompt 对不上任何命令形态，本机制拦不住，
属已知边界（见 SKILL.md）。
"""
from __future__ import annotations

import io
import itertools
import json
import sys
from pathlib import Path

import pytest

from scripts import prompt_submit_load as psl
from scripts._config_loader import DEFAULT_CONFIG, _normalize_relevance
from scripts._signal_collect import _strip_slash_command, extract_slash_command_name
from scripts._decision import is_deictic_only, is_skipped_slash_command
from scripts.analyze_metrics import _render_slash_skip


@pytest.fixture(autouse=True)
def _no_topic_spawn(monkeypatch):
    """端到端用例绝不真起提炼子进程 —— 那是 detached 的 `claude -p --model haiku`，
    **消耗真实额度**且耗时不可控（首轮实测约 21s）。与 `test_pin_wiring.py` 同一做法。

    首版漏了这条：本文件的端到端用例一度真的 spawn，识别信号是「67 条用例耗时
    1.5s vs 49 条耗时 61s」这种与预期严重不符的波动。autouse 覆盖全文件，纯单元
    用例不受影响（它们根本不走 `main()`）。
    """
    monkeypatch.setattr(psl, "spawn_fine", lambda *a, **k: "spawn_error")


def _is_skipped(prompt, names):
    """便捷包装：判据真实签名是 `(prompt, config)`，与同族 `is_deictic_only` 对齐。"""
    return is_skipped_slash_command(prompt, {"relevance": {"skip_slash_commands": names}})


# ── 第 1 层：解析 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("prompt,expected", [
    ("/bug-batch 21292", "bug-batch"),
    ("/bug-batch", "bug-batch"),                      # 纯命令无参数
    ("   /bug-batch xx", "bug-batch"),                # 前导空白
    ("/context-vault:vault 关键词", "context-vault:vault"),
    ("/superpowers:brainstorming 做个功能", "superpowers:brainstorming"),
    ("/BUG-BATCH x", "BUG-BATCH"),                    # 原样保留大小写，归一在匹配层
    ("普通提问，不是命令", ""),
    ("", ""),
    ("这里有个 /bug-batch 但不在开头", ""),
])
def test_extract_form_a_raw_text(prompt: str, expected: str) -> None:
    assert extract_slash_command_name(prompt) == expected


@pytest.mark.parametrize("prompt,expected", [
    ("<command-name>/bug-batch</command-name>", "bug-batch"),
    ("<command-name>/superpowers:brainstorming</command-name>", "superpowers:brainstorming"),
    # 无前导斜杠也认：保留形态 B 的理由是「harness 改形态时不静默失效」，而它改形态时
    # 未必带斜杠 —— 只认带斜杠的写法会让那条理由自己落空。
    ("<command-name>bug-batch</command-name>", "bug-batch"),
])
def test_extract_form_b_wrapped(prompt: str, expected: str) -> None:
    """形态 B 当前 harness 实测零命中，保留是为了让 harness 改形态时不静默失效。"""
    assert extract_slash_command_name(prompt) == expected


@pytest.mark.parametrize("prompt", [
    "xx <command-name>/bug-batch</command-name>",                 # 前面有正文
    "讨论一下 <command-name>/bug-batch</command-name> 这个标签",     # 中间出现
    "x" * 3000 + "<command-name>/bug-batch</command-name>",       # 远在后方
    "<command-message>x</command-message>\n<command-name>/bug-batch</command-name>",
])
def test_form_b_matches_only_at_start(prompt: str) -> None:
    """形态 B **锚定开头**（match 而非 search）。

    契约是「prompt 以命令开头」，四处文档都这么写。用 search 时讨论中粘一段含
    `<command-name>` 的文本就会被误拦，是一条未文档化的误拦通路。
    最后一条形态（command-message 在前）当前 harness 不产生，一并按「不在开头」处理，
    真出现时宁可漏拦不误拦。
    """
    assert extract_slash_command_name(prompt) == ""


@pytest.mark.parametrize("prompt", [
    "/bug-batch 21292", "/x:y args", "/solo", "  /lead space", "//double a",
    "/a\tb", "/trail   ",
])
def test_parse_parity_with_strip_slash_command(prompt: str) -> None:
    """两处 slash 解析共用「到第一个空白为止」的切分口径，必须同步改。

    契约：剥掉 `_strip_slash_command` 返回的余下正文后，剩的那截前缀去掉前导 `/`
    就等于 `extract_*` 给出的命令名 —— 任一侧改了切分点，这个等式立刻不成立。

    ⚠️ 此前写成 `bool(name) == (_strip_slash_command(p) != p)`，**该等式在形态 B 下
    本就不成立**（包裹形态不以 `/` 开头 ⇒ `_strip_slash_command` 原样返回，而
    `extract_*` 认得出命令名），只是当时选的 5 条参数恰好全是形态 A 或非命令、
    避开了全部反例，于是这条守卫对真实漂移零判别力。边界由下一条用例显式钉住。
    """
    s = prompt.lstrip()
    rest = _strip_slash_command(prompt)
    head = s[: len(s) - len(rest)] if rest else s
    assert head.lstrip("/") == extract_slash_command_name(prompt), f"切分口径漂移：{prompt!r}"


def test_parity_contract_does_not_apply_to_form_b() -> None:
    """显式钉住上一条契约的适用边界，免得有人把它推广到形态 B 后往错方向「修」。"""
    p = "<command-name>/bug-batch</command-name>"
    assert extract_slash_command_name(p) == "bug-batch"
    assert _strip_slash_command(p) == p, "不以 / 开头 ⇒ 原样返回，这正是旧契约失效的原因"


# ── 第 2 层：匹配语义 ──────────────────────────────────────────────────
def test_bare_name_matches_root_level() -> None:
    assert _is_skipped("/bug-batch 21292", ["bug-batch"]) is True


def test_bare_name_also_matches_any_plugin_prefix() -> None:
    """刻意的宽松语义：用户不该被迫知道某 skill 是根级还是 plugin 提供的。"""
    assert _is_skipped("/context-vault:vault kw", ["vault"]) is True


def test_qualified_name_is_exact_and_does_not_match_bare_form() -> None:
    """名单写全限定名 ⇒ 精确匹配，这是用户「只想拦这一个」的表达方式。"""
    assert _is_skipped("/context-vault:vault kw", ["context-vault:vault"]) is True
    assert _is_skipped("/vault kw", ["context-vault:vault"]) is False


@pytest.mark.parametrize("entry", ["bug-batch", "BUG-BATCH", "/bug-batch", "  bug-batch  "])
def test_entry_normalization_case_and_slash_and_space(entry: str) -> None:
    assert _is_skipped("/Bug-Batch 21292", [entry]) is True


def test_non_listed_command_is_not_skipped() -> None:
    assert _is_skipped("/commit 改动说明", ["bug-batch"]) is False


def test_non_slash_prompt_never_skipped() -> None:
    assert _is_skipped("帮我分析一下 bug-batch 的实现", ["bug-batch"]) is False


def test_pasted_path_is_treated_as_command_by_design() -> None:
    """已知行为，SKILL.md 明写：以 `/` 开头且首 token 命中名单的**任何** prompt 都算该命令。

    判据无法区分「真 slash 调用」与「粘贴的路径/代码」。失败方向温和（这一轮不注入，
    复述即可），故不修。有这条守卫才能挡住日后「顺手收紧」而让文档失准。
    """
    assert _is_skipped("/usr/bin/foo 有 bug", ["usr/bin/foo"]) is True
    assert _is_skipped("/usr/bin/foo 有 bug", ["bug-batch"]) is False


@pytest.mark.parametrize("bad", [123, None, b"/bug-batch", object(), ["/bug-batch"]])
def test_fail_open_on_malformed_prompt(bad) -> None:
    """hook 硬不变量：任何异常或畸形输入一律放行（False），绝不因本闸门丢召回。

    `is_deictic_only` 同参数做阳性对照 —— 它是本仓库认可的写法，两者行为必须一致，
    否则下一个人会以为「这层不需要防」。
    """
    cfg = {"relevance": {"skip_slash_commands": ["bug-batch"]}}
    assert is_skipped_slash_command(bad, cfg) is False
    assert is_deictic_only(bad, {}) is False


@pytest.mark.parametrize("cfg", [
    None, {}, {"relevance": None}, {"relevance": {}},
    {"relevance": {"skip_slash_commands": "bug-batch"}},   # 未经归一化的字符串
    {"relevance": {"skip_slash_commands": {"a": 1}}},
])
def test_fail_open_on_malformed_config(cfg) -> None:
    """config 侧同样 fail-open。注意名单为字符串时**不得**逐字符匹配成 /b /u /g。"""
    assert is_skipped_slash_command("/bug-batch x", cfg) is False


def test_empty_list_short_circuits() -> None:
    """100% 存量用户走这条路径，必须零解析开销。"""
    assert _is_skipped("/bug-batch x", []) is False


def test_wrapped_form_also_matches_list() -> None:
    assert _is_skipped("<command-name>/bug-batch</command-name>", ["bug-batch"]) is True


# ── 配置归一化（判据单点在 _normalize_relevance，调用点不再各写一份）──────
def test_default_config_has_empty_skip_list() -> None:
    """默认空 = 存量用户零行为变化。改这个默认值等于给所有人上线一条新路径。"""
    assert DEFAULT_CONFIG["relevance"]["skip_slash_commands"] == []


@pytest.mark.parametrize("bad", ["bug-batch", {"a": 1}, 42, None])
def test_non_list_config_is_normalized_to_empty(bad) -> None:
    """字符串最危险：`for n in "bug-batch"` 会造出 /b /u /g … 一批单字符规则。"""
    rel = {"skip_slash_commands": bad}
    _normalize_relevance(rel)
    assert rel["skip_slash_commands"] == []


def test_non_string_and_blank_entries_are_dropped() -> None:
    rel = {"skip_slash_commands": ["bug-batch", 42, "", "   ", None, "commit"]}
    _normalize_relevance(rel)
    assert rel["skip_slash_commands"] == ["bug-batch", "commit"]


def test_normalized_string_config_cannot_skip_single_char_commands() -> None:
    """归一化的行为后果，直接钉死（上面那条只钉了中间产物）。"""
    rel = {"skip_slash_commands": "bug-batch"}
    _normalize_relevance(rel)
    assert _is_skipped("/b something", rel["skip_slash_commands"]) is False


# ── 第 3 层：接线（端到端跑 main()）──────────────────────────────────────
_seq = itertools.count(1)


def _run_main(monkeypatch, cwd: Path, prompt: str, skip_list, session="s-slash"):
    cfg_path = Path.home() / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg.setdefault("relevance", {})["skip_slash_commands"] = skip_list
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    payload = {"cwd": str(cwd), "prompt": prompt, "session_id": session,
               "prompt_id": f"p{next(_seq)}"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    psl.main()
    raw = out.getvalue().strip()
    return json.loads(raw) if raw else {}


@pytest.fixture
def vault_for_slash(tmp_home, tmp_vault, write_frontmatter_cache):
    """一篇必定被 `配额` 命中的笔记，用来制造「本该注入」的对照组。"""
    write_frontmatter_cache({
        "笔记/配额.md": {"tags": ["配额"], "summary": "配额上限说明",
                         "mtime": 100, "keywords": ["配额"]},
    })
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}),
                   encoding="utf-8")
    return tmp_vault


def _injected(res: dict) -> str:
    return (res.get("hookSpecificOutput") or {}).get("additionalContext") or ""


PROMPT = "/bug-batch 配额 配额上限"


def test_wiring_positive_control_injects_when_list_empty(monkeypatch, tmp_path, vault_for_slash):
    """阳性对照：名单为空时这条 prompt **确实会注入**。

    没有这条，下面那条「命中后不注入」就可能只是别的闸门拦的，对本功能零判别力。
    """
    res = _run_main(monkeypatch, tmp_path, PROMPT, [])
    assert "配额" in _injected(res), "对照组未注入 ⇒ 下面的用例失去判别力"


def test_wiring_listed_command_injects_nothing(monkeypatch, tmp_path, vault_for_slash):
    res = _run_main(monkeypatch, tmp_path, PROMPT, ["bug-batch"], session="s-slash-2")
    assert _injected(res) == ""


def test_wiring_unlisted_command_still_injects(monkeypatch, tmp_path, vault_for_slash):
    """名单非空但未命中 ⇒ 照常注入（证明闸门不是「有名单就全拦」）。"""
    res = _run_main(monkeypatch, tmp_path, PROMPT, ["some-other-cmd"], session="s-slash-3")
    assert "配额" in _injected(res)


def test_wiring_records_its_own_gate_value(monkeypatch, tmp_path, vault_for_slash):
    """gate 值要能把本闸门与其它早退区分开，否则报表上看不出「拦了多少」。"""
    seen = []
    monkeypatch.setattr(psl, "_stage_gate_record",
                        lambda turn, cfg, hi, gate: seen.append(gate))
    _run_main(monkeypatch, tmp_path, PROMPT, ["bug-batch"], session="s-slash-4")
    assert seen == ["skipped_slash_command"]


# ── 反静默：自描述字段 skip_on 与报表三态 ──────────────────────────────
# 这一组守的是「让失效不再静默」的那个机制本身。本功能有一类**必然静默失效**的
# 场景（部分命令 UPS 收不到 `/cmd` 原文），而「配了但从未生效」与「压根没配过」
# 在报表上本来完全同形 —— 与 `topic_on` / `pin_on` 立过的不变量同源。
@pytest.mark.parametrize("skip_on", [True, False])
def test_build_record_actually_persists_skip_on(tmp_path, skip_on: bool) -> None:
    """写端真的把 `skip_on` 写进记录 —— 不只是「收到了这个参数」。

    ⚠️ 这条是**变异验证暴露的缺口**：上面那条 spy 用例只检查传给 `build_record` 的
    kwargs，报表用例用的是手工造的记录，于是把 `record["skip_on"] = bool(skip_on)`
    整行删掉时，两者全绿 —— 用例绕过了被测的那一层。
    """
    from scripts._decision import Decision
    d = Decision(admitted=[], excluded=[], fulltext_path=None, fulltext_arm="",
                 any_relevant=True, relaxed=False, gate_reason="")
    rec = psl._metrics.build_record(
        d, {"内存"}, Path("D:/proj"), session_id="s1", prompt_id="p1",
        salt=psl._metrics.get_salt(tmp_path), src="", skip_on=skip_on)
    assert rec["skip_on"] is skip_on


def test_report_line_absent_when_never_configured() -> None:
    assert _render_slash_skip({}) is None
    assert _render_slash_skip({"skip_on_n": 0, "gate_dist": {}}) is None


def test_report_calls_out_configured_but_never_hit() -> None:
    """只渲染百分比等于把「0 意味着坏了」留给读者，所以必须把话说出来。"""
    line = _render_slash_skip({"skip_on_n": 12, "gate_dist": {"ok": 12}})
    assert line is not None
    assert "一次都没命中" in line
    assert "summarize-session" in line, "应指出那类 UPS 收不到命令原文的命令"


def test_report_normal_when_hit() -> None:
    line = _render_slash_skip({"skip_on_n": 3,
                               "gate_dist": {"skipped_slash_command": 7}})
    assert line is not None and "7" in line
    assert "一次都没命中" not in line


def _seed_skip_record(home: Path, session: str, *, skip_on: bool) -> None:
    """造一条「走到打分」的记录（未命中名单的形态）。走真实写端，不手搓文件。"""
    psl._metrics.write_record(home, session, {
        "_schema": 1, "ts": 10.0, "session": session, "prompt_id": f"p-{session}",
        "cwd_h": "abc123", "kw_h": ["h1"], "n_kw": 1, "src": "",
        "gate": "", "relaxed": False,
        "admitted": [], "n_admitted": 0, "arm_counts": {}, "admitted_k": 20,
        "near_miss": [], "near_miss_scorelow": [], "near_miss_k": 10,
        "max_notes": 3, "skip_on": skip_on,
    })


def test_report_actually_renders_the_line(tmp_path) -> None:
    """接线：`render_report` 真的把这一行接了出来。

    上面三条只钉住渲染函数本身——把 `render_report` 里那两行 append 删掉，
    它们照样全绿，而用户永远看不到这条告警。这里走
    `write_record → load_records → summarize → render_report` 全链路，
    不手工构造 summary dict（手搓的 dict 会绕过 summarize 的字段计算）。
    """
    from scripts.analyze_metrics import load_records, render_report, summarize
    for i in range(3):
        _seed_skip_record(tmp_path, f"s-skip-{i}", skip_on=True)
    out = render_report(summarize(load_records(tmp_path)))
    assert "skip_slash_commands" in out
    assert "一次都没命中" in out


def test_report_omits_line_entirely_when_unconfigured(tmp_path) -> None:
    """没配过该功能的用户不该看到这一行 —— 报表版面是稀缺资源。"""
    from scripts.analyze_metrics import load_records, render_report, summarize
    _seed_skip_record(tmp_path, "s-noskip", skip_on=False)
    out = render_report(summarize(load_records(tmp_path)))
    assert "skip_slash_commands" not in out


def test_wiring_passes_skip_on_to_build_record(monkeypatch, tmp_path, vault_for_slash):
    """接线：未命中的轮次必须把「名单配了没有」如实带进落盘记录。

    只测渲染函数是不够的 —— 把调用点的 `skip_on=` 删掉，上面三条照样全绿。
    """
    seen = {}
    real = psl._metrics.build_record

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(psl._metrics, "build_record", spy)
    monkeypatch.setattr(psl._metrics, "flush", lambda *a, **k: None)
    cfg_path = Path.home() / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["metrics"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    _run_main(monkeypatch, tmp_path, PROMPT, ["some-other-cmd"], session="s-skipon-1")
    assert seen.get("skip_on") is True, "配了名单的未命中轮次应落 skip_on=True"

    seen.clear()
    _run_main(monkeypatch, tmp_path, PROMPT, [], session="s-skipon-2")
    assert seen.get("skip_on") is False, "没配名单应落 skip_on=False"


def test_gate_attribution_wins_over_later_gates(monkeypatch, tmp_path, vault_for_slash):
    """顺序守卫：本闸门必须早于 `deictic_only` / `too_few_keywords`。

    用纯命令 `/bug-batch`（剥掉命令名后一个关键词都不剩）—— 它同时满足
    `too_few_keywords` 的条件。若本闸门被挪到那之后，这轮会被归因成
    `too_few_keywords`，报表上就再也看不出「是跳过名单拦的」，而**注入结果完全相同**，
    行为测试对这种归因漂移天生无感。文档主打的例子恰好落在这一格。
    """
    seen = []
    monkeypatch.setattr(psl, "_stage_gate_record", lambda t, c, h, g: seen.append(g))
    _run_main(monkeypatch, tmp_path, "/bug-batch", ["bug-batch"], session="s-order")
    assert seen == ["skipped_slash_command"]


def test_wiring_gate_runs_before_keyword_tokenization(monkeypatch, tmp_path, vault_for_slash):
    """闸门必须早于信号 J —— 分词会剥掉命令名，之后就拿不到判据输入了。"""
    called = []
    real = psl.collect_signal_j_prompt_keywords
    monkeypatch.setattr(psl, "collect_signal_j_prompt_keywords",
                        lambda *a, **k: (called.append(1), real(*a, **k))[1])
    _run_main(monkeypatch, tmp_path, PROMPT, ["bug-batch"], session="s-slash-5")
    assert called == [], "命中跳过名单的轮次不应走到分词"
