"""跨 hook 维护"本会话已注入笔记"状态，按 cwd 路径 hash 隔离。"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

MAX_STATE_BYTES = 100 * 1024  # 100 KB，超出视为损坏
MAX_STATE_PATHS = 2000  # 写端护栏（评审 R6）：超出即裁剪，防撞读端 100KB 上限后去重永久失效
TRIM_STATE_BYTES = 90 * 1024  # 90 KB，留量提前裁剪，避免踩线抖动


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

    existing: dict = {}
    existing_paths: set[str] = set()
    existing_fulltext: set[str] = set()
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            # isinstance 守卫：旧代码直接 loaded.get(...)，state.json 内容若是数组会抛
            # AttributeError，而它不在下面的 except 元组里，异常一路冒到 hook 顶层兜底、
            # 本轮 state 静默丢失。
            if isinstance(loaded, dict):
                existing = loaded
            old_paths = existing.get("paths", [])
            if isinstance(old_paths, list):
                existing_paths = {x for x in old_paths if isinstance(x, str)}
            old_ft = existing.get("fulltext_paths", [])
            if isinstance(old_ft, list):
                existing_fulltext = {x for x in old_ft if isinstance(x, str)}
        except (json.JSONDecodeError, OSError):
            pass

    new_paths = {x for x in paths if isinstance(x, str)}
    new_ft = {x for x in (fulltext_paths or []) if isinstance(x, str)}

    merged_ft = sorted(existing_fulltext | new_ft)
    merged_paths = sorted(existing_paths | new_paths | new_ft)  # 不变量：paths ⊇ fulltext
    # 读-改-写：保留本函数不认识的键。旧实现从零构造 payload、只显式搬运 fallback_ts，
    # 于是任何其他写入方新增的字段（如诊断冷却 diag_ts）会在下一次成功注入时被静默抹掉
    # ——冷却窗口归零、提示每轮重发。与 save_fallback_ts 的写法对齐。
    payload = dict(existing)
    payload.update({
        "timestamp": time.time(),
        "paths": merged_paths,
        "fulltext_paths": merged_ft,
    })
    payload.setdefault("fallback_ts", 0)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(merged_paths) > MAX_STATE_PATHS or len(serialized.encode("utf-8")) > TRIM_STATE_BYTES:
        # 写端护栏：撞读端 100KB 上限前主动裁剪为「本轮注入 ∪ 全部已知全文」重置
        # （fulltext 集小且最值得保留——防同篇全文重注）
        # OBS-8：此前完全静默——去重集被丢弃，用户会看到已注入过的笔记再次注入却无从知情。
        print(
            f"[vault-loader] state.json 达上限（{len(merged_paths)} paths），"
            f"去重集已裁剪为本轮注入 ∪ 已知全文",
            file=sys.stderr,
        )
        merged_ft = sorted(existing_fulltext | new_ft)
        merged_paths = sorted(new_paths | set(merged_ft))
        payload["paths"] = merged_paths
        payload["fulltext_paths"] = merged_ft
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    p.write_text(serialized, encoding="utf-8")


def load_fallback_ts(cwd: Path) -> float:
    """上次兜底提示时间戳；缺失/损坏/超限 → 0（等效允许提示，fail-open）。"""
    p = state_path_for_cwd(cwd)
    if not p.exists():
        return 0.0
    try:
        if p.stat().st_size > MAX_STATE_BYTES:
            # OBS-8：与 _load_path_field:36-38 的同一条件行为对齐——那边打 stderr、
            # 这边此前静默，同一个超限的 state 文件会得到两种待遇。诊断通道建成后
            # 这两处应一并改走 notify。
            print("[vault-loader] state.json 异常膨胀，兜底冷却按未提示处理", file=sys.stderr)
            return 0.0
        data = json.loads(p.read_text(encoding="utf-8"))
        ts = data.get("fallback_ts", 0) if isinstance(data, dict) else 0
        return float(ts) if isinstance(ts, (int, float)) else 0.0
    except (json.JSONDecodeError, OSError, ValueError):
        return 0.0


def fallback_cooldown_expired(cwd: Path, ttl_hours: int) -> bool:
    """兜底冷却（评审 R4：bigram 使离题中文/日文繁体输入每轮触发兜底）：
    距上次提示超过 ttl_hours 才允许再次提示。复用 state_ttl_hours，不新增 config 键。"""
    return time.time() - load_fallback_ts(cwd) > ttl_hours * 3600


def save_fallback_ts(cwd: Path) -> None:
    """记录本次兜底提示时间。只 setdefault 其余字段——不得刷新 paths 的 timestamp
    （否则会变相续命注入去重 TTL）。"""
    p = state_path_for_cwd(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            pass
    data["fallback_ts"] = time.time()
    data.setdefault("timestamp", 0)
    data.setdefault("paths", [])
    data.setdefault("fulltext_paths", [])
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 诊断冷却（按 code 分别计） ────────────────────────────────────────────────
#
# 为什么不复用 fallback_ts：它是**标量**，且已被「本轮未匹配到强相关笔记」的兜底提示
# 占用（prompt_submit_load 用它 gate、用它写）。共用会让二者互相压制——一条失效诊断
# 能把兜底提示静默满 TTL，反之亦然。诊断按 code 分表存，互不干扰。
#
# 本组函数依赖 save_injected 的「读-改-写保留未知键」语义：否则每次成功注入都会把
# diag_ts 整个抹掉，冷却窗口归零、诊断每轮重发。

def load_diag_ts(cwd: Path) -> dict[str, float]:
    """读诊断冷却表 `{code: ts}`；缺失/损坏/超限 → 空表（等效允许提示，fail-open）。"""
    p = state_path_for_cwd(cwd)
    if not p.exists():
        return {}
    try:
        if p.stat().st_size > MAX_STATE_BYTES:
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        table = data.get("diag_ts", {})
        if not isinstance(table, dict):
            return {}
        return {k: float(v) for k, v in table.items()
                if isinstance(k, str) and isinstance(v, (int, float))}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def diag_cooldown_expired(cwd: Path, code: str, ttl_hours: int) -> bool:
    """该条诊断是否已过冷却窗口（缺失即视为已过期 → 允许提示）。"""
    return time.time() - load_diag_ts(cwd).get(code, 0.0) > ttl_hours * 3600


def save_diag_ts(cwd: Path, codes: list[str]) -> None:
    """记录这些诊断的提示时间。读-改-写，不动其余字段（同 save_fallback_ts）——
    尤其不得刷新 paths 的 timestamp，否则会变相续命注入去重 TTL。"""
    if not codes:
        return
    p = state_path_for_cwd(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            pass
    table = data.get("diag_ts")
    if not isinstance(table, dict):
        table = {}
    now = time.time()
    for code in codes:
        table[code] = now
    data["diag_ts"] = table
    data.setdefault("timestamp", 0)
    data.setdefault("paths", [])
    data.setdefault("fulltext_paths", [])
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # fail-open：冷却写不下去只会让诊断多出现一次，不能因此中断 hook
