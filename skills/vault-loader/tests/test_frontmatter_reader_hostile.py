"""`load_cache_status` 的畸形输入守卫（H-A / T-2）。

这个函数是**每次 hook 调用的必经之路**，而且是诊断链的**触发点**——cache 损坏时
由它返回 `CORRUPT`，上层才能告知用户。所以它自己失败得比 cache 损坏更严重：

- 它抛出的异常若逃出 `except` 元组，会冒泡到 hook 顶层；顶层兜底只在
  `if __name__ == "__main__"` 里，import 期与函数期的行为不同，实测表现为
  **stdout 全空、诊断永不触发**——用户看到的是「知识库悄无声息地没了」，
  而这正是这批改动想消灭的那种失效。
- 它把一条坏笔记升级成整份索引作废，会让 499 篇健康笔记陪葬。

cache 文件的内容不受本插件控制：写端是另一个 skill，用户可能手工编辑，
也可能来自 clone 的他人 Vault。所以「畸形输入」是常态假设，不是攻击假设。

用例分三组：根形状 / 单字段类型 / 爆炸半径。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts._frontmatter_reader import (
    CACHE_VERSION,
    CacheStatus,
    load_cache_status,
)


def _vault(tmp_path: Path, payload: str) -> Path:
    """写一份原始 cache 文本（故意不经 json.dumps，便于构造非法结构）。"""
    v = tmp_path / "V"
    (v / ".meta").mkdir(parents=True)
    (v / ".meta" / "frontmatter-cache.json").write_text(payload, encoding="utf-8")
    return v


def _ok_entry(**over) -> dict:
    e = {"tags": ["x"], "category": "c", "summary": "s", "mtime": 1, "updated": "u"}
    e.update(over)
    return e


def _payload(entries: dict) -> str:
    return json.dumps({"_version": CACHE_VERSION, "entries": entries})


# ── 组 1：JSON 根形状 ────────────────────────────────────────────────
# 根不是 dict 时 `data.get(...)` 抛 AttributeError。它不在 except 元组里 →
# 直接冒泡出函数。


@pytest.mark.parametrize(
    "raw", ["[]", '["a", "b"]', '"just a string"', "123", "null", "true"],
    ids=["empty_list", "list", "string", "int", "null", "bool"],
)
def test_non_dict_root_is_corrupt_not_raise(tmp_path: Path, raw: str) -> None:
    entries, status = load_cache_status(_vault(tmp_path, raw))
    assert entries == {}
    assert status is CacheStatus.CORRUPT


def test_deeply_nested_json_does_not_raise(tmp_path: Path) -> None:
    """深嵌套触发 RecursionError——它是 RuntimeError 的子类，**不是** ValueError，
    所以补 TypeError/AttributeError 并不能覆盖它。这是本组最容易漏的一条。

    体积仅 ~80KB，远在 10MB 上限之内，OVERSIZE 那道闸拦不住。
    """
    depth = 20000
    entries, status = load_cache_status(_vault(tmp_path, "[" * depth + "]" * depth))
    assert entries == {}
    assert status is CacheStatus.CORRUPT


def test_invalid_json_is_corrupt(tmp_path: Path) -> None:
    entries, status = load_cache_status(_vault(tmp_path, "{not json"))
    assert entries == {}
    assert status is CacheStatus.CORRUPT


def test_entries_not_dict_is_corrupt(tmp_path: Path) -> None:
    entries, status = load_cache_status(
        _vault(tmp_path, json.dumps({"_version": CACHE_VERSION, "entries": []}))
    )
    assert entries == {}
    assert status is CacheStatus.CORRUPT


# ── 组 2：单字段类型 ─────────────────────────────────────────────────


def test_scalar_tags_not_iterated_per_char(tmp_path: Path) -> None:
    """`tags: "foo"` 不得被逐字符迭代成 ('f','o','o')。

    这不只是脏数据：假 tag 会进 build_tag_df 参与 IDF 统计，而 tag-IDF 加权正是
    0.5.0 的核心特性——三个各出现一次的单字符 tag 会被算成高信息量的精确信号。
    """
    v = _vault(tmp_path, _payload({"n.md": _ok_entry(tags="foo")}))
    entries, status = load_cache_status(v)
    assert status is CacheStatus.OK
    assert entries["n.md"].tags == (), f"标量 tags 被迭代成了 {entries['n.md'].tags}"


@pytest.mark.parametrize(
    "bad", [123, {"a": 1}, True], ids=["int", "dict", "bool"]
)
def test_non_list_tags_degrade_to_empty(tmp_path: Path, bad) -> None:
    v = _vault(tmp_path, _payload({"n.md": _ok_entry(tags=bad)}))
    entries, status = load_cache_status(v)
    assert status is CacheStatus.OK
    assert entries["n.md"].tags == ()


def test_keywords_guard_still_holds(tmp_path: Path) -> None:
    """对照组：keywords 侧本就有 isinstance 守卫，不得回退。"""
    v = _vault(tmp_path, _payload({"n.md": _ok_entry(keywords="abc")}))
    entries, _ = load_cache_status(v)
    assert entries["n.md"].keywords == ()


@pytest.mark.parametrize(
    "bad", ["NaN", "abc", None, {}, [], "1e999"],
    ids=["nan_str", "text", "none", "dict", "list", "overflow_str"],
)
def test_bad_mtime_does_not_kill_whole_index(tmp_path: Path, bad) -> None:
    """一条笔记的 mtime 非法，**不得**让整份索引作废。

    原实现里 `int()` 抛 ValueError 被最外层捕获 → 整份 CORRUPT：
    3 篇笔记只坏 1 篇，存活 0 条。
    """
    v = _vault(tmp_path, _payload({
        "good1.md": _ok_entry(mtime=100),
        "bad.md": _ok_entry(mtime=bad),
        "good2.md": _ok_entry(mtime=200),
    }))
    entries, status = load_cache_status(v)
    assert status is CacheStatus.OK, f"单条坏数据把整份索引判成了 {status}"
    assert "good1.md" in entries and "good2.md" in entries, "健康笔记未存活"
    assert entries["good1.md"].mtime == 100


def test_bad_entry_does_not_kill_index_generic(tmp_path: Path) -> None:
    """更广的爆炸半径守卫：任何单条 entry 的异常都只丢那一条。"""
    v = _vault(tmp_path, _payload({
        "good.md": _ok_entry(),
        "bad.md": {"tags": ["ok"], "mtime": {"nested": "not-an-int"}},
    }))
    entries, status = load_cache_status(v)
    assert status is CacheStatus.OK
    assert "good.md" in entries


# ── 组 3：无界字段 ───────────────────────────────────────────────────


def test_oversized_summary_is_capped(tmp_path: Path) -> None:
    """summary 无长度上限时，攻击者/损坏数据可把最多 10MB 文本送进模型上下文。

    10MB 是 MAX_CACHE_BYTES 那道闸的门槛，单条 summary 可以逼近它。
    """
    huge = "A" * 200_000
    v = _vault(tmp_path, _payload({"n.md": _ok_entry(summary=huge)}))
    entries, _ = load_cache_status(v)
    assert len(entries["n.md"].summary) < 10_000, (
        f"summary 未截断，长度 {len(entries['n.md'].summary)}"
    )


def test_oversized_path_key_is_rejected_or_capped(tmp_path: Path) -> None:
    huge_path = "x" * 100_000 + ".md"
    v = _vault(tmp_path, _payload({huge_path: _ok_entry(), "n.md": _ok_entry()}))
    entries, status = load_cache_status(v)
    assert status is CacheStatus.OK
    assert "n.md" in entries, "正常笔记必须存活"
    assert all(len(p) < 10_000 for p in entries), "超长 path 未被处理"


# ── 组 4：健康态不受影响（防止上面的加固变成过度拦截）──────────────


def test_healthy_cache_unaffected(tmp_path: Path) -> None:
    v = _vault(tmp_path, _payload({
        "技术笔记/gradle.md": {
            "tags": ["gradle", "构建"], "category": "技术笔记",
            "summary": "内存不足排查", "mtime": 1900000000,
            "updated": "2026-08-05", "keywords": ["OOM", "堆内存"],
        },
    }))
    entries, status = load_cache_status(v)
    assert status is CacheStatus.OK
    e = entries["技术笔记/gradle.md"]
    assert e.tags == ("gradle", "构建")
    assert e.keywords == ("OOM", "堆内存")
    assert e.mtime == 1900000000
    assert e.summary == "内存不足排查"


def test_absent_and_version_mismatch_stay_healthy(tmp_path: Path) -> None:
    """硬约束回归：这两个是健康态，不得被加固误判成 CORRUPT（否则全量误报）。"""
    v_absent = tmp_path / "novault"
    v_absent.mkdir()
    assert load_cache_status(v_absent)[1] is CacheStatus.ABSENT

    v = _vault(tmp_path, json.dumps({"_version": CACHE_VERSION + 99, "entries": {}}))
    assert load_cache_status(v)[1] is CacheStatus.VERSION_MISMATCH
