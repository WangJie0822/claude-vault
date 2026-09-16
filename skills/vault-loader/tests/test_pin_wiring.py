# -*- coding: utf-8 -*-
"""UPS 接线：池扩展、门禁不变、pinned 读取。用 monkeypatch 拦 spawn，不起真实子进程。

Ruling 8（controller）：`_run_main` 不 monkeypatch 不存在的 `psl.load_config`
（模块只 import 了 `load_config_ex`），改为直接读写 `vault_with_project` fixture
落盘的 legacy config 文件——与 `tests/test_session_topic_wiring.py::_setup` 同一模式。
"""
from __future__ import annotations

import io
import itertools
import json
import sys
from pathlib import Path

import pytest

from scripts import prompt_submit_load as psl
from scripts._output import INJECTION_NOTICE
from scripts._topic import save_session_topic


def _mkrepo(root: Path) -> Path:
    g = root / ".git"
    g.mkdir(parents=True, exist_ok=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


# `prompt_id` 必须逐次唯一：`context_vault.runtime.HookContext.from_payload` 把它当
# `event_id`（stable=True），`main()` 早段的 `claim_event(..., scope=cwd)` 据
# (runtime, event, session_id, event_id, scope) 去重——同一 session 内固定
# `prompt_id` 会让第二次调用被判「同一事件已处理过」，在到达 pinned 对账/metrics
# 之前就静默 return 0（Task 8 的两轮记账用例首次暴露：第二轮 stdout 全空、
# `_metrics.stage` 一次都没被调）。真实宿主每轮下发不同 `prompt_id`，这里用计数器
# 还原这一点，不影响任何断言（本文件无用例依赖 `prompt_id` 的具体取值）。
_prompt_id_seq = itertools.count(1)


def _run_main(monkeypatch, cwd: Path, prompt: str, session="s-pin", cfg_extra=None):
    """在当前 pytest 进程内直接调 `psl.main()`：stdin 喂 payload，stdout 收 JSON。

    `cfg_extra`：形如 `{"relevance": {"session_pin_enabled": False}}` 的顶层配置段覆盖
    （value 为 dict 时与既有同名段深合并一层），或形如 `{"dry_run": True}` 的顶层
    标量键直接覆盖（value 非 dict 时直接赋值——`dry_run`/`enabled` 等本就是顶层标量，
    不是嵌套段，`.update()` 对它们无意义）。与 `vault_with_project` 写的同一份
    legacy config 文件（`~/.claude/skills/vault-loader/config.json`）合并后写回，
    再调 `main()`——不 monkeypatch 任何 `load_config*`。
    """
    if cfg_extra:
        cfg_path = Path.home() / ".claude" / "skills" / "vault-loader" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        for sec, kv in cfg_extra.items():
            if isinstance(kv, dict):
                cfg.setdefault(sec, {}).update(kv)
            else:
                cfg[sec] = kv
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    payload = {"cwd": str(cwd), "prompt": prompt, "session_id": session,
               "prompt_id": f"p{next(_prompt_id_seq)}"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    psl.main()
    raw = out.getvalue().strip()
    return json.loads(raw) if raw else {}


@pytest.fixture
def vault_with_project(tmp_home, tmp_vault, write_frontmatter_cache):
    """cache 里放 1 篇会被「配额」命中的笔记 + 3 篇项目目录笔记（不命中 prompt）。"""
    write_frontmatter_cache({
        "笔记/配额.md": {"tags": ["配额"], "summary": "配额上限说明", "mtime": 100, "keywords": ["配额"]},
        "项目笔记/cv/p1.md": {"tags": [], "summary": "项目笔记一", "mtime": 300, "keywords": []},
        "项目笔记/cv/p2.md": {"tags": [], "summary": "项目笔记二", "mtime": 200, "keywords": []},
        "项目笔记/cv/INDEX.md": {"tags": [], "summary": "索引", "mtime": 999, "keywords": []},
    })
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}), encoding="utf-8")
    return tmp_vault


