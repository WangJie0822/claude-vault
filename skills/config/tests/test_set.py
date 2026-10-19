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
import os
import time
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


@pytest.mark.parametrize("payload", ["[1, 2, 3]", "42", '"text"', "null"],
                         ids=["array", "number", "string", "null"])
def test_restore_rejects_non_object_backup(_isolate_home, tmp_path, payload):
    """备份顶层不是 JSON 对象：经 CLI 入口 rc≠0，目标文件逐字节不变，也不留备份（2.4.0 full-review DO-M2）。
    写进去的话 loader 判 config 读坏、整份回落默认值，用户关掉的精排与目录层会被默认值重新打开。"""
    p = _write(_isolate_home, {"relevance": {"fine_rank": False, "fine_catalog": False}})
    before = p.read_bytes()
    bad = tmp_path / "bad.json"
    bad.write_text(payload, encoding="utf-8")
    backups = cs._migrate_config().backup_dir_path()
    assert backups.is_relative_to(_isolate_home)

    assert cs.main(["--restore", str(bad)]) != 0

    assert p.read_bytes() == before
    assert not backups.exists() or not any(backups.iterdir())


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


def test_retired_codex_switch_cannot_be_written(_isolate_home):
    res = cs.set_key("runtimes.codex.enabled", "false", home=_isolate_home, assume_yes=True)
    assert res["ok"] is False
    assert "已废弃" in res["error"]


# ── spec 2026-10-13 §5.3 / §11：旧键、BOM、读不出、锁、非字符串入参、备份数量 ────────────
def test_superseded_key_is_refused_with_hint(_isolate_home):
    res = cs.set_key("relevance.session_topic", "false", home=_isolate_home, assume_yes=True)
    assert res["ok"] is False and "relevance.fine_rank" in res["error"]
    assert not (_isolate_home / ".context-vault" / "config.json").exists()


def test_writing_new_key_removes_superseded_key(_isolate_home):
    """写新键时把同义旧键一并删掉（同一次原子写，带备份）：否则两键并存，读者分不清哪个生效。"""
    p = _write(_isolate_home, {"relevance": {"session_topic": False, "min_topical_score": 7}})
    res = cs.set_key("relevance.fine_rank", "false", home=_isolate_home, assume_yes=True)
    assert res["ok"], res
    assert _read(p)["relevance"] == {"fine_rank": False, "min_topical_score": 7}
    assert res["removed"] == ["relevance.session_topic"]
    assert _read(Path(res["backup"]))["relevance"]["session_topic"] is False


def test_removal_of_superseded_key_is_verified_by_reading_back(_isolate_home, monkeypatch):
    """回读核验同时核「旧键已删」：落盘时旧键还在就要报错，不能静默留下两键并存。
    （brief 之外的补充守卫：不加它，把核验里的 `not gone` 去掉，上面那条用例照样绿。）"""
    _write(_isolate_home, {"relevance": {"session_topic": False}})
    real = cs._atomic_write

    def keep_old_key(path, data):
        data["relevance"]["session_topic"] = False          # 模拟旧键没被带走
        real(path, data)

    monkeypatch.setattr(cs, "_atomic_write", keep_old_key)
    res = cs.set_key("relevance.fine_rank", "false", home=_isolate_home, assume_yes=True)
    assert res["ok"] is False and "核验" in res["error"]
    # 报错要点名残留的旧键，而不是「期望 False，实际 False」这种自相矛盾的话
    assert "relevance.session_topic" in res["error"]


def test_unreadable_config_is_not_overwritten(_isolate_home):
    """文件在但读不出：此前当成空 dict 写回，用户的整份配置被一个键替换（F38）。"""
    p = _isolate_home / ".context-vault" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'{"vault_path": "D:/V", oops')
    before = p.read_bytes()
    res = cs.set_key("metrics.enabled", "true", home=_isolate_home, assume_yes=True)
    assert res["ok"] is False and "读不出" in res["error"]
    assert p.read_bytes() == before


def test_bom_config_keeps_its_content(_isolate_home):
    p = _isolate_home / ".context-vault" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"vault_path": "D:/V", "relevance": {"min_topical_score": 7}}
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(body).encode("utf-8"))
    res = cs.set_key("relevance.min_topical_score", "9", home=_isolate_home, assume_yes=True)
    assert res["ok"], res
    assert json.loads(p.read_text(encoding="utf-8-sig")) == {
        "vault_path": "D:/V", "relevance": {"min_topical_score": 9}}


def test_non_string_values_are_accepted(_isolate_home):
    """hook 里面板的「设为默认」传的是 bool / int，不是 CLI 的字符串（F42）。"""
    r1 = cs.set_key("relevance.fine_max_notes", 2, home=_isolate_home, assume_yes=True)
    assert r1["ok"] and r1["new"] == 2
    r2 = cs.set_key("relevance.fine_rank", False, home=_isolate_home, assume_yes=True)
    assert r2["ok"] and r2["new"] is False
    r3 = cs.set_key("relevance.fine_exclude_tags", ["private", "x"], home=_isolate_home,
                    assume_yes=True, privacy_ack=True)
    assert r3["ok"] and r3["new"] == ["private", "x"]


