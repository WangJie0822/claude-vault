"""回落解析器（未装 PyYAML）认 YAML 块序列，且与 PyYAML 路径的产出一致（2.4.0 full-review S-H1）。

块序列是 tags 的常见写法（`tags:` 换行 `  - private`）。回落解析器此前不认它：`tags:`
后面的值为空就整键丢掉，下一行 `- private` 又匹配不到键正则，于是 cache 里没有这篇的 tags，vault-loader
的 private 排除看不到它（该篇的文件名与摘要照样进精排外发 prompt）。

**两种情形怎么测**：本机没装 PyYAML。`test_block_seq_fallback_matches_pyyaml` 对同一段 frontmatter 各跑
一次写端的两个分支——`_HAS_YAML=True`（PyYAML 分支：`safe_load` 之后按写端口径把 list 逐项 `str()`、
跳过 None、标量 `str()`）与 `_HAS_YAML=False`（回落解析器），断言二者产出相同。PyYAML 分支用的
`yaml` 模块：装了就用真库，并顺带核对下表录下的 `safe_load` 结果；没装就换成按下表回放的替身
（monkeypatch `rebuild_index.yaml`）。下表第三列是真 PyYAML 6.0.3 的 `safe_load` 输出（2026-10-19
在隔离目录装 PyYAML 实跑录下），不是手写的期望值。
"""
from __future__ import annotations

import copy
import types

import pytest

import rebuild_index as R

# (用例名, frontmatter 正文（两条 --- 之间）, 真 PyYAML 6.0.3 的 safe_load 结果)
PARITY = [
    ("indent2", "tags:\n  - private\n  - diary\nsummary: s",
     {"tags": ["private", "diary"], "summary": "s"}),
    ("flush", "tags:\n- private\n- diary\nsummary: s",
     {"tags": ["private", "diary"], "summary": "s"}),
    ("indent4", "category: c\ntags:\n    - private\nsummary: s",
     {"category": "c", "tags": ["private"], "summary": "s"}),
    ("quoted", "tags:\n  - \"Private\"\n  - 'diary'\nsummary: s",
     {"tags": ["Private", "diary"], "summary": "s"}),
    ("hash_quoted", "tags:\n  - \"#private\"\nsummary: s",
     {"tags": ["#private"], "summary": "s"}),
    ("nested", "tags:\n  - private/journal\nsummary: s",
     {"tags": ["private/journal"], "summary": "s"}),
    ("blank_between", "tags:\n  - a\n\n  - b\nsummary: s",
     {"tags": ["a", "b"], "summary": "s"}),
    ("comment_between", "tags:\n  # c\n  - a\n  # d\n  - b\nsummary: s",
     {"tags": ["a", "b"], "summary": "s"}),
    ("crlf", "tags:\r\n  - private\r\nsummary: s\r",
     {"tags": ["private"], "summary": "s"}),
    ("keywords_cjk", "keywords:\n  - 召回\n  - 精排\ntags: [x]",
     {"keywords": ["召回", "精排"], "tags": ["x"]}),
    ("last_key", "summary: s\ntags:\n  - private",
     {"summary": "s", "tags": ["private"]}),
    ("two_keys", "tags:\n  - a\nkeywords:\n  - k1\n  - k2\nsummary: s",
     {"tags": ["a"], "keywords": ["k1", "k2"], "summary": "s"}),
    ("item_space", "tags:\n  - my tag\nsummary: s",
     {"tags": ["my tag"], "summary": "s"}),
    ("after_block_scalar", "summary: >-\n  line one\n  line two\ntags:\n  - private",
     {"summary": "line one line two", "tags": ["private"]}),
    ("empty_key_then_key", "tags:\nsummary: s",
     {"tags": None, "summary": "s"}),
]


class _StubYAMLError(Exception):
    pass


def _yaml_backend():
    """(PyYAML 分支要用的 yaml 模块, 是否真库)。"""
    try:
        import yaml  # noqa: PLC0415
        return yaml, True
    except ImportError:
        table = {body: raw for _n, body, raw in PARITY}
        return types.SimpleNamespace(YAMLError=_StubYAMLError,
                                     safe_load=lambda src: copy.deepcopy(table[src])), False


def _md(body: str) -> str:
    return "---\n" + body + "\n---\n# 正文\n"


@pytest.mark.parametrize("name, body, raw", PARITY, ids=[c[0] for c in PARITY])
def test_block_seq_fallback_matches_pyyaml(monkeypatch, name, body, raw):
    yaml_mod, real = _yaml_backend()
    if real:
        assert yaml_mod.safe_load(body) == raw, "录制表与本机 PyYAML 的 safe_load 不一致，先更新录制表"
    monkeypatch.setattr(R, "yaml", yaml_mod, raising=False)
    monkeypatch.setattr(R, "_HAS_YAML", True)
    via_yaml = R.parse_frontmatter(_md(body))
    monkeypatch.setattr(R, "_HAS_YAML", False)
    via_fallback = R.parse_frontmatter(_md(body))
    assert via_fallback == via_yaml
    # 非空网：块序列那几键在两条分支里都必须是 list（两边都丢键时上一条断言也会成立）
    for k, v in raw.items():
        if isinstance(v, list):
            assert via_fallback[k] == [str(x) for x in v]


def test_block_seq_fallback_deliberate_divergences(monkeypatch):
    """刻意与 PyYAML 不同的两处（见 `_take_seq` 的 docstring）：`- #x` 收成 '#x'（YAML 里是注释，
    PyYAML 得 None、写端转成 'None'），空条目跳过（PyYAML 同样得 None）。"""
    monkeypatch.setattr(R, "_HAS_YAML", False)
    assert R.parse_frontmatter(_md("tags:\n  - #private\nsummary: s"))["tags"] == ["#private"]
    assert R.parse_frontmatter(_md("tags:\n  -\n  - b\nsummary: s"))["tags"] == ["b"]


def test_block_seq_does_not_eat_nested_mapping(monkeypatch):
    """只吃 `- ` 开头的行：键后面跟的是缩进映射时维持既有行为（映射内的键按顶层键读出），不当成序列。"""
    monkeypatch.setattr(R, "_HAS_YAML", False)
    assert R.parse_frontmatter(_md("meta:\n  a: 1\nsummary: s")) == {"a": "1", "summary": "s"}


def test_read_entry_keeps_block_seq_tags_and_keywords(tmp_path, monkeypatch):
    """写进 cache 的那一步（`_read_entry`）：块序列的 tags 与 keywords 都以 list 进 entry。"""
    monkeypatch.setattr(R, "_HAS_YAML", False)
    (tmp_path / "技术笔记").mkdir()
    (tmp_path / "技术笔记" / "a.md").write_text(
        "---\ntags:\n  - private\n  - 日记\nkeywords:\n  - 召回\nsummary: 一段摘要\n---\n# A\n",
        encoding="utf-8")
    entry = R._read_entry(tmp_path, "技术笔记/a.md", 1)
    assert entry["tags"] == ["private", "日记"]
    assert entry["keywords"] == ["召回"]
    assert entry["summary"] == "一段摘要"