def test_pool_includes_project_notes_and_spawn_gate_unchanged(monkeypatch, tmp_path, vault_with_project):
    cwd = _mkrepo(tmp_path / "cv")
    calls = []
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: calls.append(a) or True)
    _run_main(monkeypatch, cwd, "配额 上限 说明")
    assert len(calls) == 1
    cands = calls[0][3]
    paths = [p for p, _ in cands]
    assert paths[0] == "笔记/配额.md"                       # admitted 在前
    assert "项目笔记/cv/p1.md" in paths and "项目笔记/cv/p2.md" in paths
    assert paths.index("项目笔记/cv/p1.md") < paths.index("项目笔记/cv/p2.md")   # mtime 降序
    assert "项目笔记/cv/INDEX.md" not in paths


def test_no_spawn_when_admitted_empty_even_if_project_notes_exist(monkeypatch, tmp_path, vault_with_project):
    """reverse H：门禁必须仍是 admitted 非空，否则无匹配的开场 prompt 会把 pinned 锁定 24h。"""
    cwd = _mkrepo(tmp_path / "cv")
    calls = []
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: calls.append(a) or True)
    _run_main(monkeypatch, cwd, "完全 无关 的 提问 词语")
    assert calls == []


def test_pin_disabled_restores_v121_pool(monkeypatch, tmp_path, vault_with_project):
    cwd = _mkrepo(tmp_path / "cv")
    calls = []
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: calls.append(a) or True)
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra={"relevance": {"session_pin_enabled": False}})
    paths = [p for p, _ in calls[0][3]]
    assert paths == ["笔记/配额.md"]


def _seed_pinned(cwd: Path, session: str, paths: list[str]):
    save_session_topic(cwd, session, ["配额"], pinned=paths, accounted=False)


def test_pinned_injected_when_admitted_empty(monkeypatch, tmp_path, vault_with_project):
    """Critical R1：admitted 为空正是机械召回失效、pinned 价值最高的场景。"""
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p1.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    out = _run_main(monkeypatch, cwd, "完全 无关 的 提问 词语")
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert "[[项目笔记/cv/p1.md]]" in ctx
    assert psl.PIN_HEADER in ctx
    assert "未匹配到强相关笔记" not in json.dumps(out, ensure_ascii=False)   # 有 pinned 就不出兜底提示


def test_pinned_appended_after_normal_injection_and_deduped(monkeypatch, tmp_path, vault_with_project):
    """落盘 2 篇 pinned 须把 `session_pin_max` 配到 2：读端按配置值收口（1B-M-6），默认 1 只会读到首篇。"""
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["笔记/配额.md", "项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    out = _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra={"relevance": {"session_pin_max": 2}})
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert ctx.count("[[笔记/配额.md]]") == 1            # 已在机械清单里，pinned 不重复
    assert "[[项目笔记/cv/p2.md]]" in ctx
    assert ctx.index(psl.PIN_HEADER) > ctx.index("📚 vault-loader")   # 小节在正文之后
    assert ctx.count(INJECTION_NOTICE.rstrip("\n")) == 1             # 清单头部已有，小节不重复（1A-L7）


def test_pinned_not_in_injected_state(monkeypatch, tmp_path, vault_with_project):
    """spec §1.5 末条：pinned 不写进 save_injected。"""
    from scripts._state import load_already_injected
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    _run_main(monkeypatch, cwd, "配额 上限 说明")
    assert "项目笔记/cv/p2.md" not in load_already_injected(cwd, 24)


def test_pinned_skipped_when_already_fulltext_injected(monkeypatch, tmp_path, vault_with_project):
    from scripts._state import save_injected
    cwd = _mkrepo(tmp_path / "cv")
    save_injected(cwd, ["项目笔记/cv/p2.md"], fulltext_paths=["项目笔记/cv/p2.md"])
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    out = _run_main(monkeypatch, cwd, "配额 上限 说明")
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert "[[项目笔记/cv/p2.md]]" not in ctx


def test_gate_early_exit_does_not_inject_pinned(monkeypatch, tmp_path, vault_with_project):
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p1.md"])
    out = _run_main(monkeypatch, cwd, "ok")            # deictic_only 闸门
    assert "additionalContext" not in json.dumps(out)


def test_user_summary_mentions_pinned(monkeypatch, tmp_path, vault_with_project):
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    out = _run_main(monkeypatch, cwd, "配额 上限 说明")
    msg = out.get("systemMessage", "")
    assert "📌" in msg and "p2" in msg


def test_pinned_injected_annotated(monkeypatch, tmp_path, vault_with_project):
    from scripts import _metrics
    seen = {}
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: seen.update(kw))
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra={"metrics": {"enabled": True}})
    assert seen.get("pinned_injected") == 1


