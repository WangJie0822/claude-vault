# -*- coding: utf-8 -*-
"""首轮提炼的候选池：admitted ∪ 项目目录笔记（对已在内存的 cache 按前缀过滤）。

候选**筛选**零 IO；`main_repo_basename` 对 `.git` 做一次 is_file/stat/read，调用方的
`_repo_root(cwd)` 再向上逐级 stat 一次——都只发生在触发提炼的那一轮（1B-L-7：此前写「零 IO」不准）。

为什么不复用 `_signal_collect.collect_signal_a_project_dir`：那是对 vault 目录 `rglob`，
这里 cache 已在内存，前缀过滤 1307 篇 <1ms。索引排除判据与它**共用同一个函数**
（`_signal_collect.is_system_index_name`），不是各写一份。

`main_repo_basename` 只用来算**候选池前缀**，不参与任何落点 ——`_state._repo_root` 刻意不解析
`gitdir:` 里的路径，是因为它决定写入落点；这里解析出来的名字最多让送 LLM 的候选多几篇或少几篇。
"""
from __future__ import annotations

from pathlib import Path

from ._signal_collect import is_system_index_name

_WT_MARKER = "/.git/worktrees/"
_GITFILE_MAX = 4096
SUMMARY_CHARS = 120           # 送进提炼 prompt 的每篇摘要上限（spec §2.1 钉在 prompt 格式上）


def safe_int(v, default: int) -> int:
    """bool / None / 非数字 → default；负数由调用处 max(0, ·) 收敛。

    公开名（1A-L8）：它已是本模块对外契约之一，`prompt_submit_load` 的三个配置读取点都用它。
    ⚠️ `_topic.effective_pin_max` **刻意不用**它——那边 bool 要按 int（`false → 0` 篇，Ruling 9），
    而这里 bool 视为垃圾。"""
    if isinstance(v, bool) or v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def main_repo_basename(root: Path) -> str:
    """worktree 归一：`.git` 是文件且 `gitdir:` 指向 `<main>/.git/worktrees/<x>` ⇒ `<main>` 的目录名。
    其它一切形态（普通检出、submodule gitfile、非 git、读失败）回落 `root.name`。"""
    try:
        g = root / ".git"
        if g.is_file() and g.stat().st_size <= _GITFILE_MAX:
            first = g.read_text(encoding="utf-8", errors="replace").splitlines()
            line = first[0].strip() if first else ""
            if line.startswith("gitdir:"):
                target = line[len("gitdir:"):].strip().replace("\\", "/")
                i = target.find(_WT_MARKER)
                if i > 0:
                    name = Path(target[:i]).name
                    if name not in ("", ".", ".."):
                        return name
    except (OSError, IndexError, ValueError):
        pass
    return root.name


def is_system_index(path: str) -> bool:
    """cache 键（slash 相对路径）形态的索引判据；判据本体在 `_signal_collect.is_system_index_name`。"""
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    parent = parts[-2] if len(parts) >= 2 else ""
    return is_system_index_name(name, parent)


WORKLOG_CATEGORY = "工作日志"


def is_worklog(path: str, entry) -> bool:
    """工作日志判据（spec 2.4.0 W1）：路径首段为 `工作日志`，或 category 含它。

    与 summarize-session `_summary.is_worklog(rel, entry_dict)` 同一判据、两份实现（写端判 cache 原始
    dict，读端判 Entry；两个 skill 不互相导入），由仓库根 `tests/test_worklog_parity.py` 对同一批样本
    对跑钉住一致（先例：`_signal_collect.is_system_index_name`）。首段按 `/` 切、不归一反斜杠，与写端
    逐字一致（cache 键恒为 `/` 分隔）。`entry.category` 判等是给直接构造、没填 `categories` 的 Entry 兜底。
    fail-open：异常判「不是工作日志」——多召回一篇日志，比误排除一篇知识笔记代价小。
    """
    try:
        if isinstance(path, str) and path.split("/", 1)[0] == WORKLOG_CATEGORY:
            return True
        if WORKLOG_CATEGORY in (getattr(entry, "categories", ()) or ()):
            return True
        return getattr(entry, "category", "") == WORKLOG_CATEGORY
    except Exception:                              # noqa: BLE001
        return False


