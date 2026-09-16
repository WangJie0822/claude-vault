# -*- coding: utf-8 -*-
"""写端：安全改写用户配置。

安全边界**继承自同仓 `migrate_config.py`**（它已过一轮安全评审，docstring 逐条记录了
S-4/S-5/R11 落实的守卫），而不是重新发明——首版设计只重合了「备份」「原子写」两条，
漏掉重定向守卫、备份隔离位置、restore、写入白名单。

两条最容易踩的具名守卫（评审 M-4，PoC 实测）：
- **禁止以 `load_config()` 的返回值为写入源**：它返回的是合并后的完整配置，写回去会
  一次性物化 66 个叶子键，此后该用户**所有**默认值演进永久静默失效；且 `_config_version`
  / `_comment` 已在合并时被剥除，写回等于删掉迁移版本判据与指向 SKILL.md 的唯一线索。
- 判据必须**同时**断言「叶子键数不变」与「meta 键仍在」——只断言前者时，剥掉 meta 的
  实现照样绿。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config_set as cs


def _write(home: Path, data: dict) -> Path:
    p = home / ".context-vault" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# ── 核心：只改点名键 ───────────────────────────────────────────────────
def test_only_the_named_key_changes(_isolate_home):
    p = _write(_isolate_home, {
        "_config_version": 1,
        "_comment": "完整可配键见 SKILL.md",
        "vault_path": "D:/MyVault",
        "relevance": {"min_topical_score": 7, "use_tag_idf": False},
    })
    before = _read(p)

    cs.set_key("relevance.min_topical_score", "9", home=_isolate_home, assume_yes=True)

    after = _read(p)
    assert after["relevance"]["min_topical_score"] == 9
    # 其余逐字不变
    assert after["vault_path"] == before["vault_path"]
    assert after["relevance"]["use_tag_idf"] is False
    assert set(_flat(after)) == set(_flat(before)), "叶子键集合不得变化"


def _flat(d, pre=""):
    out = {}
    for k, v in d.items():
        key = f"{pre}{k}"
        if isinstance(v, dict):
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out


def test_meta_keys_survive_and_defaults_are_not_materialized(_isolate_home):
    """M-4 的两条具名守卫，必须同时断言。"""
    p = _write(_isolate_home, {"_config_version": 1, "_comment": "见 SKILL.md"})
    cs.set_key("metrics.enabled", "true", home=_isolate_home, assume_yes=True)
    after = _read(p)

    assert "_config_version" in after, "meta 键被剥除 —— 多半是写回了 load_config() 的输出"
    assert "_comment" in after
    leaves = _flat(after)
    assert len(leaves) <= 4, (
        f"叶子键被物化到 {len(leaves)} 个 —— 写入源必须是原始 JSON，不是合并后的配置")


def test_creates_file_when_absent(_isolate_home):
    """配置文件不存在时，写端**可以**创建（与读端相反：用户明确要求改）。"""
    res = cs.set_key("metrics.enabled", "true", home=_isolate_home, assume_yes=True)
    assert res["ok"]
    p = _isolate_home / ".context-vault" / "config.json"
    assert p.is_file() and _read(p)["metrics"]["enabled"] is True


# ── 备份 ───────────────────────────────────────────────────────────────
def test_backup_is_written_before_overwrite(_isolate_home):
    _write(_isolate_home, {"relevance": {"min_topical_score": 7}})
    res = cs.set_key("relevance.min_topical_score", "9", home=_isolate_home, assume_yes=True)
    bak = Path(res["backup"])
    assert bak.is_file(), "覆盖写之前必须留下备份"
    assert _read(bak)["relevance"]["min_topical_score"] == 7, "备份必须是**被覆盖前**的内容"


def test_restore_puts_the_old_value_back(_isolate_home):
    p = _write(_isolate_home, {"relevance": {"min_topical_score": 7}})
    res = cs.set_key("relevance.min_topical_score", "9", home=_isolate_home, assume_yes=True)
    cs.restore(res["backup"], home=_isolate_home, assume_yes=True)
    assert _read(p)["relevance"]["min_topical_score"] == 7


# ── 类型校验 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("key,bad", [
    ("relevance.min_topical_score", "abc"),      # int 位置给字符串
    ("metrics.enabled", "42"),                   # bool 位置给数字
    ("relevance.exclude_note_tags", "notalist"), # list 位置给字符串
])
def test_type_mismatch_is_rejected(_isolate_home, key, bad):
    res = cs.set_key(key, bad, home=_isolate_home, assume_yes=True)
    assert not res["ok"] and "类型" in res["error"]


def test_bool_is_not_accepted_as_int(_isolate_home):
    """`isinstance(True, int)` 为真——不特判就会让 `true` 写进 int 键。"""
    res = cs.set_key("relevance.session_topic_top_n", "true",
                     home=_isolate_home, assume_yes=True, privacy_ack=True)
    assert not res["ok"] and "类型" in res["error"]


# ── 白名单：不该写的一律拒绝 ───────────────────────────────────────────
@pytest.mark.parametrize("key", [
    "user_prompt_submit.min_score",      # 废弃键
    "obsidian_cli.enabled",              # 外部键（只认 canonical，路径会分叉）
    "work_log_dir",                      # 外部键（不在 DEFAULT_CONFIG）
    "relevance.__not_a_real_key__",      # 不存在
    "_config_version",                   # 元键
])
def test_non_writable_keys_are_rejected(_isolate_home, key):
    res = cs.set_key(key, "1", home=_isolate_home, assume_yes=True)
    assert not res["ok"], f"{key} 不该被写入"


# ── 隐私旋钮 ───────────────────────────────────────────────────────────
def test_privacy_knob_needs_explicit_acknowledgement(_isolate_home):
    """三个 int 键控制「多少篇笔记发往外部模型端点」。

    它们的类型是普通 int，类型校验全放行；不额外确认，一句自然语言指令就能把
    外发量放大两个数量级而没有任何提示。
    """
    res = cs.set_key("relevance.session_topic_top_n", "500",
                     home=_isolate_home, assume_yes=True)
    assert not res["ok"] and "隐私" in res["error"]

    ok = cs.set_key("relevance.session_topic_top_n", "50",
                    home=_isolate_home, assume_yes=True, privacy_ack=True)
    assert ok["ok"]


# ── 回读核验 ───────────────────────────────────────────────────────────
def test_write_is_verified_by_reading_back(_isolate_home, monkeypatch):
    """写后必须回读核验；核验不过要报错，不能静默成功。"""
    _write(_isolate_home, {"relevance": {"min_topical_score": 7}})

    real = cs._atomic_write

    def sabotage(path, data):          # 模拟写入没真正落盘
        real(path, {"relevance": {"min_topical_score": 7}})

    monkeypatch.setattr(cs, "_atomic_write", sabotage)
    res = cs.set_key("relevance.min_topical_score", "9",
                     home=_isolate_home, assume_yes=True)
    assert not res["ok"] and "核验" in res["error"]
