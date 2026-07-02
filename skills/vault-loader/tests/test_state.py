"""_state 单测：TTL 过滤、损坏重置。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scripts._state import (
    MAX_STATE_BYTES,
    load_already_injected,
    load_fulltext_injected,
    save_injected,
    state_path_for_cwd,
)


def test_state_path_for_cwd_hashed(tmp_home: Path) -> None:
    path = state_path_for_cwd(Path("/Users/test/proj/foo"))
    assert str(path).startswith(str(tmp_home / ".claude" / "projects"))
    assert path.name == "vault-loader-state.json"


def test_load_missing_returns_empty(tmp_home: Path) -> None:
    paths = load_already_injected(Path("/no/such"), ttl_hours=24)
    assert paths == set()


def test_save_then_load(tmp_home: Path) -> None:
    cwd = Path("/Users/test/proj/bar")
    save_injected(cwd, ["a.md", "b.md"])

    paths = load_already_injected(cwd, ttl_hours=24)
    assert paths == {"a.md", "b.md"}


def test_ttl_expired_returns_empty(tmp_home: Path) -> None:
    cwd = Path("/Users/test/proj/old")
    save_injected(cwd, ["a.md"])

    p = state_path_for_cwd(cwd)
    data = json.loads(p.read_text())
    data["timestamp"] = time.time() - 25 * 3600  # 25 小时前
    p.write_text(json.dumps(data))

    paths = load_already_injected(cwd, ttl_hours=24)
    assert paths == set()


def test_corrupted_state_returns_empty(tmp_home: Path) -> None:
    cwd = Path("/Users/test/proj/x")
    p = state_path_for_cwd(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ broken")

    paths = load_already_injected(cwd, ttl_hours=24)
    assert paths == set()


def test_huge_state_rejected(tmp_home: Path) -> None:
    cwd = Path("/Users/test/proj/big")
    p = state_path_for_cwd(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * (MAX_STATE_BYTES + 1))

    paths = load_already_injected(cwd, ttl_hours=24)
    assert paths == set()


def test_save_merges_with_existing(tmp_home: Path) -> None:
    cwd = Path("/Users/test/proj/merge")
    save_injected(cwd, ["a.md"])
    save_injected(cwd, ["b.md", "a.md"])  # a 已有

    paths = load_already_injected(cwd, ttl_hours=24)
    assert paths == {"a.md", "b.md"}


# ===== fulltext_paths（全文升级去重） =====

def test_load_fulltext_missing_returns_empty(tmp_home: Path) -> None:
    assert load_fulltext_injected(Path("/no/such"), ttl_hours=24) == set()


def test_old_schema_without_fulltext_treated_as_empty(tmp_home: Path) -> None:
    """旧 schema（无 fulltext_paths）→ load_fulltext_injected 视空集；
    load_already_injected 仍返回全 paths（向后兼容，不破坏既有去重）。"""
    cwd = Path("/Users/test/proj/oldschema")
    save_injected(cwd, ["a.md", "b.md"])  # 2 参旧调用，不写 fulltext_paths
    assert load_already_injected(cwd, ttl_hours=24) == {"a.md", "b.md"}
    assert load_fulltext_injected(cwd, ttl_hours=24) == set()


def test_save_with_fulltext_paths(tmp_home: Path) -> None:
    cwd = Path("/Users/test/proj/ft")
    save_injected(cwd, ["a.md", "b.md"], fulltext_paths=["b.md"])
    assert load_fulltext_injected(cwd, ttl_hours=24) == {"b.md"}
    # fulltext 篇也在 paths（candidate_paths = paths - fulltext 由调用方算）
    assert load_already_injected(cwd, ttl_hours=24) == {"a.md", "b.md"}


def test_fulltext_paths_auto_union_into_paths(tmp_home: Path) -> None:
    """防御：fulltext_paths 即使未在 paths 参数里，也并入 paths（保持 paths ⊇ fulltext 不变量）。"""
    cwd = Path("/Users/test/proj/ftunion")
    save_injected(cwd, ["a.md"], fulltext_paths=["c.md"])
    assert load_already_injected(cwd, ttl_hours=24) == {"a.md", "c.md"}
    assert load_fulltext_injected(cwd, ttl_hours=24) == {"c.md"}


def test_fulltext_merges_across_saves(tmp_home: Path) -> None:
    cwd = Path("/Users/test/proj/ftmerge")
    save_injected(cwd, ["a.md"], fulltext_paths=["a.md"])
    save_injected(cwd, ["b.md"], fulltext_paths=["b.md"])
    assert load_fulltext_injected(cwd, ttl_hours=24) == {"a.md", "b.md"}


def test_fulltext_ttl_expired_returns_empty(tmp_home: Path) -> None:
    cwd = Path("/Users/test/proj/ftttl")
    save_injected(cwd, ["a.md"], fulltext_paths=["a.md"])
    p = state_path_for_cwd(cwd)
    data = json.loads(p.read_text())
    data["timestamp"] = time.time() - 25 * 3600
    p.write_text(json.dumps(data))
    assert load_fulltext_injected(cwd, ttl_hours=24) == set()


def test_fulltext_corrupted_returns_empty(tmp_home: Path) -> None:
    cwd = Path("/Users/test/proj/ftcorrupt")
    p = state_path_for_cwd(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ broken")
    assert load_fulltext_injected(cwd, ttl_hours=24) == set()


def test_fallback_ts_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows Path.home()
    from scripts._state import (load_fallback_ts, save_fallback_ts,
                                fallback_cooldown_expired, save_injected,
                                load_already_injected)
    from pathlib import Path

    cwd = Path(str(tmp_path / "proj"))
    assert load_fallback_ts(cwd) == 0.0
    assert fallback_cooldown_expired(cwd, 24)          # 从未提示 → 允许
    save_fallback_ts(cwd)
    assert load_fallback_ts(cwd) > 0
    assert not fallback_cooldown_expired(cwd, 24)      # 冷却中
    # save_injected 不得丢 fallback_ts
    save_injected(cwd, ["a.md"])
    assert load_fallback_ts(cwd) > 0
    assert load_already_injected(cwd, 24) == {"a.md"}


def test_save_injected_trims_when_oversized(tmp_path, monkeypatch) -> None:
    """写端护栏（评审 R6）：merged 超限时裁剪为「本轮 ∪ fulltext」，防撞 100KB 后
    读端返回空集、去重永久失效。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from scripts._state import save_injected, load_already_injected, MAX_STATE_PATHS
    from pathlib import Path

    cwd = Path(str(tmp_path / "proj"))
    save_injected(cwd, [f"很长的笔记路径/note-{i:04d}.md" for i in range(MAX_STATE_PATHS + 50)])
    save_injected(cwd, ["本轮/new.md"], fulltext_paths=["本轮/full.md"])
    loaded = load_already_injected(cwd, 24)
    assert "本轮/new.md" in loaded and "本轮/full.md" in loaded
    assert len(loaded) <= MAX_STATE_PATHS   # 裁剪生效，未无界增长