def project_note_prefixes(basename: str, extra_paths) -> list[str]:
    out: list[str] = []
    if basename:
        out.append(f"项目笔记/{basename}/")
    for x in (extra_paths or []):
        s = str(x).strip().replace("\\", "/").strip("/")
        if s:
            out.append(s + "/")
    return out


def build_topic_pool(admitted_paths, active_entries: dict, project_root: Path, extra_paths,
                     top_n: int, project_top_k: int, pool_max: int) -> list[tuple[str, str]]:
    """admitted 前 `top_n`（原序）全保留 + 项目笔记按 mtime 降序补足，去重，封顶 `pool_max`。

    `pool_max` 在 admitted 之后才截：admitted 前 `top_n` 全保留，**被截掉的是项目笔记那一片**——
    本轮 admitted **实际篇数**达到 `pool_max` 时项目笔记一篇都进不了池（看的是实际篇数，不只是
    `top_n`；post-review A-L4 订正了此前「`top_n` ≥ `pool_max` 即截光」的说法）。
    """
    # 上限参数垃圾值一律按 0 处理：候选池是送进 LLM 子进程的内容，配错时只能收窄、不能放宽。
    # 与调用点 `safe_int(…, 配置默认值)` 的默认不同是刻意的：那边收的是 config 值（垃圾 ⇒ 默认），
    # 这边收的是已经过那一层的实参（再垃圾 ⇒ 0，宁可空池）。
    def _proj(p: str) -> tuple[str, str]:
        return p, (getattr(active_entries[p], "summary", "") or "")[:SUMMARY_CHARS]

    try:
        adm = [p for p in list(admitted_paths or [])[: max(0, safe_int(top_n, 0))]
               if isinstance(p, str) and p in active_entries]
        prefixes = project_note_prefixes(main_repo_basename(project_root), extra_paths)
        proj = [p for p in active_entries
                if isinstance(p, str) and any(p.startswith(pf) for pf in prefixes)
                and not is_system_index(p)]

        def _mt(p):
            v = getattr(active_entries[p], "mtime", 0)
            return -int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

        proj.sort(key=_mt)
        proj = proj[: max(0, safe_int(project_top_k, 0))]
        seen: dict[str, None] = dict.fromkeys(adm)
        for p in proj:
            seen.setdefault(p, None)
        pool = list(seen)[: max(0, safe_int(pool_max, 0))]
        return [_proj(p) for p in pool]
    except Exception:                              # noqa: BLE001 — fail-open：退化为纯 admitted
        try:
            if not isinstance(active_entries, dict):
                return []
            return [_proj(p) for p in list(admitted_paths or [])[: max(0, safe_int(top_n, 0))]
                    if isinstance(p, str) and p in active_entries]
        except Exception:                          # noqa: BLE001 — 兜底的兜底：宁可空池
            return []


def count_project_notes(pool, project_root: Path, extra_paths) -> int:
    """池中按**项目前缀归属**的篇数——1B-H-1 的可观测量。

    前缀与 `build_topic_pool` 同一来源（`main_repo_basename` + `project_note_prefixes`）：basename
    归一失败 ⇒ 前缀匹配不到任何池成员 ⇒ 0，正是要照出来的形态。**不按「admitted 之外」数**——
    既是项目笔记又已 admitted 的篇同样证明前缀解析是对的，按补集数会把健康态报成「组件 A 未生效」
    （post-review A-M1 实跑：两篇项目笔记都在池里却记 0）。fail-open：异常一律 0。
    """
    try:
        prefixes = project_note_prefixes(main_repo_basename(project_root), extra_paths)
        return sum(1 for p, _ in (pool or [])
                   if isinstance(p, str) and any(p.startswith(pf) for pf in prefixes)
                   and not is_system_index(p))
    except Exception:                              # noqa: BLE001
        return 0