def test_writer_waits_for_the_config_lock(_isolate_home, monkeypatch):
    """读改写全程持锁（F47）：面板 persist 与 /config 可能同时写同一个文件。"""
    p = _write(_isolate_home, {"relevance": {"min_topical_score": 7}})
    lock = p.with_name(p.name + ".lock")
    # 刚创建的锁：年龄远小于陈旧阈值，lease_lock 不会去探测持有者是否存活，只会等到超时
    lock.write_text(f"{os.getpid()} {time.time()} held-by-test", encoding="ascii")
    monkeypatch.setattr(cs, "_LOCK_TIMEOUT", 0.2)
    res = cs.set_key("relevance.min_topical_score", "9", home=_isolate_home, assume_yes=True)
    assert res["ok"] is False and "锁" in res["error"]
    assert _read(p)["relevance"]["min_topical_score"] == 7
    assert lock.exists()                                   # 别人的锁不得被删
    lock.unlink()
    assert cs.set_key("relevance.min_topical_score", "9", home=_isolate_home,
                      assume_yes=True)["ok"]               # 阳性对照：锁释放后照常写入


def test_lock_oserror_refuses_without_raising(_isolate_home, monkeypatch):
    """POSIX 上配置目录不可写时，lease_lock 原样抛 PermissionError：set_key 要返回 ok=False 的 dict，
    不能把异常抛给按 ok / error 分支处理的调用方。"""
    p = _write(_isolate_home, {"relevance": {"min_topical_score": 7}})
    before = p.read_bytes()

    class _Denied:
        def __enter__(self):
            raise PermissionError(13, "Permission denied", "config.json.lock")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cs, "_config_lock", lambda _path: _Denied())
    res = cs.set_key("relevance.min_topical_score", "9", home=_isolate_home, assume_yes=True)
    assert res["ok"] is False and "写锁" in res["error"]
    assert p.read_bytes() == before


def test_write_oserror_keeps_its_own_message(_isolate_home, monkeypatch):
    """持锁后写入失败仍走「写入失败」，不被取锁失败的 `except OSError` 改成写锁措辞。"""
    p = _write(_isolate_home, {"relevance": {"min_topical_score": 7}})

    def denied(_path, _data):
        raise PermissionError(13, "Permission denied", "config.json")

    monkeypatch.setattr(cs, "_atomic_write", denied)
    res = cs.set_key("relevance.min_topical_score", "9", home=_isolate_home, assume_yes=True)
    assert res["ok"] is False and "写入失败" in res["error"] and "写锁" not in res["error"]
    assert _read(p)["relevance"]["min_topical_score"] == 7


def test_none_value_is_refused(_isolate_home):
    """`None` 不得被转成字面量 "None" 写进 str 型键（relevance.fine_model）。"""
    p = _write(_isolate_home, {"relevance": {"min_topical_score": 7}})
    before = p.read_bytes()
    res = cs.set_key("relevance.fine_model", None, home=_isolate_home, assume_yes=True)
    assert res["ok"] is False and "缺少值" in res["error"]
    assert p.read_bytes() == before


def test_missing_lock_implementation_refuses_to_write(_isolate_home, monkeypatch):
    """拿不到锁实现时 fail-closed：拒写并点名「写锁」，而不是退回无锁写入。（brief 之外的补充守卫）"""
    p = _write(_isolate_home, {"relevance": {"min_topical_score": 7}})

    def no_lock(_path):
        raise ImportError("No module named 'context_vault'")

    monkeypatch.setattr(cs, "_config_lock", no_lock)
    res = cs.set_key("relevance.min_topical_score", "9", home=_isolate_home, assume_yes=True)
    assert res["ok"] is False and "写锁" in res["error"]
    assert _read(p)["relevance"]["min_topical_score"] == 7


def test_backups_keep_latest_twenty(_isolate_home):
    _write(_isolate_home, {"relevance": {"min_topical_score": 7}})
    bdir = _isolate_home / ".claude" / "projects" / "vault-loader-backups"
    bdir.mkdir(parents=True)
    old = [bdir / f"config-20200101T0000{i:02d}000000Z.json" for i in range(25)]
    for f in old:
        f.write_text("{}", encoding="utf-8")
    other = bdir / "notes.txt"
    other.write_text("keep", encoding="utf-8")
    # 用户手工放的副本：名字以 config- 开头、.json 结尾，且按名排序排在全部时间戳备份之前——
    # 淘汰按名排序删最旧的，宽 glob 会第一个删它（不可逆）。必须只认备份生产者的时间戳形态。
    handmade = [bdir / "config-0notes.json", bdir / "config-0my-handmade-copy.json"]
    for f in handmade:
        f.write_text("{}", encoding="utf-8")
    res = cs.set_key("relevance.min_topical_score", "9", home=_isolate_home, assume_yes=True)
    left = sorted(x.name for x in bdir.glob("config-2*.json"))   # 只数时间戳形态的备份
    assert len(left) == 20
    assert Path(res["backup"]).name in left                # 刚写的那份留着
    assert old[0].name not in left and old[-1].name in left
    assert other.exists()                                  # 非 json 的文件不碰
    assert all(f.exists() for f in handmade)               # 名字像备份、形态不是时间戳的也不碰
