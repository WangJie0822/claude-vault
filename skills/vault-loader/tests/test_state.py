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
