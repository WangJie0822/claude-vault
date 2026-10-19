# -*- coding: utf-8 -*-
"""精排子进程的输入构造（spec 2026-10-13 §6.3）：最近 K 轮用户输入、本项目最近工作、宽召回词、
排除集、摘要层与目录层候选、统一编号。

输入构造在精排子进程里跑（没有 UPS 的 300 ms 预算）。父进程（UPS）只用 `exclusion_set`：发起前按同一份
排除集数一次「排除后还有没有候选」（`prompt_submit_load._maybe_spawn`），并且在单独兜底的块里导入它——本模块
导入失败只让本轮不发起精排，粗排照常；`_topic` 也只在用到的函数里导入本模块（2.4.0 full-review A-H1）。
所有进 prompt 的文本都是 **不可信数据**：经 `sanitize_injected_text(keep_newlines=False)` 折成单行、剥控制字符
与 Unicode Cf（含 bidi 控制与零宽字符），并把 «» 《》 换成单引号——编号回显靠 «» 定界
（`_topic.parse_selection`），文件名与摘要里不得再出现它们。本模块不落盘任何东西（F16：K 轮文本与目录只在
内存里，用完即弃）。
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from ._candidate_pool import (is_system_index, is_worklog, main_repo_basename,
                              project_note_prefixes)
from ._fine_config import validate_value
from ._note_paths import safe_rel_note_path
from ._output import sanitize_injected_text
from ._scorer import Signals, build_tag_df, is_archived, tag_key, topical_score
from ._signal_collect import collect_signal_j_prompt_keywords, is_system_index_name

# 只读 transcript 尾部这么多字节（spec §6.3【初值】）；单条 K 轮文本与合计的上限
TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024
K_ITEM_CHARS = 500
K_TOTAL_CHARS = 2000
MAX_WORK_TITLES = 15
MAX_IN_CONTEXT = 30
SUMMARY_LAYER_CHARS = 120
MAX_NAME_CHARS = 200
# transcript user 条目的 promptSource：只认用户手输、排队的用户消息、点选的建议（实测 2.1.272–2.1.296）
USER_SOURCES = ("typed", "queued", "suggestion_accepted")

_BRACKETS = str.maketrans({"«": "'", "»": "'", "《": "'", "》": "'"})
_PROJECT_RE = re.compile(r"\*\*项目\*\*:\s*(.+)$", re.M)
_SECTION_RE = re.compile(r"(?m)^## ")


def clean_text(s, limit: "int | None" = None) -> str:
    """不可信文本进 prompt 前的统一净化：折单行、剥控制与不可见字符、«» 《》 换成单引号、按字符截断。

    不可见字符由 `sanitize_injected_text` 的 Unicode Cf 剥离负责。此前这里另剥一次
    `_session_ops._INVISIBLE_RE`：逐码位枚举（U+0000–U+10FFFF）实测它匹配的 21 个码位全部落在 `_CF_RE`
    的 170 个码位之内，两种写法在全部码位上输出相同，所以去掉了那一步，也去掉了对快照模块的依赖。"""
    if not isinstance(s, str):
        return ""
    t = sanitize_injected_text(s, keep_newlines=False).translate(_BRACKETS).strip()
    return t[:limit] if limit else t


def note_name(rel: str) -> str:
    """cache 键 → 文件名（不含目录段与 `.md`），已净化并按 `MAX_NAME_CHARS` 封顶。

    cache 键不可信（`_note_paths` 的前提）：不封顶时一个超长键会在目录层按全长计费、把预算吃光（其后
    的条目全被计入丢弃），作为项目笔记进摘要层时则把整串送进 prompt。截断而不是丢弃：真实文件名受文件
    系统限制（≤255 字节），截断只会动到异常键；候选靠编号定位、不靠名字，截断后重名无妨。"""
    last = str(rel).replace("\\", "/").split("/")[-1]
    if last.endswith(".md"):
        last = last[:-3]
    return clean_text(last, MAX_NAME_CHARS)


def _mtime(e) -> int:
    v = getattr(e, "mtime", 0)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _strs(v) -> list:
    return [x for x in (v or []) if isinstance(x, str) and x]


# ---------------- transcript ----------------

def config_dir() -> Path:
    """Claude Code 的配置目录：`CLAUDE_CONFIG_DIR` 优先，否则 `~/.claude`。"""
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".claude"


def safe_transcript_path(raw: str, session_id: str, config_dir: Path) -> "Path | None":
    """transcript_path 的纯字符串 + lstat 校验（F12/F13）：绝对路径、不是 UNC、文件名恰为
    `<session_id>.jsonl`、位于 `<配置目录>/projects/` 之下、本身是普通文件（`lstat`，不跟随链接）。
    任一条不成立 → None（调用方记 `transcript_err`）。hook stdin 的这个字段是宿主给的，但仍按不可信处理。"""
    try:
        if not isinstance(raw, str) or not raw or not isinstance(session_id, str) or not session_id:
            return None
        if raw.startswith(("\\\\", "//")) or "\x00" in raw:
            return None
        p = Path(raw)
        if not p.is_absolute() or p.name != f"{session_id}.jsonl":
            return None
        root = os.path.normcase(os.path.abspath(str(Path(config_dir) / "projects")))
        cand = os.path.normcase(os.path.abspath(str(p)))
        if cand == root or os.path.commonpath([root, cand]) != root:
            return None
        if not stat.S_ISREG(os.lstat(cand).st_mode):
            return None
        return p
    except (OSError, ValueError, TypeError):
        return None


def _entry_text(obj) -> "str | None":
    """transcript 一行 → 用户手输文本；不是则 None。字段均未文档化（实测 2.1.272–2.1.296），不认识的一律跳过。"""
    if not isinstance(obj, dict) or obj.get("type") != "user":
        return None
    if obj.get("promptSource") not in USER_SOURCES:
        return None
    if obj.get("isMeta") or obj.get("isSidechain") or obj.get("isCompactSummary"):
        return None
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif (isinstance(content, list) and content
          and all(isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                  for b in content)):
        text = "\n".join(b["text"] for b in content)
    else:
        return None
    if not text.strip() or text.lstrip().startswith("<"):
        return None                          # `<bash-stdout>`、`<task-notification>`、`<command-name>` 等
    return text


def recent_user_prompts(path, k: int, current_prompt: str) -> "tuple[list, str | None]":
    """本会话最近 k 轮用户输入（不含本轮），时间正序；单条截 `K_ITEM_CHARS`、合计 `K_TOTAL_CHARS`
    （从最旧的一端丢）。返回 `(文本列表, 降级码)`：读不到 → `transcript_err`，筛出 0 条 → `transcript_none`。
    只读文件尾部 `TRANSCRIPT_TAIL_BYTES` 字节；窗口不从文件头开始时第一行一定是半行（可能切在 UTF-8
    多字节中间），整行丢弃。"""
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        return [], None
    if path is None:
        return [], "transcript_err"
    try:
        size = os.lstat(path).st_size
        start = max(0, size - TRANSCRIPT_TAIL_BYTES)
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(TRANSCRIPT_TAIL_BYTES)
    except (OSError, ValueError):
        return [], "transcript_err"
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]
    cur = (current_prompt or "").strip()
    texts = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            continue                         # 深层嵌套（`[` × 数十万）抛的是 RecursionError，不是 ValueError
        t = _entry_text(obj)
        if t is None or t.strip() == cur:
            continue
        texts.append(t)
    if not texts:
        return [], "transcript_none"
    out: list = []
    total = 0
    for t in reversed(texts):
        if len(out) >= k:
            break
        c = clean_text(t, K_ITEM_CHARS)
        if not c:
            continue
        if total + len(c) > K_TOTAL_CHARS:
            c = c[: K_TOTAL_CHARS - total]
            if c:
                out.append(c)
            break
        out.append(c)
        total += len(c)
    out.reverse()
    return out, None


# ---------------- 本项目最近工作 ----------------

def worklog_files(vault_path: Path, days: int) -> list:
    """`<vault>/工作日志/` 下 mtime 在 days 天内的 .md，按文件名降序（新的在前）。

    与信号 F（`_signal_collect.collect_signal_f_recent_worklogs`）同源：同一目录、同一组跳过规则
    （索引页、AppleDouble），由 parity 用例钉住。项目过滤不在这里：信号 F 按文件、大小写敏感比对，
    精排的「最近工作」按小节、大小写不敏感比对（spec §6.3 F50）。"""
    d = Path(vault_path) / "工作日志"
    try:
        if not d.is_dir():
            return []
        threshold = time.time() - max(0, int(days)) * 86400
        out = []
        for md in sorted(d.rglob("*.md"), reverse=True):
            # 索引页判据取共享单点 `is_system_index_name`；信号 F 内联的那条表达式与它等价
            if is_system_index_name(md.name, md.parent.name) or md.name.startswith("._"):
                continue
            try:
                if md.stat().st_mtime < threshold:
                    continue
            except OSError:
                continue
            out.append(md)
        return out
    except (OSError, ValueError, TypeError):
        return []


def recent_work_titles(worklog_files, project: str, max_logs: int = 3) -> list:
    """按 `^## ` 切小节，小节内 `**项目**: <名>` 与 project 大小写不敏感相同者取标题（头行最后一个 `|`
    之后的文本）。最多看 `max_logs` 篇「有命中小节」的日志、合计 `MAX_WORK_TITLES` 条。"""
    want = (project or "").strip().casefold()
    try:
        cap_logs = max(0, int(max_logs))
    except (TypeError, ValueError):
        cap_logs = 0
    if not want or cap_logs == 0:
        return []
    titles: list = []
    n_logs = 0
    for md in worklog_files or []:
        if n_logs >= cap_logs or len(titles) >= MAX_WORK_TITLES:
            break
        try:
            text = Path(md).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hit = False
        for sec in _SECTION_RE.split(text)[1:]:
            m = _PROJECT_RE.search(sec)
            if not m or m.group(1).strip().casefold() != want:
                continue
            head = sec.split("\n", 1)[0]
            title = clean_text(head.rsplit("|", 1)[-1] if "|" in head else head, 120)
            if title:
                titles.append(title)
                hit = True
                if len(titles) >= MAX_WORK_TITLES:
                    break
        if hit:
            n_logs += 1
    return titles


# ---------------- 排除集、宽召回词、项目笔记 ----------------

def _tag_items(v) -> list:
    """一组 tag 的原始配置值 → str 列表。单个字符串当一个 tag（不逐字符迭代），其他类型当空。"""
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple, set, frozenset)):
        return [x for x in v if isinstance(x, str)]
    return []


def exclude_tag_set(exclude_note_tags, fine_exclude_tags) -> set:
    """不进精排输入的两组 tag（`exclude_note_tags` 与 `fine_exclude_tags`）按 `tag_key` 口径并成一个集合。
    候选排除（`exclusion_set`）与「已在上下文」的文件名（`build_fine_input`）共用这一处，两条外发通路同口径。"""
    return {k for k in (tag_key(t) for t in _tag_items(exclude_note_tags) + _tag_items(fine_exclude_tags))
            if k}


def tag_blocked(entry, tags: set) -> bool:
    """精排两条外发通路共用的 tag 判定：命中排除 tag（`is_archived`），或排除 tag 非空而这篇的 tag 集合
    判不全（`Entry.tags_unknown`：原始 tags 不是 str / list、list 里有非 str 项、被条数或长度上限截掉过）。
    后者 fail-closed：被丢掉的那个 tag 可能正是 private（2.4.0 full-review S-H1）。排除 tag 为空时没有
    需要判定的东西，判不全也照常外发。粗排不走这里，`tags_unknown` 不改变粗排的行为。"""
    if not tags:
        return False
    return bool(getattr(entry, "tags_unknown", False)) or is_archived(entry, tags)


def exclusion_set(entries, *, exclude_note_tags, fine_exclude_tags, injected, excluded) -> set:
    """摘要层与目录层共用的排除集（spec §6.3）：不安全的 cache 键、归档与 `exclude_note_tags`、
    `fine_exclude_tags`（D3）及 tag 判不全的笔记（`tag_blocked`）、系统索引、工作日志（W1）、本会话排除与已注入。"""
    tags = exclude_tag_set(exclude_note_tags, fine_exclude_tags)
    skip = set(_strs(injected)) | set(_strs(excluded))
    out = set()
    for rel, e in (entries or {}).items():
        if (not isinstance(rel, str) or not safe_rel_note_path(rel) or rel in skip
                or tag_blocked(e, tags) or is_system_index(rel) or is_worklog(rel, e)):
            out.add(rel)
    return out


def wide_recall_words(texts, jcfg: dict) -> list:
    """宽召回词 = 各段文本分别过信号 J（与 UPS 同参数）后的并集，排序后返回。"""
    j = dict(jcfg or {})
    words: set = set()
    for t in texts or []:
        if not isinstance(t, str) or not t.strip():
            continue
        try:
            words |= collect_signal_j_prompt_keywords(
                t, bool(j.get("strip_slash_command", True)), bool(j.get("split_english_token", True)),
                int(j.get("en_subtoken_min", 4)), split_cjk_bigram=bool(j.get("split_cjk_bigram", True)),
                max_keywords=j.get("max_prompt_keywords", 30))
        except (TypeError, ValueError):
            continue
    return sorted(words)


def project_rels(entries: dict, project_root: Path, extra_paths) -> list:
    """本项目的笔记（与 `_candidate_pool.build_topic_pool` 同一前缀口径），按 mtime 降序。"""
    try:
        prefixes = project_note_prefixes(main_repo_basename(Path(project_root)), extra_paths)
    except Exception:  # noqa: BLE001
        return []
    rels = [p for p in (entries or {}) if isinstance(p, str)
            and any(p.startswith(pf) for pf in prefixes) and not is_system_index(p)]
    rels.sort(key=lambda p: (-_mtime(entries[p]), p))
    return rels


# ---------------- 候选 ----------------

@dataclass
class Candidate:
    n: int
    rel: str
    name: str
    summary: "str | None"


@dataclass
class FineInput:
    candidates: list = field(default_factory=list)
    n_summary_layer: int = 0
    n_catalog: int = 0
    n_catalog_dropped: int = 0
    in_context: list = field(default_factory=list)
    work_titles: list = field(default_factory=list)
    k_texts: list = field(default_factory=list)
    degraded: list = field(default_factory=list)


def _cint(cfg: dict, key: str, default: int) -> int:
    v = cfg.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    try:
        return max(0, int(v))
    except (ValueError, OverflowError):
        return default


def build_fine_input(entries: dict, *, words, exclude: set, project_rels: list, cfg: dict,
                     injected: list, work_titles: list, k_texts: list, degraded: list) -> FineInput:
    """摘要层 + 目录层候选（spec §6.3）。

    - 摘要层：宽召回 topical>0（只用宽召回词，不含信号 K）按 topical 降序前 `top_n`，并上项目笔记按
      mtime 前 `project_top_k`，封顶 `pool_max`；每篇附摘要前 `SUMMARY_LAYER_CHARS` 字。
    - 目录层（`catalog` 开时）：排除集之外、不在摘要层的全部文件名，先放宽召回 topical>0 的（按 topical
      降序），再放其余（按 mtime 降序），按「文件名字数 + 1」累加到 `catalog_max_chars`，第一个放不下的
      及其后全部计入 `n_catalog_dropped`（F63：超预算时保住老而相关的那些）。
    - 统一编号：摘要层在前、目录层在后，1-based 连续。
    """
    cfg = cfg or {}
    pool = {r: e for r, e in (entries or {}).items()
            if isinstance(r, str) and r not in (exclude or set()) and note_name(r)}
    weights = {"prompt_tag_hit": 4, "prompt_summary_hit": 2, "prompt_keyword_hit": 5}
    for k, v in (cfg.get("weights") or {}).items():
        if k in weights and isinstance(v, (int, float)) and not isinstance(v, bool):
            weights[k] = v
    weights["session_topic_hit"] = 0                    # 宽召回不含信号 K（spec §6.3）
    topical: dict = {}
    wset = {w for w in (words or []) if isinstance(w, str) and w}
    if wset and pool:
        sig = Signals(prompt_keywords=wset)
        tag_df = build_tag_df(pool) if cfg.get("use_tag_idf", True) else None
        floor = cfg.get("tag_idf_floor", 0.5)
        if not isinstance(floor, (int, float)) or isinstance(floor, bool):
            floor = 0.5
        use_kw = bool(cfg.get("use_keywords", True))
        for r, e in pool.items():
            t = topical_score(e, sig, weights, use_kw, tag_df=tag_df, n_docs=len(pool), tag_idf_floor=floor)
            if t > 0:
                topical[r] = t
    ranked = sorted(topical, key=lambda r: (-topical[r], -_mtime(pool[r]), r))

    summary: list = []
    seen: set = set()
    proj = [p for p in (project_rels or []) if p in pool][: _cint(cfg, "project_top_k", 20)]
    for r in ranked[: _cint(cfg, "top_n", 30)] + proj:
        if r not in seen:
            seen.add(r)
            summary.append(r)
    summary = summary[: _cint(cfg, "pool_max", 50)]

    catalog: list = []
    dropped = 0
    if cfg.get("catalog", True):
        budget = _cint(cfg, "catalog_max_chars", 80000)
        sset = set(summary)
        rest = [r for r in ranked if r not in sset]
        rest_set = set(rest)
        others = sorted((r for r in pool if r not in sset and r not in rest_set),
                        key=lambda r: (-_mtime(pool[r]), r))
        used = 0
        full = False
        for r in rest + others:
            cost = len(note_name(r)) + 1
            if not full and used + cost <= budget:
                catalog.append(r)
                used += cost
            else:
                full = True
                dropped += 1

    cands: list = []
    n = 0
    for r in summary:
        n += 1
        cands.append(Candidate(n=n, rel=r, name=note_name(r),
                               summary=clean_text(getattr(pool[r], "summary", "") or "", SUMMARY_LAYER_CHARS)))
    for r in catalog:
        n += 1
        cands.append(Candidate(n=n, rel=r, name=note_name(r), summary=None))
    # 「已在上下文」也是外发通路：带 `fine_exclude_tags` / `exclude_note_tags` 的已注入笔记、tag 判不全的
    # 已注入笔记，文件名同样不送（`exclusion_set` 只管候选）。`fine_exclude_tags` 与候选那条（`_topic` 里
    # 调 `exclusion_set` 处）同样经 `validate_value` 归一：缺省或类型不对回落默认 `["private"]`，单个字符串
    # 当一个 tag。entries 里查不到的路径取不到 tag，保留文件名（现状）
    skip_tags = exclude_tag_set(cfg.get("exclude_note_tags"),
                                validate_value("fine_exclude_tags", cfg.get("fine_exclude_tags"))[1])
    visible = [p for p in _strs(injected)
               if not (p in (entries or {}) and tag_blocked(entries[p], skip_tags))]
    in_ctx = [x for x in (note_name(p) for p in visible) if x][-MAX_IN_CONTEXT:]
    return FineInput(candidates=cands, n_summary_layer=len(summary), n_catalog=len(catalog),
                     n_catalog_dropped=dropped, in_context=in_ctx, work_titles=list(work_titles or []),
                     k_texts=list(k_texts or []), degraded=list(degraded or []))
