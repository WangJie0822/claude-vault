# -*- coding: utf-8 -*-
"""pinned/accounted 落盘与回读；占位不清空；旧读端不受影响。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import _topic
from scripts._state import session_topics_path
from scripts._topic import (MAX_PIN_HARD_CAP, effective_pin_max,
                            has_recent_topic_attempt, load_pinned_accounted,
                            load_session_pinned, load_session_topic,
                            mark_pinned_accounted, save_session_topic)


def _mkrepo(root: Path) -> Path:
    g = root / ".git"
    g.mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def test_roundtrip_pinned_and_words(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"], pinned=["项目笔记/a.md"], accounted=False)
    assert load_session_topic(cwd, "s1", 24) == ["精排"]
    assert load_session_pinned(cwd, "s1", 24) == ["项目笔记/a.md"]
    assert load_pinned_accounted(cwd, "s1", 24) is False


def test_placeholder_save_keeps_existing_pinned(tmp_path):
    """spawn 的 in-flight 占位 `save_session_topic(cwd, sid, [])` 不得清空上一窗口的 pinned（reverse L）。"""
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"], pinned=["项目笔记/a.md"], accounted=True)
    save_session_topic(cwd, "s1", [])            # 占位：不传 pinned/accounted
    assert load_session_pinned(cwd, "s1", 24) == ["项目笔记/a.md"]
    assert load_pinned_accounted(cwd, "s1", 24) is True
    assert load_session_topic(cwd, "s1", 24) == []


def test_explicit_empty_pinned_clears(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"], pinned=["项目笔记/a.md"])
    save_session_topic(cwd, "s1", ["精排"], pinned=[])
    assert load_session_pinned(cwd, "s1", 24) == []


def test_mark_accounted_only_flips_flag(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"], pinned=["项目笔记/a.md"], accounted=False)
    mark_pinned_accounted(cwd, "s1")
    assert load_pinned_accounted(cwd, "s1", 24) is True
    assert load_session_pinned(cwd, "s1", 24) == ["项目笔记/a.md"]
    assert load_session_topic(cwd, "s1", 24) == ["精排"]


def test_pinned_read_side_validation(tmp_path):
    """手工篡改 / 撕裂写入：非 str、含控制字符、超长、超条数都要被读端过滤。"""
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"])
    p = session_topics_path(cwd)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["topics"]["s1"]["pinned"] = (
        [42, "ok.md", "bad\x1b[31m.md", "x\ny.md", "x" * 5000]
        + [f"n{i}.md" for i in range(20)])
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    got = load_session_pinned(cwd, "s1", 24)
    assert 42 not in got
    assert "bad.md" in got                      # ANSI 被剥掉、条目保留
    assert "bad\x1b[31m.md" not in got          # 原始串不得泄漏
    assert "xy.md" in got                        # \n（M-7：字符类扩到 \t\n\r）被剥掉
    assert not any("\n" in x for x in got)
    assert all(len(x) <= 1024 for x in got)
    assert len(got) <= 8


def test_pinned_string_input_returns_empty(tmp_path):
    """篡改成字符串而非列表时返回空列表。"""
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"])
    p = session_topics_path(cwd)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["topics"]["s1"]["pinned"] = "a.md"    # str 而非 list
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    got = load_session_pinned(cwd, "s1", 24)
    assert got == []


def test_missing_and_expired_return_empty(tmp_path):
    cwd = _mkrepo(tmp_path / "p")
    assert load_session_pinned(cwd, "nope", 24) == []
    assert load_pinned_accounted(cwd, "nope", 24) is False
    save_session_topic(cwd, "s1", ["精排"], pinned=["a.md"])
    assert load_session_pinned(cwd, "s1", 0) == []          # ttl=0 即过期


def test_old_reader_ignores_new_fields(tmp_path):
    """1.2.1 的 load_session_topic 只读 ts/words，新字段对它透明。"""
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"], pinned=["a.md"], accounted=True)
    assert load_session_topic(cwd, "s1", 24) == ["精排"]


def test_read_side_clamps_to_configured_pin_max(tmp_path):
    """1B-M-6：spec §2.2 读侧 `[:session_pin_max]`——此前读端只按内部常量 8 封顶，用户在 TTL 内
    调低配置、或手工/撕裂写入的文件里多出来的条目，照样每轮按 8 篇注入。"""
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"], pinned=["a.md", "b.md", "c.md"], accounted=False)
    assert load_session_pinned(cwd, "s1", 24, pin_max=1) == ["a.md"]
    assert load_session_pinned(cwd, "s1", 24, pin_max=0) == []
    assert load_session_pinned(cwd, "s1", 24, pin_max=2) == ["a.md", "b.md"]
    assert load_session_pinned(cwd, "s1", 24) == ["a.md", "b.md", "c.md"]          # 默认仍是硬上限
    assert load_session_pinned(cwd, "s1", 24, pin_max=99) == ["a.md", "b.md", "c.md"]
    assert load_session_pinned(cwd, "s1", 24, pin_max="x") == ["a.md"]   # 垃圾值往窄的方向回落（默认 1，A-L5）


@pytest.mark.parametrize("rel, expected", [
    ({}, 1),                                                # 全默认：enabled=True、max=1
    ({"session_pin_enabled": False}, 0),
    ({"session_pin_enabled": False, "session_pin_max": 3}, 0),
    ({"session_pin_max": 3}, 3),
    ({"session_pin_max": 99}, MAX_PIN_HARD_CAP),
    ({"session_pin_max": -1}, 0),
    ({"session_pin_max": False}, 0),                        # Ruling 9：bool 按 int，false → 0 篇
    ({"session_pin_max": True}, 1),
    ({"session_pin_max": "3"}, 3),
    ({"session_pin_max": "x"}, 1),                          # 垃圾值回落默认 1
    ({"session_pin_max": None}, 1),
])
def test_effective_pin_max_is_the_single_judgement(rel, expected):
    """写读两侧共用一个点名上限（1A-L2：此前默认值散在 4 处）。"""
    assert effective_pin_max({"relevance": rel}) == expected


def test_effective_pin_max_garbage_config_shapes():
    assert effective_pin_max(None) == 1
    assert effective_pin_max({}) == 1
    assert effective_pin_max({"relevance": None}) == 1
    assert effective_pin_max({"relevance": "x"}) == 1


def test_read_side_words_are_cleaned_like_write_side(tmp_path):
    """1A-L3 实跑形态：写端 `_clean_words(['3','17','x','精排']) == ['精排']`，而修复前读端原样
    返回 `['3','17','x','精排']`——手工改过 / 旧版本写的文件绕过写端过滤，`'3'` 走词边界匹配。"""
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"])
    p = session_topics_path(cwd)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["topics"]["s1"]["words"] = ["3", "17", "x", "精排", "w" * 500]
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    got = load_session_topic(cwd, "s1", 24)
    assert got[0] == "精排"
    assert "3" not in got and "17" not in got and "x" not in got
    assert all(len(w) <= _topic.MAX_TOPIC_WORD_LEN for w in got)      # 读端也截断


def test_read_side_words_non_list_returns_empty(tmp_path):
    """`words` 被改成字符串时不得逐字符迭代出词。"""
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"])
    p = session_topics_path(cwd)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["topics"]["s1"]["words"] = "精排效果"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert load_session_topic(cwd, "s1", 24) == []


def test_all_readers_go_through_load_entry(tmp_path, monkeypatch):
    """1A-M6：四个读端共用一份「读 + 校验」阶梯。钉接线：把 `_load_entry` 桩成恒 None，
    文件里明明有合法 entry，四个读端也必须全部给出「没有」。"""
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"], pinned=["a.md"], accounted=True)
    assert has_recent_topic_attempt(cwd, "s1", 24) is True          # 前置：桩之前确有 entry
    monkeypatch.setattr(_topic, "_load_entry", lambda *a, **k: None)
    assert load_session_topic(cwd, "s1", 24) == []
    assert load_session_pinned(cwd, "s1", 24) == []
    assert load_pinned_accounted(cwd, "s1", 24) is False
    assert has_recent_topic_attempt(cwd, "s1", 24) is False


def test_mark_accounted_does_not_mutate_caller_dict(tmp_path):
    """1A-L9：mutate 重建嵌套 dict，不原地改 `update_json` 传入的对象。"""
    from scripts import _topic as t
    cwd = _mkrepo(tmp_path / "p")
    save_session_topic(cwd, "s1", ["精排"], pinned=["a.md"], accounted=False)
    seen = {}

    def fake_update_json(path, mutate, *, max_bytes=None):
        existing = {"topics": {"s1": {"words": ["精排"], "ts": 1.0, "accounted": False}}}
        inner = existing["topics"]["s1"]
        out = mutate(dict(existing))
        seen["inner_untouched"] = inner.get("accounted") is False
        seen["out_flag"] = out["topics"]["s1"]["accounted"]
        return out

    original = t.update_json
    t.update_json = fake_update_json
    try:
        mark_pinned_accounted(cwd, "s1")
    finally:
        t.update_json = original
    assert seen == {"inner_untouched": True, "out_flag": True}


def test_corrupt_topics_file_reported_once_per_process(tmp_path, capsys):
    """post-review A-L2：一轮 UPS 四个读端各调一次 `_load_entry`，损坏文件不能打三四行 stderr。"""
    cwd = _mkrepo(tmp_path / "p")
    p = session_topics_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    capsys.readouterr()
    load_session_topic(cwd, "s1", 24)
    load_session_pinned(cwd, "s1", 24)
    has_recent_topic_attempt(cwd, "s1", 24)
    load_pinned_accounted(cwd, "s1", 24)
    err = capsys.readouterr().err
    assert err.count("读会话 topics entry 失败") == 1


def test_mark_accounted_skips_write_when_missing_or_already_true(tmp_path):
    """1A-L9 后半句（post-review B-L7）：entry 不存在或已 True 时不取锁不写盘。"""
    cwd = _mkrepo(tmp_path / "p")
    calls = []
    original = _topic.update_json

    def counting(path, mutate, *, max_bytes=None):
        calls.append(path)
        return original(path, mutate, max_bytes=max_bytes)

    _topic.update_json = counting
    try:
        mark_pinned_accounted(cwd, "nope")                 # 文件都不存在
        assert calls == []
        save_session_topic(cwd, "s1", ["精排"], pinned=["a.md"], accounted=True)
        calls.clear()
        mark_pinned_accounted(cwd, "s1")                   # 已经是 True
        assert calls == []
        save_session_topic(cwd, "s2", ["精排"], pinned=["a.md"], accounted=False)
        calls.clear()
        mark_pinned_accounted(cwd, "s2")                   # 真要翻：写一次
        assert len(calls) == 1 and load_pinned_accounted(cwd, "s2", 24) is True
    finally:
        _topic.update_json = original


def test_overlong_pinned_path_dropped_not_truncated(tmp_path):
    """post-review B-L9：截断后的 path 匹配不到 cache 键、到渲染层才被静默跳过；读端直接丢。"""
    cwd = _mkrepo(tmp_path / "p")
    long_path = "项目笔记/" + "x" * (_topic.MAX_PIN_PATH_CHARS + 1) + ".md"
    save_session_topic(cwd, "s1", ["精排"], pinned=[long_path, "ok.md"], accounted=False)
    assert load_session_pinned(cwd, "s1", 24, pin_max=8) == ["ok.md"]
    assert _topic._clean_pinned([long_path]) == []