def test_save_injected_trims_on_byte_budget(tmp_path, monkeypatch) -> None:
    """F-T2：隔离「字节触发器」分支——条数 < MAX_STATE_PATHS 但序列化 > TRIM_STATE_BYTES
    时也须裁剪。删 line 108 的字节触发器（只留条数）会让此分支回归 → state 涨到读端
    100KB 上限后 load 返空、去重永久失效。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from scripts._state import (save_injected, load_already_injected,
                                MAX_STATE_PATHS, TRIM_STATE_BYTES)
    from pathlib import Path

    cwd = Path(str(tmp_path / "proj"))
    # 300 条 × ~400 字符 ≈ 120KB > 90KB，但 300 « 2000（条数触发器不生效，仅字节触发器）
    long_paths = ["x" * 400 + f"/{i:03d}.md" for i in range(300)]
    assert len(long_paths) < MAX_STATE_PATHS
    save_injected(cwd, long_paths)
    # 第二次写：merged=302 条仍 < 2000 但 >90KB → 只可能由字节触发器裁剪
    save_injected(cwd, ["本轮/new.md"], fulltext_paths=["本轮/full.md"])
    loaded = load_already_injected(cwd, 24)
    assert "本轮/new.md" in loaded and "本轮/full.md" in loaded
    assert len(loaded) < 50                 # 字节分支裁到「本轮 ∪ fulltext」；未裁则 302
    # 未撞读端 100KB 上限（否则 load 返空）——本条已 in loaded 即证
    import json as _json
    assert len(_json.dumps(sorted(loaded)).encode("utf-8")) < TRIM_STATE_BYTES


def test_fallback_cooldown_rearms_after_ttl(tmp_path, monkeypatch) -> None:
    """F-T1：兜底冷却第三态——非零但陈旧的 fallback_ts 超 ttl 后重新允许提示（re-arm）。
    防「>ttl*3600 误写成 <」或漏乘 3600 致提示永久消失（前两态测试无法捕获）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from scripts._state import fallback_cooldown_expired, state_path_for_cwd
    from pathlib import Path

    cwd = Path(str(tmp_path / "proj"))
    p = state_path_for_cwd(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    stale = time.time() - 25 * 3600         # 25h 前（ttl=24 → 已过期）
    p.write_text(json.dumps({"fallback_ts": stale, "timestamp": stale,
                             "paths": [], "fulltext_paths": []}), encoding="utf-8")
    assert fallback_cooldown_expired(cwd, 24)        # 过期 → 重新允许（第三态）
    fresh = time.time() - 3600              # 1h 前（仍在窗口内）
    p.write_text(json.dumps({"fallback_ts": fresh, "timestamp": fresh,
                             "paths": [], "fulltext_paths": []}), encoding="utf-8")
    assert not fallback_cooldown_expired(cwd, 24)    # 仍冷却