def test_dry_run_pinned_only_round_does_not_inject(monkeypatch, tmp_path, vault_with_project):
    """dry_run 契约：admitted 为空只剩 pinned 时也只预览、不落 additionalContext。"""
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p1.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    out = _run_main(monkeypatch, cwd, "完全 无关 的 提问 词语", cfg_extra={"dry_run": True})
    assert "additionalContext" not in json.dumps(out)
    assert "[DRY-RUN]" in out.get("systemMessage", "") and "📌" in out.get("systemMessage", "")


def test_pinned_summary_titles_are_sanitized():
    """SEC-1：pinned 标题与其它标题同走 _title()——U+2028 折叠、超长截断。"""
    from scripts._config_loader import DEFAULT_CONFIG
    rel_cfg = DEFAULT_CONFIG["relevance"]
    bad = "项目笔记/cv/a 伪造.md"
    long = "项目笔记/cv/" + "x" * 500 + ".md"
    msg = psl.build_summary_ups([], set(), None, "正文", {"verbosity": "compact"}, rel_cfg,
                                hits_by_path={}, pinned_paths=[bad, long])
    assert " " not in msg
    assert "x" * 500 not in msg
    assert "📌" in msg and "2笔记" in msg


def test_accounting_happens_once_on_first_pinned_round(monkeypatch, tmp_path, vault_with_project):
    from scripts import _metrics
    from scripts._topic import load_pinned_accounted
    staged = []
    monkeypatch.setattr(_metrics, "stage", lambda rec: staged.append(rec))
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: None)
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    cfg = {"metrics": {"enabled": True}}
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra=cfg)
    _run_main(monkeypatch, cwd, "配额 上限 再问", cfg_extra=cfg)
    assert len(staged) == 2
    assert staged[0]["llm_pinned"] == ["项目笔记/cv/p2.md"]
    assert staged[0]["llm_pinned_rank"] == [None]        # 该篇不在本轮 admitted 里
    assert isinstance(staged[0]["topic_extracted_h"], list) and len(staged[0]["topic_extracted_h"]) == 1
    assert staged[0]["n_topic_extracted"] == 1
    assert "llm_pinned" not in staged[1]                  # 第二轮不再记
    assert load_pinned_accounted(cwd, "s-pin", 24) is True


