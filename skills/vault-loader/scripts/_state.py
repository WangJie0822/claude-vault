"""跨 hook 维护"本会话已注入笔记"状态，按 cwd 路径 hash 隔离。"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
# 只有**确实像插件根**才插入 sys.path。legacy 独立布局
# （`~/.claude/skills/<skill>/scripts/`）下 parents[3] 正是 `~/.claude` 本身——
# 把一个多插件共享、可被任意工具写入的目录放到 sys.path[0]，等于让任何能在那里
# 落一个 `context_vault/__init__.py` 的东西在每次 hook 进程内取得代码执行。
# 判据不成立时跳过，交给下面的 ImportError façade 兜底。
_LOOKS_LIKE_PLUGIN_ROOT = (_PLUGIN_ROOT / "context_vault" / "runtime.py").is_file()
if _LOOKS_LIKE_PLUGIN_ROOT and str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

try:
    from context_vault.atomic import update_json
    from context_vault.paths import canonical_config, context_home, use_canonical_namespace
except ImportError:  # compatibility façade for isolated legacy script copies
    def context_home(home: Path | None = None) -> Path:
        return (home or Path.home()) / ".context-vault"

    def canonical_config(home: Path | None = None) -> Path:
        return context_home(home) / "config.json"

    def use_canonical_namespace(home: Path | None = None) -> bool:
        # façade 降级：拿不到 context_vault 时保守走 legacy 布局，
        # 绝不把既有用户的数据切到一个空命名空间。
        return False

    def update_json(path: Path, mutate, *, max_bytes=None) -> dict:
        current = {}
        try:
            if path.exists() and (max_bytes is None or path.stat().st_size <= max_bytes):
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = loaded
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        updated = mutate(current)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        return updated

MAX_STATE_BYTES = 100 * 1024  # 100 KB，超出视为损坏
MAX_STATE_PATHS = 2000  # 写端护栏（评审 R6）：超出即裁剪，防撞读端 100KB 上限后去重永久失效
TRIM_STATE_BYTES = 90 * 1024  # 90 KB，留量提前裁剪，避免踩线抖动

_RUNTIME = "legacy"


def _safe_component(value: str, fallback: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in value)
    return (cleaned[:80] or fallback)


def configure_context(runtime: str, session_id: str = "") -> None:
    """Select the current hook process' runtime state namespace.

    ⚠️ **注入去重的 state 按 cwd 隔离，不按 session。** 这是有意的：

    - 隔离要解决的问题是「两个 runtime 互相踩踏」，runtime 一层就够了；
    - 再切一层 session 会让跨会话去重失效——同一批笔记每开一次新会话就被重新
      全文注入一遍，而这正是去重机制存在的理由（0.9.x 的行为就是按 cwd）；
    - 每会话一个目录还会单调增长，且没有任何清理路径。

    `session_id` 参数保留（调用点仍在传），但不再参与路径构成。
    """
    global _RUNTIME
    # Preserve 0.9.x state paths until a Claude user explicitly migrates.
    if runtime in {"legacy", "unknown"} or (
            runtime == "claude" and not use_canonical_namespace()):
        _RUNTIME = "legacy"
    else:
        # 白名单之外一律回落 legacy，与 `_metrics.configure_context` 同一口径：
        # 放行任意字符串会凭空造出 `state/<乱码>/` 这样的孤儿命名空间，写进去的
        # 去重集此后无人读取——现象与「去重没生效」完全一样，却查不到原因。
        # `_safe_component` 作为二次清洗保留：白名单日后扩容时不至于漏掉路径净化。
        _RUNTIME = (_safe_component(runtime, "legacy")
                    if runtime in {"claude", "codex"} else "legacy")


def _cwd_hash(cwd: Path) -> str:
    """对 cwd 绝对路径取短 hash，用于隔离不同项目的 state。"""
    canonical = str(cwd.resolve() if cwd.exists() else cwd.absolute())
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def state_path_for_cwd(cwd: Path) -> Path:
    """返回该 cwd 对应的 state.json 路径。"""
    if _RUNTIME != "legacy":
        return context_home() / "state" / _RUNTIME / f"{_cwd_hash(cwd)}.json"
    return (
        Path.home() / ".claude" / "projects" / _cwd_hash(cwd) / "vault-loader-state.json"
    )


def diagnostics_path_for_cwd(cwd: Path) -> Path:
    if _RUNTIME != "legacy":
        return (context_home() / "state" / _RUNTIME / "projects" /
                f"{_cwd_hash(cwd)}.json")
    return state_path_for_cwd(cwd)


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
    new_paths = {x for x in paths if isinstance(x, str)}
    new_ft = {x for x in (fulltext_paths or []) if isinstance(x, str)}
    p = state_path_for_cwd(cwd)

    def mutate(existing: dict) -> dict:
        old_paths = existing.get("paths", [])
        old_ft = existing.get("fulltext_paths", [])
        existing_paths = {x for x in old_paths if isinstance(x, str)} if isinstance(old_paths, list) else set()
        existing_fulltext = {x for x in old_ft if isinstance(x, str)} if isinstance(old_ft, list) else set()
        merged_ft = sorted(existing_fulltext | new_ft)
        merged_paths = sorted(existing_paths | new_paths | new_ft)
        payload = dict(existing)
        payload.update({"timestamp": time.time(), "paths": merged_paths,
                        "fulltext_paths": merged_ft})
        payload.setdefault("fallback_ts", 0)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(merged_paths) > MAX_STATE_PATHS or len(serialized.encode("utf-8")) > TRIM_STATE_BYTES:
            print(f"[vault-loader] state.json 达上限（{len(merged_paths)} paths），"
                  "去重集已裁剪为本轮注入 ∪ 已知全文", file=sys.stderr)
            payload["paths"] = sorted(new_paths | set(merged_ft))
        return payload

    update_json(p, mutate, max_bytes=MAX_STATE_BYTES)


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

    def mutate(data: dict) -> dict:
        data["fallback_ts"] = time.time()
        data.setdefault("timestamp", 0)
        data.setdefault("paths", [])
        data.setdefault("fulltext_paths", [])
        return data

    update_json(p, mutate, max_bytes=MAX_STATE_BYTES)


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
    p = diagnostics_path_for_cwd(cwd)
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
    p = diagnostics_path_for_cwd(cwd)

    def mutate(data: dict) -> dict:
        table = data.get("diag_ts")
        if not isinstance(table, dict):
            table = {}
        now = time.time()
        for code in codes:
            table[code] = now
        data["diag_ts"] = table
        if _RUNTIME == "legacy":
            data.setdefault("timestamp", 0)
            data.setdefault("paths", [])
            data.setdefault("fulltext_paths", [])
        return data
    try:
        update_json(p, mutate, max_bytes=MAX_STATE_BYTES)
    except (OSError, TimeoutError):
        pass  # fail-open：冷却写不下去只会让诊断多出现一次，不能因此中断 hook
