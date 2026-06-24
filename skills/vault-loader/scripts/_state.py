"""跨 hook 维护"本会话已注入笔记"状态，按 cwd 路径 hash 隔离。"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

MAX_STATE_BYTES = 100 * 1024  # 100 KB，超出视为损坏


def _cwd_hash(cwd: Path) -> str:
    """对 cwd 绝对路径取短 hash，用于隔离不同项目的 state。"""
    canonical = str(cwd.resolve() if cwd.exists() else cwd.absolute())
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def state_path_for_cwd(cwd: Path) -> Path:
    """返回该 cwd 对应的 state.json 路径。"""
    return (
        Path.home() / ".claude" / "projects" / _cwd_hash(cwd) / "vault-loader-state.json"
    )


def _load_path_field(cwd: Path, ttl_hours: int, field: str) -> set[str]:
    """读 state.json 中某个 path 列表字段（paths / fulltext_paths）。
    TTL 过期 / 损坏 / 缺失 / 字段不存在 → 空集合。"""
    p = state_path_for_cwd(cwd)
    if not p.exists():
        return set()

    try:
        if p.stat().st_size > MAX_STATE_BYTES:
            print(f"[vault-loader] state.json 异常膨胀，重置", file=sys.stderr)
            return set()

        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return set()

        ts = data.get("timestamp", 0)
        if not isinstance(ts, (int, float)) or time.time() - ts > ttl_hours * 3600:
            return set()

        paths = data.get(field, [])
        if not isinstance(paths, list):
            return set()
        return {p for p in paths if isinstance(p, str)}

    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"[vault-loader] state.json 加载失败：{exc}", file=sys.stderr)
        return set()


def load_already_injected(cwd: Path, ttl_hours: int) -> set[str]:
    """加载已注入 paths（候选 ∪ 全文）。TTL 过期 / 损坏 / 缺失 → 空集合。"""
    return _load_path_field(cwd, ttl_hours, "paths")


def load_fulltext_injected(cwd: Path, ttl_hours: int) -> set[str]:
    """加载已以全文注入过的 paths 子集（旧 schema 无此字段 → 空集）。
    供全文升级去重：candidate_paths = load_already_injected - load_fulltext_injected。"""
    return _load_path_field(cwd, ttl_hours, "fulltext_paths")


def save_injected(
    cwd: Path, paths: list[str], fulltext_paths: list[str] | None = None
) -> None:
    """合并写入 paths 与 fulltext_paths。已有 state 合并；损坏 / 缺失视为新写入。

    - fulltext_paths（默认 None=不新增全文）：本轮以全文注入的 path 子集。
    - 不变量：fulltext_paths 自动并入 paths（paths ⊇ fulltext_paths）。
    - 2 参旧调用（SessionStart）零改动：fulltext_paths=None → 既有 fulltext_paths 原样保留。"""
    p = state_path_for_cwd(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)

    existing_paths: set[str] = set()
    existing_fulltext: set[str] = set()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            old_paths = data.get("paths", [])
            if isinstance(old_paths, list):
                existing_paths = {x for x in old_paths if isinstance(x, str)}
            old_ft = data.get("fulltext_paths", [])
            if isinstance(old_ft, list):
                existing_fulltext = {x for x in old_ft if isinstance(x, str)}
        except (json.JSONDecodeError, OSError):
            pass

    new_paths = {x for x in paths if isinstance(x, str)}
    new_ft = {x for x in (fulltext_paths or []) if isinstance(x, str)}

    merged_ft = sorted(existing_fulltext | new_ft)
    merged_paths = sorted(existing_paths | new_paths | new_ft)  # 不变量：paths ⊇ fulltext
    payload = {
        "timestamp": time.time(),
        "paths": merged_paths,
        "fulltext_paths": merged_ft,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