def test_rank_is_position_in_admitted(monkeypatch, tmp_path, vault_with_project):
    from scripts import _metrics
    staged = []
    monkeypatch.setattr(_metrics, "stage", lambda rec: staged.append(rec))
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: None)
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["笔记/配额.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra={"metrics": {"enabled": True}})
    assert staged[0]["llm_pinned_rank"] == [1]


def test_pinned_after_fulltext_fence_and_deduped_against_fulltext_pick(
        monkeypatch, tmp_path, vault_with_project, write_frontmatter_cache):
    """I-2：spec §6「渲染两分支各追加」此前只有清单分支的 wiring 守卫——全文分支恰是
    没有外层 NOTICE、pinned 必须自带隔离声明的那条路径。prompt「配额上限说明 详细」
    产生两条独立证据链（「配额上限说明」5 个相邻 bigram 合并成一条 + 「详细」单独一条，
    chains=2 ⇒ `has_strong_evidence` 成立，`_scorer.py:281-289`）触发全文分支——单独
    连写「配额上限说明」只产生 1 条合并链，chains<2 达不到强证据档，故 fixture 需要
    这第二条独立链；cache summary/keywords 据此加宽，与终审报告 hands-on 探针
    （`D:\\Temp\\final-review-mut\\probe_branches.py` run1）逐字一致，已实测走到全文分支。
    pinned 含本轮全文候选自身（「笔记/配额.md」）与另一篇（p2），断言：小节落在 nonce
    fence 闭合之后（`>>>` 是闭合行 `f"{fence}>>>"` 的稳定后缀，fence 本身是随机 hex，
    不含 `>`），且全文候选与 pinned 重复时只渲染一次。"""
    write_frontmatter_cache({
        "笔记/配额.md": {"tags": ["配额"], "summary": "配额上限说明的详细摘要文本", "mtime": 100,
                        "keywords": ["配额", "上限"]},
        "项目笔记/cv/p1.md": {"tags": [], "summary": "项目笔记一的摘要文本足够长", "mtime": 300, "keywords": []},
        "项目笔记/cv/p2.md": {"tags": [], "summary": "项目笔记二的摘要文本足够长", "mtime": 200, "keywords": []},
        "项目笔记/cv/INDEX.md": {"tags": [], "summary": "索引", "mtime": 999, "keywords": []},
    })
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["笔记/配额.md", "项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    out = _run_main(monkeypatch, cwd, "配额上限说明 详细", cfg_extra={"relevance": {"session_pin_max": 2}})
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    if "强命中" not in ctx:
        raise AssertionError(
            "NEEDS_CONTEXT：该 fixture 未走全文分支，实际 additionalContext 开头：\n"
            + ctx[:500])
    assert ctx.index(psl.PIN_HEADER) > ctx.rindex(">>>")   # pinned 小节在 fence 闭合之后
    assert ctx.count("[[笔记/配额.md]]") == 1               # 本轮全文候选自身，去重不重复渲染
    assert ctx.count("[[项目笔记/cv/p2.md]]") == 1
    # 全文分支的隔离声明在 fence **内**（属于 ft_body），pinned 小节追加在 fence 外、必须自带一份：
    # 最后一次出现须在 fence 闭合之后（清单分支则只有头部那一份，见上一条用例的 count == 1）。
    notice = INJECTION_NOTICE.rstrip("\n")
    assert ctx.rindex(notice) > ctx.rindex(">>>")
    assert ctx.index(notice) < ctx.rindex(">>>") < ctx.rindex(notice)


def test_rank_two_when_pinned_note_is_second_in_admitted(
        monkeypatch, tmp_path, vault_with_project, write_frontmatter_cache):
    """T8（deferred fix now）：`test_rank_is_position_in_admitted` 只覆盖 rank=1，
    没有任何用例覆盖 rank>1 的形态。追加一篇同 tag/keywords、mtime 更小的笔记
    （`_decision.py` admitted 排序 `(-total, -mtime)`，total 并列时新 mtime 排前），
    pin 那篇 mtime 更小的，期望它排第二。"""
    from scripts import _metrics
    staged = []
    monkeypatch.setattr(_metrics, "stage", lambda rec: staged.append(rec))
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: None)
    write_frontmatter_cache({
        "笔记/配额.md": {"tags": ["配额"], "summary": "配额上限说明", "mtime": 100, "keywords": ["配额"]},
        "项目笔记/cv/p1.md": {"tags": [], "summary": "项目笔记一", "mtime": 300, "keywords": []},
        "项目笔记/cv/p2.md": {"tags": [], "summary": "项目笔记二", "mtime": 200, "keywords": []},
        "项目笔记/cv/INDEX.md": {"tags": [], "summary": "索引", "mtime": 999, "keywords": []},
        "笔记/配额2.md": {"tags": ["配额"], "summary": "配额上限说明二", "mtime": 50, "keywords": ["配额"]},
    })
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["笔记/配额2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra={"metrics": {"enabled": True}})
    observed = staged[0]["llm_pinned_rank"]
    if observed != [2]:
        raise AssertionError(
            f"NEEDS_CONTEXT：观测 llm_pinned_rank={observed}，与预期 [2] 不符，"
            "两篇笔记的并列排序与假设不同，需确认后再收紧断言")
    assert observed == [2]


def test_topic_hash_never_contains_plaintext(monkeypatch, tmp_path, vault_with_project):
    from scripts import _metrics
    staged = []
    monkeypatch.setattr(_metrics, "stage", lambda rec: staged.append(rec))
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: None)
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra={"metrics": {"enabled": True}})
    assert "配额" not in json.dumps(staged[0].get("topic_extracted_h"), ensure_ascii=False)


