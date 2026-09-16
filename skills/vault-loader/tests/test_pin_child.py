# -*- coding: utf-8 -*-
"""子进程入口：双段输出 → pinned 落盘；模型失败不落 pinned；pin_max 从 stdin 读。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import _topic
from scripts._topic import (load_pinned_accounted, load_session_pinned,
                            load_session_topic, run_extraction_child)


def _mkrepo(root: Path) -> Path:
    g = root / ".git"
    g.mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def _stdin(prompt, cands, pin_max=1):
    return json.dumps({"prompt": prompt, "candidates": [list(c) for c in cands], "pin_max": pin_max},
                      ensure_ascii=False)


def test_child_saves_pinned_from_pool_index(tmp_path, monkeypatch):
    cwd = _mkrepo(tmp_path / "p")
    cands = [("项目笔记/a.md", "A"), ("项目笔记/b.md", "B"), ("项目笔记/c.md", "C")]
    monkeypatch.setattr(_topic, "_call_model", lambda text: "PINNED: 2\nWORDS: 精排,排序")
    rc = run_extraction_child([str(cwd), "s1"], stdin_text=_stdin("q", cands))
    assert rc == 0
    assert load_session_pinned(cwd, "s1", 24) == ["项目笔记/b.md"]
    assert load_session_topic(cwd, "s1", 24) == ["精排", "排序"]
    assert load_pinned_accounted(cwd, "s1", 24) is False


def test_child_respects_pin_max_from_stdin(tmp_path, monkeypatch):
    cwd = _mkrepo(tmp_path / "p")
    cands = [(f"n{i}.md", "x") for i in range(5)]
    seen = {}
    def fake(text):
        seen["prompt"] = text
        return "PINNED: 1,2,3\nWORDS: 精排"
    monkeypatch.setattr(_topic, "_call_model", fake)
    run_extraction_child([str(cwd), "s1"], stdin_text=_stdin("q", cands, pin_max=2))
    assert load_session_pinned(cwd, "s1", 24) == ["n0.md", "n1.md"]
    assert "最多 2 个" in seen["prompt"]


def test_child_model_failure_saves_placeholder_without_pinned(tmp_path, monkeypatch):
    cwd = _mkrepo(tmp_path / "p")
    monkeypatch.setattr(_topic, "_call_model", lambda text: None)
    run_extraction_child([str(cwd), "s1"], stdin_text=_stdin("q", [("a.md", "A")]))
    assert load_session_topic(cwd, "s1", 24) == []
    assert load_session_pinned(cwd, "s1", 24) == []


def test_child_none_pin_gives_empty_pinned(tmp_path, monkeypatch):
    cwd = _mkrepo(tmp_path / "p")
    monkeypatch.setattr(_topic, "_call_model", lambda text: "PINNED: NONE\nWORDS: 精排")
    run_extraction_child([str(cwd), "s1"], stdin_text=_stdin("q", [("a.md", "A")]))
    assert load_session_pinned(cwd, "s1", 24) == []
    assert load_session_topic(cwd, "s1", 24) == ["精排"]


def test_spawn_payload_carries_pin_max(tmp_path, monkeypatch):
    """spawn 把 config 里的 session_pin_max 放进 stdin，且门禁/占位行为不变。"""
    cwd = _mkrepo(tmp_path / "p")
    captured = {}
    class _P:
        class stdin:
            @staticmethod
            def write(b): captured["payload"] = json.loads(b.decode("utf-8"))
            @staticmethod
            def close(): pass
    monkeypatch.setattr(_topic.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(_topic.subprocess, "Popen", lambda *a, **k: _P())
    cfg = {"relevance": {"session_pin_max": 3}}
    assert _topic.spawn_topic_extraction(cwd, "s1", "q", [("a.md", "A")], cfg) is True
    assert captured["payload"]["pin_max"] == 3


def test_prompt_reverts_to_words_only_when_pin_max_zero():
    """session_pin_enabled 关 ⇒ pin_max=0 ⇒ 1.2.1 单段模板：无 PINNED 行、候选行无编号。"""
    from scripts._topic import build_topic_prompt
    text = build_topic_prompt("q", [("项目笔记/a.md", "摘要A")], pin_max=0)
    assert "PINNED" not in text and "WORDS" not in text
    assert "提炼 3-8 个用于检索本地知识库的中文关键词" in text
    assert "\n- 项目笔记/a.md：«摘要A»\n" in text
    assert "1 - 项目笔记/a.md" not in text


def test_spawn_payload_pin_max_zero_when_pin_disabled(tmp_path, monkeypatch):
    cwd = _mkrepo(tmp_path / "p")
    captured = {}
    class _P:
        class stdin:
            @staticmethod
            def write(b): captured["payload"] = json.loads(b.decode("utf-8"))
            @staticmethod
            def close(): pass
    monkeypatch.setattr(_topic.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(_topic.subprocess, "Popen", lambda *a, **k: _P())
    cfg = {"relevance": {"session_pin_enabled": False, "session_pin_max": 3}}
    assert _topic.spawn_topic_extraction(cwd, "s1", "q", [("a.md", "A")], cfg) is True
    assert captured["payload"]["pin_max"] == 0


def test_child_with_pin_max_zero_parses_plain_words_and_never_pins(tmp_path, monkeypatch):
    cwd = _mkrepo(tmp_path / "p")
    seen = {}
    def fake(text):
        seen["prompt"] = text
        return "精排,排序"                      # 1.2.1 形态的纯词输出
    monkeypatch.setattr(_topic, "_call_model", fake)
    run_extraction_child([str(cwd), "s1"], stdin_text=_stdin("q", [("a.md", "A"), ("b.md", "B")], pin_max=0))
    assert "PINNED" not in seen["prompt"]
    assert load_session_topic(cwd, "s1", 24) == ["精排", "排序"]
    assert load_session_pinned(cwd, "s1", 24) == []


@pytest.mark.parametrize("raw_pin_max, expected", [
    ("3", 1),     # 非 int → run_extraction_child 内回落默认 1
    (True, 1),    # bool 同样回落默认 1（isinstance(bool, int) 为 True，须显式排除）
    (-1, 0),      # 负数被夹到下界 0
    (99, 8),      # 超过 MAX_PIN_HARD_CAP 被夹到上界 8
])
def test_child_pin_max_garbage_in_payload_is_clamped(tmp_path, monkeypatch, raw_pin_max, expected):
    """`payload["pin_max"]` 是不可信输入（stdin 由 spawn 写，但校验仍须在读侧自持）：
    非 int/bool/超界值必须被夹到 [0, MAX_PIN_HARD_CAP] 之内，不能原样传给
    `build_topic_prompt`/`parse_pin_and_words`。"""
    cwd = _mkrepo(tmp_path / "p")
    cands = [(f"n{i}.md", "x") for i in range(9)]
    seen = {}
    def fake(text):
        seen["prompt"] = text
        return "PINNED: 1,2,3,4,5,6,7,8,9\nWORDS: 精排"
    monkeypatch.setattr(_topic, "_call_model", fake)
    run_extraction_child([str(cwd), "s1"], stdin_text=_stdin("q", cands, pin_max=raw_pin_max))
    assert len(load_session_pinned(cwd, "s1", 24)) == expected
    if expected == 0:
        assert "PINNED" not in seen["prompt"]
