# -*- coding: utf-8 -*-
"""标注落盘 (session, prompt_h)，让人工标注可关联到具体提问。

背景（2026-09-02）：已有 74 条标注只有 `path` + `verdict`，**没有它对应哪次提问**。
同一篇笔记对 query A 相关、对 query B 不相关，缺了 query 关联就算不出任何排序指标——
这正是层 2 三个候选方案（W 判据 / keywords IDF / summary IDF）全都无法验收的直接原因。

隐私边界不变：只落 `prompt_h`（加盐 hash，metrics 里本来就有）与 `session`，
**不落 prompt 原文**——原文按需经 transcript 回查，落盘一份就等于把现有契约作废。

⚠️ 承重守卫两条：
  test_web_annotation_ignores_browser_supplied_ids —— 浏览器不可信，标识符必须服务端查
  test_saved_annotation_never_contains_prompt_text —— 隐私契约
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.analyze_metrics import (annotations_path, apply_web_annotations,
                                     attach_contexts, load_annotations,
                                     pick_readable_contexts, save_annotation)

SECRET = "这句提问原文绝不允许出现在任何落盘文件里"


# ── pick_readable_contexts 现在要一并交出「选中的是哪几条」 ──────────────
def test_pick_readable_returns_ids_aligned_with_items() -> None:
    events = [("s1", "h1", ["a"]), ("s2", "h2", ["b"]), ("s3", "h3", [])]
    resolve = {"h1": "第一条提问", "h2": "第二条提问", "h3": "第三条提问"}.get
    items, reasons, ids = pick_readable_contexts(
        events, lambda s, ph: resolve(ph), want=3)
    assert len(items) == len(ids) == 3
    assert ids == [("s1", "h1"), ("s2", "h2"), ("s3", "h3")]


def test_pick_readable_ids_exclude_unreadable() -> None:
    """回查不到 / 乱码的那些不能进 ids —— 否则标注会关联到读不出来的提问。"""
    events = [("s1", "h1", []), ("s2", "h2", []), ("s3", "h3", [])]

    def resolve(_s, ph):
        return {"h1": "", "h2": "正常提问", "h3": "坏�内容"}[ph]

    items, reasons, ids = pick_readable_contexts(events, resolve, want=3)
    assert ids == [("s2", "h2")], f"只有可读的那条能进：{ids}"
    assert len(items) == 1
    assert reasons["unresolved"] == 1 and reasons["corrupt"] == 1


def test_pick_readable_ids_respect_want_limit() -> None:
    events = [(f"s{i}", f"h{i}", []) for i in range(5)]
    items, _r, ids = pick_readable_contexts(events, lambda s, ph: "有内容", want=2)
    assert len(items) == len(ids) == 2


# ── attach_contexts 把标识符挂到条目上 ─────────────────────────────────
def test_attach_contexts_carries_ids(monkeypatch) -> None:
    rec = {"session": "sess-1", "prompt_h": "h-1",
           "near_miss_scorelow": [{"path": "n.md", "topical": 5.0}]}
    out = attach_contexts([{"path": "n.md", "kind": "near_miss"}],
                          lambda: iter([rec]),
                          lambda s, ph: "可读的提问", want=3)
    assert len(out) == 1
    assert out[0]["context_ids"] == [("sess-1", "h-1")]


# ── save_annotation 落盘 ────────────────────────────────────────────────
def _read(home: Path) -> list[dict]:
    p = annotations_path(home)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_save_annotation_persists_context_ids(tmp_path: Path) -> None:
    save_annotation(tmp_path, "a.md", "relevant", kind="near_miss",
                    context_ids=[("s1", "h1"), ("s2", "h2")])
    rec = _read(tmp_path)[-1]
    assert rec["context_ids"] == [{"session": "s1", "prompt_h": "h1"},
                                  {"session": "s2", "prompt_h": "h2"}]


def test_save_annotation_without_ids_omits_the_key(tmp_path: Path) -> None:
    """不传就不落该键——旧记录与新记录都合法，读端不必区分两种形态。"""
    save_annotation(tmp_path, "a.md", "relevant", kind="near_miss")
    rec = _read(tmp_path)[-1]
    assert "context_ids" not in rec


def test_existing_annotations_still_load(tmp_path: Path) -> None:
    """向后兼容：新增字段不得让既有 74 条标注读不出来。"""
    p = annotations_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"_schema": 3, "path": "old.md",
                             "verdict": "irrelevant"}) + "\n", encoding="utf-8")
    save_annotation(tmp_path, "new.md", "relevant", kind="near_miss",
                    context_ids=[("s1", "h1")])
    loaded = load_annotations(tmp_path)
    assert loaded[("near_miss", "old.md")] == "irrelevant"
    assert loaded[("near_miss", "new.md")] == "relevant"


# ── 承重守卫 1：浏览器提交的标识符一律不采信 ────────────────────────────
def test_web_annotation_ignores_browser_supplied_ids(tmp_path: Path) -> None:
    """标识符必须由服务端按 (path, kind) 从自己的 items 里查，不能用请求体里的。

    请求体来自浏览器、一律不可信（同 verdict/kind 的既有约定）。若采信它，
    攻击者可以把任意 session/prompt_h 写进这份不可再生的标注数据。
    """
    items = [{"path": "a.md", "kind": "near_miss",
              "context_ids": [("real-s", "real-h")]}]
    payload = [{"path": "a.md", "kind": "near_miss", "verdict": "relevant",
                "context_ids": [{"session": "FORGED", "prompt_h": "FORGED"}]}]
    saved, errs = apply_web_annotations(tmp_path, payload, items=items)
    assert saved == 1 and not errs
    rec = _read(tmp_path)[-1]
    assert rec["context_ids"] == [{"session": "real-s", "prompt_h": "real-h"}]
    assert "FORGED" not in json.dumps(rec, ensure_ascii=False)


def test_web_annotation_without_matching_item_omits_ids(tmp_path: Path) -> None:
    """服务端查不到对应条目时，宁可不落标识符，也不编一个。"""
    saved, errs = apply_web_annotations(
        tmp_path, [{"path": "ghost.md", "kind": "near_miss", "verdict": "relevant"}],
        items=[])
    assert saved == 1 and not errs
    assert "context_ids" not in _read(tmp_path)[-1]


def test_web_annotation_matches_on_kind_too(tmp_path: Path) -> None:
    """同一 path 的两种 kind 是两个独立判断（load_annotations 的既有约定），
    标识符也必须按 (path, kind) 配对，不能只按 path。"""
    items = [{"path": "a.md", "kind": "near_miss", "context_ids": [("s-nm", "h-nm")]},
             {"path": "a.md", "kind": "admitted_list", "context_ids": [("s-al", "h-al")]}]
    apply_web_annotations(
        tmp_path, [{"path": "a.md", "kind": "admitted_list", "verdict": "relevant"}],
        items=items)
    assert _read(tmp_path)[-1]["context_ids"] == [{"session": "s-al", "prompt_h": "h-al"}]


# ── 承重守卫 2：隐私契约 ────────────────────────────────────────────────
def test_saved_annotation_never_contains_prompt_text(tmp_path: Path) -> None:
    """落盘只允许出现 hash 与 session id，绝不能出现 prompt 原文。

    这条是隐私契约的守卫：现有 `_metrics` 明确「不落盘 prompt 原文」，
    本次新增字段若图省事直接把回查出来的文本存进去，契约当场作废且无人会发现。
    """
    items = [{"path": "a.md", "kind": "near_miss",
              "context_ids": [("s1", "h1")],
              "contexts": [(SECRET, ["命中词"])]}]     # 条目上确实带着原文
    apply_web_annotations(
        tmp_path, [{"path": "a.md", "kind": "near_miss", "verdict": "relevant"}],
        items=items)
    save_annotation(tmp_path, "b.md", "relevant", kind="near_miss",
                    context_ids=[("s2", "h2")])
    raw = annotations_path(tmp_path).read_text(encoding="utf-8")
    assert SECRET not in raw, "prompt 原文泄漏进了标注文件"


@pytest.mark.parametrize("bad", [None, "not-a-list", 123, [("only-one",)], [()]])
def test_malformed_context_ids_never_raise(tmp_path: Path, bad) -> None:
    """畸形输入不得让标注写入失败——标注是不可再生数据，宁可少个字段也不能丢整条。"""
    save_annotation(tmp_path, "a.md", "relevant", kind="near_miss", context_ids=bad)
    rec = _read(tmp_path)[-1]
    assert rec["path"] == "a.md" and rec["verdict"] == "relevant"