def test_accounting_marker_not_flipped_when_flush_fails(monkeypatch, tmp_path, vault_with_project):
    """1A-M2 / 1B-M-3：标记只在 flush 成功之后翻。修复前 stage 之后立即翻，flush 抛异常那份每会话
    一次的对账数据永久丢失且无信号；现在下一轮重记（at-least-once）。"""
    from scripts import _metrics
    from scripts._topic import load_pinned_accounted
    staged = []
    monkeypatch.setattr(_metrics, "stage", lambda rec: staged.append(rec))
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: None)

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(_metrics, "flush", boom)
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    cfg = {"metrics": {"enabled": True}}
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra=cfg)
    assert "llm_pinned" in staged[0]
    assert load_pinned_accounted(cwd, "s-pin", 24) is False          # 没落盘就不翻
    monkeypatch.setattr(_metrics, "flush", lambda *a, **k: None)      # 下一轮盘好了
    _run_main(monkeypatch, cwd, "配额 上限 再问", cfg_extra=cfg)
    assert "llm_pinned" in staged[1]                                   # 重记
    assert load_pinned_accounted(cwd, "s-pin", 24) is True


def test_accounting_deferred_until_admitted_nonempty(monkeypatch, tmp_path, vault_with_project):
    """1B-M-4：admitted 为空的轮次没有机械排名可对，一次性名额留到下一个有 admitted 的轮次。"""
    from scripts import _metrics
    from scripts._topic import load_pinned_accounted
    staged = []
    monkeypatch.setattr(_metrics, "stage", lambda rec: staged.append(rec))
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: None)
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    cfg = {"metrics": {"enabled": True}}
    _run_main(monkeypatch, cwd, "完全 无关 的 提问 词语", cfg_extra=cfg)   # admitted 为空、pinned 仍注入
    assert "llm_pinned" not in staged[0]
    assert load_pinned_accounted(cwd, "s-pin", 24) is False
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra=cfg)
    assert staged[1]["llm_pinned"] == ["项目笔记/cv/p2.md"]
    assert load_pinned_accounted(cwd, "s-pin", 24) is True


