# -*- coding: utf-8 -*-
"""decide_injection 的 auto_fulltext 开关（spec 2.4.0 §8.2 / Q3）。

语料照抄 test_decision.py 的 `_two_candidate_corpus`（topical 序与 total 序相反的两条全文合格条目，
A.md topical 9 / total 9，B.md topical 6 / total 10；默认参数下全文主候选是 A.md）。复制而不 import：
测试模块之间互相导入会把一个文件的私有构造器变成另一个文件的隐式契约。
"""
from __future__ import annotations

import pytest

from scripts._config_loader import DEFAULT_CONFIG
from scripts._decision import StateView, decide_injection
from scripts._frontmatter_reader import Entry
from scripts._scorer import Signals


def _entry(path, tags=(), summary="", keywords=(), mtime=1000.0):
    return Entry(path=path, tags=tuple(tags), summary=summary, keywords=tuple(keywords), mtime=mtime)


def _corpus():
    a = _entry("A.md", tags=("alpha",), summary="无关摘要占位说明文字内容",
               keywords=("gamma",), mtime=1000000000)
    b = _entry("B.md", tags=("beta", "ctx"), summary="beta alpha 说明文档内容", mtime=1900000000)
    signals = Signals(target_tags={"ctx"}, prompt_keywords={"alpha", "beta", "gamma"})
    return {"A.md": a, "B.md": b}, signals


def _decide(state=None, **kw):
    entries, signals = _corpus()
    return decide_injection(entries, signals, DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG,
                            state or StateView(), **kw)


def test_default_keeps_fulltext_and_upgrade_candidate():
    """阳性对照：默认参数下语料确实会触发全文与升级候选，否则下面几条的「没有」没有判别力。"""
    d = _decide()
    assert d.fulltext_path == "A.md"
    d2 = _decide(StateView(candidate_injected={"A.md"}))
    by = {ed.path: ed for ed in d2.admitted}
    assert by["A.md"].admit_arm == "upgrade_candidate"
    assert d2.fulltext_path == "A.md"


def test_auto_fulltext_off_has_no_fulltext_path():
    d = _decide(auto_fulltext=False)
    assert d.fulltext_path is None and d.fulltext_arm == ""
    assert [ed.path for ed in d.admitted] == [ed.path for ed in _decide().admitted]


def test_auto_fulltext_off_does_not_readmit_upgrade_candidate():
    d = _decide(StateView(candidate_injected={"A.md"}), auto_fulltext=False)
    assert [ed.path for ed in d.admitted] == ["B.md"]
    ex = {ed.path: ed for ed in d.excluded}
    assert ex["A.md"].dedup == "candidate_injected"
    assert ex["A.md"].admit_arm == ""
    assert d.any_relevant is True
    assert d.fulltext_path is None


def test_auto_fulltext_is_keyword_only():
    entries, signals = _corpus()
    with pytest.raises(TypeError):
        decide_injection(entries, signals, DEFAULT_CONFIG["scoring"], DEFAULT_CONFIG,
                         StateView(), False)
