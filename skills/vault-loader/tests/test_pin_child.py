# -*- coding: utf-8 -*-
"""首轮点名（2.4.0 子进程 pin 模式）：编号 + «文件名» 回显 → pinned 落盘；模型失败后读端取不到词与点名；
点名上限来自 payload（`effective_pin_max` 单点），垃圾值在读侧收口。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts import _topic
from scripts._topic import (build_fine_payload, load_pinned_accounted, load_session_pinned,
                            load_session_topic, run_extraction_child)

GEN = "0123456789abcdef"
NOTES = {f"项目笔记/n{i}.md": {"tags": ["精排"], "summary": f"精排笔记 {i}", "mtime": 100 + i, "keywords": []}
         for i in range(9)}


def _mkrepo(root: Path) -> Path:
    g = root / ".git"
    g.mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


@pytest.fixture
def vault(tmp_vault, write_frontmatter_cache):
    write_frontmatter_cache(NOTES)
    return tmp_vault


def _payload(vault, rel=None):
    return build_fine_payload("pin", prompt="精排怎么做", config={"relevance": dict(rel or {}),
                                                                "vault_path": str(vault)},
                              gen_id=GEN, src_seq=1, slot=None, transcript_path="", injected=[],
                              excluded=[], prev_words=[], extra_paths=[])


def _run(cwd, vault, monkeypatch, reply, *, pin_max=None):
    seen = {}

    def fake(text):
        seen["prompt"] = text
        return reply(text) if callable(reply) else reply

    monkeypatch.setattr(_topic, "_call_model", fake)
    p = _payload(vault)
    if pin_max is not None:
        p["cfg"]["pin_max"] = pin_max
    assert run_extraction_child([str(cwd), "s1"], stdin_text=json.dumps(p, ensure_ascii=False)) == 0
    return seen


def _num(text, name):
    return int(re.search(r"(\d+) - «" + re.escape(name) + "»", text).group(1))


def _pins(*names):
    return lambda t: "PINNED: " + ", ".join(f"{_num(t, n)} «{n}»" for n in names) + "\nWORDS: 精排,排序"


def test_child_saves_pinned_by_echoed_number(tmp_path, vault, monkeypatch):
    cwd = _mkrepo(tmp_path / "p")
    _run(cwd, vault, monkeypatch, _pins("n2"))
    assert load_session_pinned(cwd, "s1", 24) == ["项目笔记/n2.md"]
    assert load_session_topic(cwd, "s1", 24) == ["精排", "排序"]
    assert load_pinned_accounted(cwd, "s1", 24) is False


def test_child_respects_pin_max_from_payload(tmp_path, vault, monkeypatch):
    cwd = _mkrepo(tmp_path / "p")
    seen = _run(cwd, vault, monkeypatch, _pins("n1", "n2", "n3"), pin_max=2)
    assert load_session_pinned(cwd, "s1", 24, pin_max=8) == ["项目笔记/n1.md", "项目笔记/n2.md"]
    assert "最多 2 篇" in seen["prompt"]


def test_child_model_failure_leaves_loader_empty(tmp_path, vault, monkeypatch):
    # 只经读端断言：读端对 failed / pending 的 entry 同样返回空，证明不了「没写 topics 文件」；
    # 那条（F01）由 test_fine_child.py::test_pin_failure_touches_only_fine 断言文件不存在
    cwd = _mkrepo(tmp_path / "p")
    _run(cwd, vault, monkeypatch, None)
    assert load_session_topic(cwd, "s1", 24) == []
    assert load_session_pinned(cwd, "s1", 24) == []


def test_child_none_pin_gives_empty_pinned(tmp_path, vault, monkeypatch):
    cwd = _mkrepo(tmp_path / "p")
    _run(cwd, vault, monkeypatch, "PINNED: NONE\nWORDS: 精排")
    assert load_session_pinned(cwd, "s1", 24) == []
    assert load_session_topic(cwd, "s1", 24) == ["精排"]


def test_payload_pin_max_follows_effective_pin_max(vault):
    assert _payload(vault, {"session_pin_max": 3})["cfg"]["pin_max"] == 3
    assert _payload(vault, {"session_pin_enabled": False, "session_pin_max": 3})["cfg"]["pin_max"] == 0


def test_child_with_pin_max_zero_parses_plain_words_and_never_pins(tmp_path, vault, monkeypatch):
    cwd = _mkrepo(tmp_path / "p")
    seen = _run(cwd, vault, monkeypatch, "精排,排序", pin_max=0)
    assert "PINNED" not in seen["prompt"]
    assert load_session_topic(cwd, "s1", 24) == ["精排", "排序"]
    assert load_session_pinned(cwd, "s1", 24) == []


@pytest.mark.parametrize("raw_pin_max, expected", [
    ("3", 1),     # 非 int → 回落默认 1
    (True, 1),    # bool 同样回落（isinstance(bool, int) 为 True，须显式排除）
    (-1, 0),      # 负数夹到下界 0
    (99, 8),      # 超过 MAX_PIN_HARD_CAP 夹到上界 8
])
def test_child_pin_max_garbage_in_payload_is_clamped(tmp_path, vault, monkeypatch, raw_pin_max, expected):
    """payload 是不可信输入（stdin 由父进程写，校验仍须在读侧自持）。"""
    cwd = _mkrepo(tmp_path / "p")
    seen = _run(cwd, vault, monkeypatch, _pins(*[f"n{i}" for i in range(9)]), pin_max=raw_pin_max)
    assert len(load_session_pinned(cwd, "s1", 24, pin_max=8)) == expected
    if expected == 0:
        assert "PINNED" not in seen["prompt"]