def test_llm_pinned_dedup_names_fulltext_injected(monkeypatch, tmp_path, vault_with_project,
                                                  write_frontmatter_cache):
    """终审 I-3：rank=None 分「被跨轮去重压掉」与「真不在 admitted」。配额.md 已拿过全文 ⇒
    决策层 dedup='fulltext_injected'；p2 不 topical ⇒ 真未召回 ⇒ None。配额2 保证 admitted 非空。"""
    from scripts import _metrics
    from scripts._state import save_injected
    staged = []
    monkeypatch.setattr(_metrics, "stage", lambda rec: staged.append(rec))
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: None)
    write_frontmatter_cache({
        "笔记/配额.md": {"tags": ["配额"], "summary": "配额上限说明", "mtime": 100, "keywords": ["配额"]},
        "笔记/配额2.md": {"tags": ["配额"], "summary": "配额上限说明二", "mtime": 50, "keywords": ["配额"]},
        "项目笔记/cv/p1.md": {"tags": [], "summary": "项目笔记一", "mtime": 300, "keywords": []},
        "项目笔记/cv/p2.md": {"tags": [], "summary": "项目笔记二", "mtime": 200, "keywords": []},
    })
    cwd = _mkrepo(tmp_path / "cv")
    save_injected(cwd, ["笔记/配额.md"], fulltext_paths=["笔记/配额.md"])
    _seed_pinned(cwd, "s-pin", ["笔记/配额.md", "项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    _run_main(monkeypatch, cwd, "配额 上限 说明",
              cfg_extra={"metrics": {"enabled": True}, "relevance": {"session_pin_max": 2}})
    assert staged[0]["llm_pinned_rank"] == [None, None]
    assert staged[0]["llm_pinned_dedup"] == ["fulltext_injected", None]


def test_pool_composition_recorded_on_spawn_round(monkeypatch, tmp_path, vault_with_project):
    """1B-H-1：候选池构成随记录落盘——fixture 里 admitted 1 篇 + 项目笔记 2 篇（INDEX 排除）。
    关掉 pin 时池回落纯 admitted：n_pool_proj 恒 0 且 pin_on=False 说明为什么。"""
    from scripts import _metrics
    staged = []
    monkeypatch.setattr(_metrics, "stage", lambda rec: staged.append(rec))
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: None)
    cwd = _mkrepo(tmp_path / "cv")
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra={"metrics": {"enabled": True}})
    assert staged[0]["pin_on"] is True
    assert staged[0]["n_pool"] == 3 and staged[0]["n_pool_proj"] == 2
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra={"relevance": {"session_pin_enabled": False}})
    assert staged[1]["pin_on"] is False
    assert staged[1]["n_pool"] == 1 and "n_pool_proj" not in staged[1]   # 关着时不算项目片，不落键（A-M1）


def test_pool_fields_absent_when_no_spawn(monkeypatch, tmp_path, vault_with_project):
    """没送出提炼的轮次不得落 n_pool（否则「池为 0」会被读成组件 A 退化）。"""
    from scripts import _metrics
    staged = []
    monkeypatch.setattr(_metrics, "stage", lambda rec: staged.append(rec))
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: None)
    cwd = _mkrepo(tmp_path / "cv")
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    _run_main(monkeypatch, cwd, "完全 无关 的 提问 词语", cfg_extra={"metrics": {"enabled": True}})
    assert "n_pool" not in staged[0] and "n_pool_proj" not in staged[0]


def test_pinned_injected_recorded_on_every_scored_round_not_on_gate(monkeypatch, tmp_path, vault_with_project):
    """1A-L12 口径：走到打分阶段的轮次一律落 `pinned_injected`（含 0，admitted 空也落），闸门早退不落。"""
    from scripts import _metrics
    seen = []
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: seen.append(dict(kw)))
    cwd = _mkrepo(tmp_path / "cv")
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)

    def _pinned_vals():
        return [d["pinned_injected"] for d in seen if "pinned_injected" in d]
    _run_main(monkeypatch, cwd, "配额 上限 说明")            # admitted 非空、无 pinned
    assert _pinned_vals() == [0]
    seen.clear()
    _run_main(monkeypatch, cwd, "完全 无关 的 提问 词语")     # admitted 为空、无 pinned
    assert _pinned_vals() == [0]
    seen.clear()
    _run_main(monkeypatch, cwd, "ok")                          # 闸门早退
    assert _pinned_vals() == []


def test_summary_omits_empty_brackets_on_pinned_only_round():
    """1A-L4 实跑形态：修复前 `📚 vault-loader(提问): 1笔记[] 命中[] · ~4 字 · 📌某篇 · /vault 展开`。"""
    from scripts._config_loader import DEFAULT_CONFIG
    rel_cfg = DEFAULT_CONFIG["relevance"]
    msg = psl.build_summary_ups([], set(), None, "正文", {"verbosity": "compact"}, rel_cfg,
                                hits_by_path={}, pinned_paths=["项目笔记/cv/p1.md"])
    assert "[]" not in msg and "命中[" not in msg
    assert "1笔记" in msg and "📌p1" in msg
    msgv = psl.build_summary_ups([], set(), None, "正文", {"verbosity": "verbose"}, rel_cfg,
                                 hits_by_path={}, pinned_paths=["项目笔记/cv/p1.md"])
    assert "命中[" not in msgv and "1 笔记" in msgv and "📌p1" in msgv


