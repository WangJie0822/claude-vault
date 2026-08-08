# -*- coding: utf-8 -*-
"""决策面指标落盘。只记录 transcript 原理上拿不到的字段。

**每会话独立文件**是正确性前提，不是风格选择：Windows 上多进程追加同一文件
实测丢 5%~38% 记录（`open(a)` 与 `os.open(O_APPEND)` 皆然，后者大载荷还撕裂），
而丢失的事件看起来恰好等于「vault-loader 那次没触发」——指标系统的数据损坏
会被误读成它要检测的失效。按 session 分文件后实测 160/160 零丢失。

本模块所有函数必须可被调用方用 try/except 完整兜住；模块顶层零 IO。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:                      # 仅供类型标注，运行期不导入（L-PY1）
    # 本模块被每次 UserPromptSubmit 无条件 import，且刻意不依赖任何内部模块
    # （import 失败要能被 fail-open 隔离掉）。放 TYPE_CHECKING 里既补上标注，
    # 又不引入运行期依赖——`from __future__ import annotations` 已让注解全部惰性求值。
    from collections.abc import Collection

    from scripts._decision import Decision      # 与 prompt_submit_load.py 同一写法

SCHEMA = 1
_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_MONTH_RE = re.compile(r"\d{4}-\d{2}")


def metrics_dir(home: Path) -> Path:
    return home / ".claude" / "vault-loader-metrics"


def event_month_dirs(home: Path) -> list[Path]:
    """按月分桶的真实事件目录（升序路径名），供 `load_records`/`prune_expired` 共用。

    **判据用目录名形态**（`\\d{4}-\\d{2}`，与 `write_record` 的落盘规则同源），
    **不是**「排除已知顶层文件名」的黑名单。顶层目前已有 `annotations.jsonl`
    （人工标注）、`near_miss_counts.json`、`nudge_ts.json`、`.salt` 四个辅助文件——
    黑名单式实现要求每新增一个就同步改一遍，是下一个同类缺陷（H1：`analyze_metrics.
    load_records` 曾用 `root.rglob("*.jsonl")` 把 `annotations.jsonl` 当成事件文件
    一起统计进报表）的温床。月份目录本身没有更深层级（`write_record` 直接把
    `<session>.jsonl` 放在月份目录下），故调用方只需 `d.glob("*.jsonl")`。
    """
    root = metrics_dir(home)
    if not root.exists():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and _MONTH_RE.fullmatch(d.name))


def _chmod(p: Path, mode: int) -> None:
    try:
        os.chmod(p, mode)      # Windows 上无实际效果，但无害，不跳过
    except OSError:
        pass


def get_salt(home: Path) -> bytes:
    """每机随机盐。用于 cwd 与关键词的不可逆化。

    **创建必须原子（O_CREAT|O_EXCL），不能写成 `if exists(): read` 再 `write`。**
    那两步之间毫无保护：首次运行时多个 hook 进程并发到达，各自生成不同 salt，
    最后写入者获胜落盘——而先到者已用它那份（此刻已与磁盘不一致的）salt 算完
    kw_h/cwd_h 并落盘，这些 hash 与后续记录**永久对不齐，且零异常零告警**。
    自然并发窗口极窄（实测 120 次首次调用未复现，解释器启动开销天然错开），
    但人为拉宽窗口 100% 复现；多 session 共享同一 ~/.claude 时并非纯理论场景。

    两条降级路径的已知代价，**刻意接受，不要"顺手修好"**：
    1. `.salt` 存在但内容不合法（<16 字节）时**永不自愈**——两轮 O_EXCL 都会
       FileExistsError，从不真正写入，于是每次调用各自生成不同的临时盐，
       该机器的 hash 从此永久互不一致。自愈需要删除并重建，那等于无条件信任
       一个可能正被别的进程使用的文件，风险更大；正解是让用户跑 `--purge`。
    2. `d.mkdir(...)` 在重试循环**之外**，不在任何 try 保护内——mkdir 失败会直接
       抛出，而非降级到临时盐。这与本函数"绝不抛异常"的自我描述不一致，但不影响
       系统级 fail-open：调用方（Task 7 的 stage 块）本身包在 try/except 里。
    """
    d = metrics_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    _chmod(d, 0o700)
    p = d / ".salt"

    for _ in range(2):          # 至多重试一次：独占创建失败必然因为已存在
        if p.exists():
            try:
                raw = p.read_bytes()
                if len(raw) >= 16:
                    return raw
            except OSError:
                pass
        try:
            # O_BINARY 不可省：Windows 上 os.open 默认文本模式，CRT 会把写入的
            # 0x0A 静默翻译成 0x0D 0x0A。salt 是均匀随机 32 字节，含至少一个
            # 0x0A 的概率 = 1-(255/256)^32 ≈ 11.8%（本机 200 次实测 12.5%）。
            # 一旦触发：os.write 仍返回 32（返回值不反映改写），但落盘变 33+ 字节；
            # 首次调用 return 的是内存值（正确），之后**所有**调用读到的都是损坏值，
            # hash 与首次永久对不上且零异常零告警；`len(raw) >= 16` 也拦不住
            # （实测 32 个 0x0A 落盘 64 字节）。POSIX 无此常量，用 getattr 兜。
            # 注：旧实现 p.write_bytes() 走 pathlib 二进制模式，没有这个问题。
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                         | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            continue            # 别的进程抢先建好了，回头读它那份
        except OSError:
            break               # 目录不可写等，落到下面的临时盐
        try:
            raw = secrets.token_bytes(32)
            os.write(fd, raw)
        finally:
            os.close(fd)
        # 不再补 _chmod：os.open 的 mode=0o600 已经够——Windows 上 chmod 本就无效，
        # POSIX 上 umask 只会遮 group/other 位，典型 umask 不碰 owner 位。
        return raw

    # .salt 存在但内容不合法且无法独占重建。宁可本轮用临时盐（与历史对不齐）
    # 也不抛异常——hook 必须 fail-open。
    return secrets.token_bytes(32)


def h(text: str, salt: bytes) -> str:
    """加盐单向摘要，取前 16 位十六进制。

    **不得复用 _state._cwd_hash**：那是无盐 SHA-1，对可枚举的短路径不构成匿名化
    （它只是用来生成文件名，不是隐私控制）。
    """
    return hashlib.sha256(salt + text.encode("utf-8")).hexdigest()[:16]


def write_record(home: Path, session_id: str, record: dict) -> Path:
    """追加一行 JSON 到该会话的独立文件。

    **每会话独立文件缩小了并发损坏面，但没有消除它。** 实测：
      - 跨 session 并发（4 进程各写各的文件，160 条）：零丢失，守恒。
      - **同一 session** 被 6 个进程并发写同一文件（预期 240 条）：三轮实测只落
        166 / 166 / 158 条，丢失 31%~34%，并出现撕裂行（样例 `"_schema": 1, "i": 1}`
        —— 开头的 `{` 没了）。量级与「朴素混写丢 5%~38%」相同。

    刻意不加锁：`msvcrt.locking` 是平台特定的，违反三平台兼容约束；而 metrics 是
    侧信道，写失败已被 `flush()` 的 try/except 兜住，不影响召回与 stdout。
    正常交互下同一 session 的 UPS hook 是串行的（Claude Code 等 hook 返回才继续），
    现实触发路径只有「同一 session 被 resume 到两个终端」这类罕见情形。
    代价由读取端承担：`analyze_metrics.load_records` 计数损坏行并经 stderr 报出，
    不静默——见该函数 docstring。
    """
    safe = _SAFE.sub("_", session_id or "unknown")[:64] or "unknown"
    month = time.strftime("%Y-%m")
    d = metrics_dir(home) / month
    d.mkdir(parents=True, exist_ok=True)
    _chmod(metrics_dir(home), 0o700)
    p = d / f"{safe}.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)
    _chmod(p, 0o600)
    return p


def build_record(decision: "Decision", prompt_keywords: "Collection[str] | None",
                 cwd: Path, *, session_id: str, prompt_id: str, salt: bytes,
                 near_miss_k: int = 10, admitted_k: int = 20) -> dict:
    """把一次 UPS 决策压成一条可落盘记录。纯计算、无 IO。

    隐私边界（不可协商，见 task-6-brief.md）：
    - prompt 全量关键词只存加盐 hash（kw_h）——`_signal_collect.py` 的 CJK 分词是
      重叠滑窗，相邻 bigram 首尾共享一字，可沿重叠链还原原文（实测 25/26 字还原）。
    - 仅 admitted 条目的命中词（hits）存明文——这些词已经是"展示给用户的内容"的
      一部分，不构成额外泄露；且是回放归因"为什么这篇被召回"的必需信息。
    - prompt 原文本身绝不出现在任何字段。
    - excluded 条目只记 path + topical（near_miss），不记 total/hits：两者在
      `_decision.py` 里是未计算的占位值（0.0 / []），补算在真实 Vault 上实测
      +150~200ms，会顶穿 UPS 300ms 预算（见 EntryDecision 上方性能护栏注释）。
    - cwd 只存 hash（cwd_h），不存原路径。

    **`session_id`/`prompt_id` 强制 keyword-only（`*` 分隔）**：二者是相邻的
    同类型字符串位置参数，调用点一旦按位置对调会静默错位——`flush()` 用
    `rec["session"]` 决定落盘**文件名**，对调后即变成「按 prompt_id 命名」，
    架空「每会话独立 `.jsonl`」的整个设计（该设计存在的理由见 `write_record`
    docstring：同文件并发写实测丢 5%~38%）。已实证：位置对调后 84 个既有测试
    全绿，说明类型系统与既有断言都拦不住，只能靠调用形态本身报错。

    **`admitted` 落盘按 `total` 降序截断到 `admitted_k` 条**（默认 20，与
    `near_miss_k` 同族）：真实 Vault 实测单条记录 `admitted` 可达 58~156 条，
    而渲染层只展示 `max_notes=5` 条，未截断的落盘体积（11795~29962 字节）
    超出 README 声称上界（3197 字节）3.7~9.4 倍。截断只影响落盘的**展示样本**，
    不影响统计口径：`n_admitted`（截断前真实条数）与 `arm_counts`（截断前对
    全部 admitted 按 `admit_arm` 统计的计数字典）在截断**之前**算出并单独落盘，
    `analyze_metrics.summarize` 优先读这两个聚合字段，缺失（旧记录）才回退到
    遍历 `admitted` 数组——保证聚合口径不因截断而失真，也不让旧记录在新报表里
    算错或崩溃。
    """
    kws = sorted(prompt_keywords or ())
    near = sorted(decision.excluded, key=lambda ed: -ed.topical)[:near_miss_k]
    admitted_all = decision.admitted
    n_admitted = len(admitted_all)
    # "?" 与 summarize() 里 `a.get("arm") or "?"` 的兜底口径保持一致：admit_arm
    # 理论上恒非空（见 _decision.py 的三种赋值），但防御性地兜底、避免与旧遍历口径分叉。
    arm_counts = Counter(ed.admit_arm or "?" for ed in admitted_all)
    # 显式按 total 降序排序再截断——不依赖 decision.admitted 已被上游 (-total, -mtime)
    # 预排序这一未在本函数签名/契约中声明的隐式前提，函数自身对排序负责。
    admitted_top = sorted(admitted_all, key=lambda ed: -ed.total)[:admitted_k]
    return {
        "_schema": SCHEMA,
        "ts": round(time.time(), 3),
        "session": session_id or "",
        "prompt_id": prompt_id or "",
        "cwd_h": h(str(cwd), salt),
        "kw_h": [h(k, salt) for k in kws],
        "n_kw": len(kws),
        "gate": decision.gate_reason,
        "relaxed": bool(decision.relaxed),
        "admitted": [
            {"path": ed.path, "topical": round(ed.topical, 3),
             "total": round(ed.total, 3), "arm": ed.admit_arm,
             "dedup": ed.dedup, "hits": list(ed.hits)}
            for ed in admitted_top
        ],
        "n_admitted": n_admitted,
        "arm_counts": dict(arm_counts),
        "admitted_k": admitted_k,
        "near_miss": [
            {"path": ed.path, "topical": round(ed.topical, 3)} for ed in near
        ],
        "n_excluded": len(decision.excluded),
        "ft": {"path": decision.fulltext_path or "", "arm": decision.fulltext_arm},
        "near_miss_k": near_miss_k,
    }


NEAR_MISS_MAX_ENTRIES = 500     # 总条目上限，仅在超出时按 count 降序截断


def _counts_path(home: Path) -> Path:
    return metrics_dir(home) / "near_miss_counts.json"


def _counts_lock_path(home: Path) -> Path:
    return metrics_dir(home) / "near_miss_counts.lock"


# 锁重试预算。取 250ms 而非更小值的依据：`bump_near_miss_counts` 只在 `flush()` 里
# 调用，而 `flush()` 恒在 `emit()` **之后**执行（见 `_finish_with_metrics` docstring）
# —— 本轮注入早已送达，这里排队不产生任何用户可感知延迟。故预算可以给得宽裕。
# 实测（barrier 严格同步的 4 进程各 +15，且循环内零间隔立即重新竞争——**远比生产
# 苛刻**，生产每轮 hook 只 +1 一次、且分散在不同 prompt 之间）：
#   50ms  预算 -> 落盘 57/60
#   250ms 预算 -> 落盘 57~60/60（三轮实测 57 / 60 / 59）
# 注意这里**不保证零丢失**，且刻意不再往上加预算：继续调大只是在把代码往这个
# 人为最苛刻的测试上凑，而非往真实竞争强度上凑。丢失量已被独立探针确认**恰好等于
# 取锁失败次数**（3=3 / 0=0 / 1=1），即全部来自「超预算主动放弃」这条设计路径，
# 不含任何正确性缺陷。对照原实现：同一场景只落 3/60（丢 95%）。
# 仍然**有界**：超预算即放弃本次 +1 并返回，绝不无限等——hook 卡死不可接受，
# 而丢一次计数无所谓（阈值是 10，多攒一轮即可）。
_LOCK_BUDGET_SEC = 0.25
_LOCK_SLEEP_SEC = 0.002
# 陈旧锁阈值。持有者崩溃会留下永不释放的锁文件，那会让计数从此**永久停摆**——
# 比原来的丢更新缺陷更糟。超过此秒数即视为无主、强夺。取值远大于临界区耗时
# （~1ms）与重试预算（50ms），正常竞争绝不会被误判为陈旧。
_LOCK_STALE_SEC = 10.0


def _acquire_counts_lock(home: Path) -> int | None:
    """获取 near_miss_counts 的写锁；拿不到返回 None（调用方应放弃本次 +1）。

    用 `O_CREAT|O_EXCL` 而非 `fcntl`/`msvcrt`：前者是可移植的原子创建，本仓库
    `get_salt` 防 TOCTOU 已用同一模式；后两者各自只在 POSIX / Windows 可用，
    而本项目必须三平台兼容。
    """
    p = _counts_lock_path(home)
    deadline = time.time() + _LOCK_BUDGET_SEC
    while True:
        try:
            return os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                           | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            try:
                if time.time() - p.stat().st_mtime > _LOCK_STALE_SEC:
                    p.unlink(missing_ok=True)      # 陈旧锁：持有者已崩溃，强夺
                    continue
            except OSError:
                pass                                # stat/unlink 竞争失败即当作没抢到
            if time.time() >= deadline:
                return None
            time.sleep(_LOCK_SLEEP_SEC)
        except OSError:
            return None                             # 目录不可写等，直接放弃


def load_near_miss_counts(home: Path) -> dict[str, int]:
    p = _counts_path(home)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return {}
        return {k: int(v) for k, v in d.items()
                if isinstance(k, str) and isinstance(v, (int, float))}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def bump_near_miss_counts(home: Path, paths: list[str]) -> None:
    """累加计数；**只在超容量时**按 count 降序截断到 NEAR_MISS_MAX_ENTRIES。

    **不得加「丢弃 count < N」的无条件裁剪。** hook 是一次性短进程，每轮对同一
    path 只 +1；无条件裁剪会让每次调用都从 0 加到 1 再被裁掉、写回空盘，计数
    永远到不了任何阈值，near-miss 提示在生产中完全失效（PoC 实证 15 轮后仍为 {}）。
    低频条目的淘汰交给容量上限：超限时 count 低者自然被挤掉。
    tiebreak 用 path 名，保证裁剪结果确定、测试可复现。

    不在 hook 内扫描全部 metrics 文件——90 天约 15~26 MB（见下方实测区间），
    会顶穿 UPS 的 81ms 余量。

    **读-改-写整段受锁保护（P2 修复）。** 本文件是**跨 session 全局共享**的，
    碰撞面比 `write_record` 大得多（后者只在同一 session resume 到两个终端时才碰）。
    无保护时并发下会**整条丢更新**且完全静默——4 进程各 +15 实测只落 **3**
    （期望 60，丢 95%）。危害不是数字略偏：计数永远攒不到阈值 ⇒ near-miss 提示
    静默失效，与「无条件裁剪」是同一失效表现的不同成因。
    锁拿不到时**放弃本次 +1 并直接返回**——见 `_acquire_counts_lock` 的取舍说明。
    """
    if not paths:
        return
    d = metrics_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    _chmod(d, 0o700)
    fd = _acquire_counts_lock(home)
    if fd is None:
        return                      # 竞争激烈，本次放弃（阈值 10，丢一次可接受）
    try:
        _bump_locked(home, paths)
    finally:
        os.close(fd)
        _counts_lock_path(home).unlink(missing_ok=True)


def _bump_locked(home: Path, paths: list[str]) -> None:
    """读-改-写本体。**调用方必须已持有 `_acquire_counts_lock` 的锁。**"""
    c = load_near_miss_counts(home)
    for p in paths:
        if isinstance(p, str) and p:
            c[p] = c.get(p, 0) + 1
    kept = c
    if len(kept) > NEAR_MISS_MAX_ENTRIES:
        kept = dict(sorted(kept.items(),
                           key=lambda kv: (-kv[1], kv[0]))[:NEAR_MISS_MAX_ENTRIES])
    # 目录创建与 chmod 已由调用方在取锁**之前**完成（取锁本身要求目录存在）
    cp = _counts_path(home)
    cp.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
    _chmod(cp, 0o600)


def _prune_ts_path(home: Path) -> Path:
    return metrics_dir(home) / "prune_ts.json"


def _prune_due(home: Path, ttl_hours: float = 24) -> bool:
    """距上次 `prune_expired` 执行是否已超过 `ttl_hours`（默认一天一次）。

    仿 `nudge_due` 同一写法：文件不存在或损坏一律视为「早已到期」，立即执行一次
    （首次调用必然清理；`.salt` 那类"损坏后永不自愈"的顾虑在这里不适用——写坏了
    至多导致多扫一次目录，不会像 salt 错位那样污染历史 hash）。
    """
    p = _prune_ts_path(home)
    if not p.exists():
        return True
    try:
        last = float(json.loads(p.read_text(encoding="utf-8")).get("ts", 0))
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return True
    return time.time() - last > ttl_hours * 3600


def mark_pruned(home: Path) -> None:
    d = metrics_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    p = _prune_ts_path(home)
    p.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
    _chmod(p, 0o600)


def _nudge_ts_path(home: Path) -> Path:
    return metrics_dir(home) / "nudge_ts.json"


def nudge_due(home: Path, threshold: int = 10, ttl_hours: int = 168) -> list[str]:
    """达阈值且全局冷却已过的 path 列表。

    **全局**而非 per-cwd：`_state.py:21-25` 的隔离是为项目局部诊断设计的，
    而 near-miss 是全局性质；本机 25 个 cwd 目录，per-cwd 会把周期放大 25 倍。
    """
    last = 0.0
    p = _nudge_ts_path(home)
    if p.exists():
        try:
            last = float(json.loads(p.read_text(encoding="utf-8")).get("ts", 0))
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            last = 0.0
    if time.time() - last <= ttl_hours * 3600:
        return []
    return sorted(k for k, v in load_near_miss_counts(home).items() if v >= threshold)


def mark_nudged(home: Path) -> None:
    d = metrics_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    p = _nudge_ts_path(home)
    p.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
    _chmod(p, 0o600)


# ── 进程级缓冲。hook 是一次性短进程，无并发写入。 ──────────────────────────
# 仿 _diagnostics._PENDING：stage 零 IO、只登记；真正写盘由出口处唯一一次 flush 完成。
_PENDING: dict | None = None


def reset() -> None:
    """清空缓冲。生产上一个 hook 进程只跑一次，无需调用；供测试隔离用。"""
    global _PENDING
    _PENDING = None


def stage(record: dict) -> None:
    """登记本轮记录。**零 IO、零副作用**。"""
    global _PENDING
    _PENDING = record


def flush(home: Path, retention_days: int | None = None) -> None:
    """把缓冲写盘。调用方必须用 try/except 兜住（见 prompt_submit_load）。

    `retention_days` 非 None 时额外接线 H2 修复——按「每天最多一次」的频率闸门
    触发 `prune_expired`（`prune_ts.json`，写法照抄 `nudge_ts.json`/`mark_nudged`）：
    距上次执行不足 24 小时直接跳过，**不做任何目录扫描**（`_prune_due` 只读一个
    时间戳文件，`event_month_dirs` 意义上的真正扫描只在到期时才发生）。

    选在这里而不是 hook 主流程接线：`flush()` 恒在 `emit()` 之后调用（见
    `_finish_with_metrics` docstring），用户对这次清理零延迟感知；日频闸门保证
    绝大多数轮次连时间戳文件都不用读第二次的 IO 都省下来。

    整段包在同一个 try/except 内、异常只打 stderr——`flush()` 本身处于 emit() 之后，
    清理失败绝不能让本轮已经完成的注入受影响，也不能让异常从 `flush()` 抛出去
    （调用方虽然也兜了一层，但这里独立兜底更贴近"接线只做增强、不改变既有失败面"
    的既定风格，与 near-miss 提示、metrics 构造等其余可选环节一致）。
    """
    global _PENDING
    if _PENDING:
        rec = _PENDING
        _PENDING = None
        write_record(home, str(rec.get("session") or ""), rec)
        bump_near_miss_counts(home, [nm.get("path", "") for nm in rec.get("near_miss") or []])
    if retention_days is not None:
        try:
            if _prune_due(home):
                prune_expired(home, retention_days)
                mark_pruned(home)
        except Exception as exc:  # noqa: BLE001 — 清理绝不影响主流程
            import sys
            print(f"[vault-loader] metrics 清理失败：{exc}", file=sys.stderr)


def prune_expired(home: Path, retention_days: int) -> int:
    """删除超出保留期的月份目录。返回删除的文件数；**不静默**（对齐 OBS-8）。

    边界：判断用 `d.name < keep_from` 严格小于——**恰好等于**保留期截止月份
    （`keep_from` 当月）的目录会被**保留**，不删除。取舍：按月份粒度裁剪本就是
    近似值（同月内早于/晚于 cutoff 的记录不做日级区分），严格小于让「刚好卡线」
    的月份多留一轮而非提前一天误删，偏保守。
    """
    # shutil 刻意局部 import（L-PY3）：本模块被每次 UserPromptSubmit 无条件
    # import，而 shutil 只在这里和 purge() 用得上。H2 之后 prune_expired 会被
    # flush() 调用，但有「每天至多一次」的频率闸门挡着——绝大多数 UPS 调用根本
    # 走不到这一行，把 shutil 提到模块顶层等于给每次 UPS 都加一份导入成本。
    # （sys 不这样处理：它是内建模块、解释器启动即常驻，局部化零收益，已提到顶层。）
    import shutil
    cutoff = time.time() - retention_days * 86400
    keep_from = time.strftime("%Y-%m", time.localtime(cutoff))
    removed = 0
    for d in event_month_dirs(home):
        if d.name < keep_from:
            removed += len(list(d.glob("*.jsonl")))
            shutil.rmtree(d, ignore_errors=True)
    if removed:
        print(f"[vault-loader] metrics 保留期 {retention_days} 天，"
              f"已清理 {removed} 个过期会话文件", file=sys.stderr)
    return removed


def purge(home: Path) -> int:
    """清空全部指标数据，保留 .salt（否则历史 hash 无法再对齐）。

    实现按「除 .salt 外全清」，**不是**枚举已知文件名：Task 13 会在顶层新增
    near_miss_counts.json / nudge_ts.json，枚举式实现会静默漏清，与 SKILL.md
    和 README 承诺的「一键清空」矛盾，且没有任何测试能拦住。

    **顶层 `annotations.jsonl`（Task 12 的人工标注）也会被清，且必须计入返回值。**
    它与派生数据不同——是用户逐条投入时间标出的 relevant/irrelevant/unsure，
    删了不可重新生成。仍然清，是因为 `--purge` 是隐私兜底、标注里含笔记路径，
    留例外就在「一键清空」上开了洞；但**静默清是缺陷**：CLI 必须在删除前用
    `count_annotations()` 读出条数并明确告知不可恢复（见 Task 11）。

    返回值 = 删除的数据文件（`.jsonl`）总数，含 annotations.jsonl。
    `near_miss_counts.json` / `nudge_ts.json` 这类派生 json 不计入。
    """
    import shutil          # 同 prune_expired，局部导入的理由见那里
    root = metrics_dir(home)
    if not root.exists():
        return 0
    n = 0
    for d in list(root.iterdir()):
        if d.is_dir():
            n += len(list(d.glob("*.jsonl")))
            shutil.rmtree(d, ignore_errors=True)
        elif d.name != ".salt":
            if d.suffix == ".jsonl":
                n += 1          # annotations.jsonl：必须计数，不能静默消失
            d.unlink(missing_ok=True)
    return n


def count_annotations(home: Path) -> int:
    """purge 前供 CLI 读取——人工标注不可重新生成，删除前必须告知条数。

    errors="replace"：与 `prompt_submit_load.py:293` / `_signal_collect.py:88` 同一约定——
    `annotations.jsonl` 若因写入中断（同类风险见 `write_record` docstring 讨论的并发撕裂行）
    留下非 UTF-8 字节，不应让本函数抛 UnicodeDecodeError 崩给调用方。坏字节被替换为 U+FFFD
    而非整行丢弃，该行仍计入返回值——它仍是一条待清理的数据，计入比漏计更安全。
    """
    p = metrics_dir(home) / "annotations.jsonl"
    if not p.exists():
        return 0
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        return sum(1 for line in text.splitlines() if line.strip())
    except OSError:
        return 0