def test_read_side_clamp_drops_extra_pins_under_default_config(monkeypatch, tmp_path, vault_with_project):
    """1B-M-6 接线：文件里 2 篇、配置默认 `session_pin_max=1` ⇒ 只注入首篇。变异：读端不传 pin_max 即红。"""
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p1.md", "项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    out = _run_main(monkeypatch, cwd, "完全 无关 的 提问 词语")
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    assert "[[项目笔记/cv/p1.md]]" in ctx and "[[项目笔记/cv/p2.md]]" not in ctx


def test_pool_project_share_counts_admitted_project_notes_too(monkeypatch, tmp_path, vault_with_project,
                                                              write_frontmatter_cache):
    """post-review A-M1 路径二：既是项目笔记又已 admitted 的篇也算项目片——它同样证明前缀解析是对的。
    q.md 命中「配额」进 admitted，p1/p2 走项目片：池 3 篇、项目归属 3 篇（按补集数只会得 2）。"""
    from scripts import _metrics
    staged = []
    monkeypatch.setattr(_metrics, "stage", lambda rec: staged.append(rec))
    monkeypatch.setattr(_metrics, "annotate", lambda **kw: None)
    write_frontmatter_cache({
        "项目笔记/cv/q.md": {"tags": ["配额"], "summary": "配额上限说明", "mtime": 100, "keywords": ["配额"]},
        "项目笔记/cv/p1.md": {"tags": [], "summary": "项目笔记一", "mtime": 300, "keywords": []},
        "项目笔记/cv/p2.md": {"tags": [], "summary": "项目笔记二", "mtime": 200, "keywords": []},
        "项目笔记/cv/INDEX.md": {"tags": [], "summary": "索引", "mtime": 999, "keywords": []},
    })
    cwd = _mkrepo(tmp_path / "cv")
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra={"metrics": {"enabled": True}})
    assert staged[0]["n_pool"] == 3 and staged[0]["n_pool_proj"] == 3


def test_accounting_marker_not_flipped_on_metrics_stub_path(monkeypatch, tmp_path, vault_with_project):
    """post-review A-L1：`_metrics` 退化成 stub（build_record 返回 {}、flush 无声成功）时不得翻标记——
    没有记录却翻了，等于那份一次性对账数据静默永久丢失。"""
    from scripts._topic import load_pinned_accounted

    class _Stub:
        SCHEMA = 1

        @staticmethod
        def current_schema():
            return 1

        @staticmethod
        def runtime_record_fields():
            return {}

        @staticmethod
        def stage(record):
            return None

        @staticmethod
        def flush(home, retention_days=None):
            return None

        @staticmethod
        def annotate(**_k):
            return None

        @staticmethod
        def build_record(*_a, **_k):
            return {}

        @staticmethod
        def get_salt(home):
            return b""

        @staticmethod
        def h(text, salt):
            return ""

        @staticmethod
        def nudge_due(home, threshold=10, ttl_hours=168):
            return []

        @staticmethod
        def mark_nudged(home):
            return None

    monkeypatch.setattr(psl, "_metrics", _Stub())
    cwd = _mkrepo(tmp_path / "cv")
    _seed_pinned(cwd, "s-pin", ["项目笔记/cv/p2.md"])
    monkeypatch.setattr(psl, "spawn_topic_extraction", lambda *a, **k: True)
    _run_main(monkeypatch, cwd, "配额 上限 说明", cfg_extra={"metrics": {"enabled": True}})
    assert load_pinned_accounted(cwd, "s-pin", 24) is False
