"""人工标注的读写单点（spec 2026-10-09 mod 注入可见与可控 §6.3）。

原先在 `analyze_metrics.py`，只有 `--review` / `--review-web` 一个写者，用不加锁的追加。
mod 让每个会话的每轮 UPS 都可能写，多进程追加同一文件在 Windows 上实测丢行（见
`_metrics.write_record` 的 docstring），所以「查 id + 追加」整体放进 `lease_lock`。
`analyze_metrics` 从这里再导出同名对象，既有调用点与测试不变。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts import _metrics

VERDICTS = ("relevant", "irrelevant", "unsure")
# 五类标注：near_miss 问「该不该被召回」；两个 admitted_* 问「机械打分打得对不对」；
# llm_pinned 问「LLM 点名点得对不对」；llm_supplement 问「逐轮精排补充得对不对」（2.4.0）。
# 新增一类时同步 analyze_metrics 的 _KIND_HIT / _KIND_BUCKET 与 _session_ops.ANN_KINDS。
KINDS = ("near_miss", "admitted_fulltext", "admitted_list", "llm_pinned", "llm_supplement")
DEFAULT_KIND = "near_miss"   # 旧记录（写于加字段之前）一律归此类


# mod 投递走 UPS 热路径、位于 emit 之前：整批只取一次锁，短超时，超时整批留到下一轮。
# 逐条取默认 2 s 锁时 N=50 实测阻塞 100.4 s（节点 2 P-H1）。hard_stale_after 缩到 10 分钟：
# 写入是毫秒级操作，内容不可解析的陈旧锁不该让每次提问多等到第二天。
HOT_LOCK_TIMEOUT = 0.25
HOT_LOCK_HARD_STALE = 600.0


def annotations_path(home: Path) -> Path:
    return _metrics.metrics_dir(home) / "annotations.jsonl"


def _lock(target: Path, **kw):
    try:
        from context_vault.atomic import lease_lock
    except ImportError:              # legacy 独立布局：退化为无锁，与 _metrics 的 façade 同理
        from contextlib import nullcontext
        return nullcontext()
    return lease_lock(target, **kw)


def _normalize_context_ids(raw) -> list[dict]:
    """把 `[(session, prompt_h), ...]` 归一成可落盘的 dict 列表。

    畸形输入一律降级为空列表，**绝不抛异常**：标注是这套机制里唯一不可再生的数据
    （见 `_metrics.purge` 的定性），为了一个附属字段让整条写入失败是本末倒置。
    落 dict 而非裸元组，是因为 JSON 没有元组、读回来会变成 list，
    带键名才能在日后加字段时不必猜位置。
    """
    out: list[dict] = []
    try:
        for item in (raw or []):
            s, ph = item                      # 长度不为 2 即抛，由下面兜住
            if isinstance(s, str) and isinstance(ph, str) and s and ph:
                out.append({"session": s, "prompt_h": ph})
    except Exception:                          # noqa: BLE001
        return []
    return out


def _existing_ids(p: Path) -> set:
    ids: set = set()
    if not p.exists():
        return ids
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            ids.add(r["id"])
    return ids


def save_annotation(home: Path, path: str, verdict: str, *,
                    kind: str = DEFAULT_KIND, context_ids=None,
                    ann_id=None, ts=None) -> bool:
    """追加一条标注；带 `ann_id` 时同 id 只写一次。返回是否真的写了。

    `kind` 强制 keyword-only：它与 `path`/`verdict` 是相邻同类型字符串，位置传参一旦对调
    会静默错位（同 `_metrics.build_record` 的 session_id/prompt_id）。
    取锁超时抛 `TimeoutError`，由调用方决定「不 ack、下一轮重投」。
    """
    rec = _record(path, verdict, kind, context_ids, ann_id, ts)
    p = annotations_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock(p):
        if ann_id is not None and ann_id in _existing_ids(p):
            return False
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def _record(path, verdict, kind, context_ids, ann_id, ts) -> dict:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict 必须是 {VERDICTS} 之一，收到 {verdict!r}")
    if kind not in KINDS:
        raise ValueError(f"kind 必须是 {KINDS} 之一，收到 {kind!r}")
    rec = {"_schema": _metrics.SCHEMA, "path": path, "verdict": verdict, "kind": kind}
    ids = _normalize_context_ids(context_ids)
    if ids:
        rec["context_ids"] = ids
    if ann_id is not None:
        rec["id"] = ann_id
    if ts is not None:
        rec["ts"] = int(ts)
    return rec


def load_annotations(home: Path) -> dict[tuple[str, str], str]:
    """读取全部人工标注，返回 `{(kind, path): verdict}`。

    **键是 (kind, path) 而非裸 path**：同一篇笔记「作为擦肩候选该不该被召回」
    与「作为已注入内容召回得对不对」是两个独立判断，共用 path 键会让两者互相
    覆盖（`--review` 的去重也会因此漏抽）。缺 `kind` 的旧记录归 `near_miss`。

    **坏行必须计数并经 stderr 报出，不能静默跳过**——与
    `analyze_metrics.load_records` 同一约定，且标注比普通 metrics 更该有可见性：
    `_metrics.purge` 的 docstring 把它定性为「用户逐条投入时间标出的、删了
    不可重新生成」的数据，静默丢一条，用户永远不知道自己的标注没了。

    两类坏数据分别计数：`bad_record`（JSON 解析失败，或解析成功但不是带 path
    的 dict——结构本身不像一条标注）、`bad_verdict`（结构合法但 verdict 不在
    `VERDICTS` 内，例如手工改坏文件、或未来 schema 演进遗留的旧值）。

    两条都带 `ts` 时按时间取新：mod 的标注在点击之后才投递（队列跨会话共享，最长可迟 7 天），
    按追加顺序合并会让它压过之后用 `--review` 做的新判断——所以三个写者都写 `ts`。
    任一条不带 `ts`（写于加字段之前的旧记录）时按文件顺序后写覆盖，与旧行为一致。
    """
    out: dict[tuple[str, str], str] = {}
    seen_ts: dict[tuple[str, str], int] = {}
    p = annotations_path(home)
    if not p.exists():
        return out
    bad_record = bad_verdict = 0
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad_record += 1
                continue
            # path 必须是**非空字符串**：只判真值的话，`{"path": ["a"]}` 会通过，
            # 随后 `out[r["path"]]` 拿 list 当 dict 键直接抛 unhashable TypeError，
            # 整个 --review 未捕获崩溃；`{"path": 123}` 更糟——可哈希、静默存成 int 键，
            # 之后与字符串路径永不相等，标注等于凭空消失（L-SEC-2）。
            # annotations.jsonl 是本机文件，损坏可能来自手工编辑或磁盘异常，
            # 不是可信输入。
            if not (isinstance(r, dict) and isinstance(r.get("path"), str)
                    and r["path"].strip()):
                bad_record += 1
                continue
            verdict = r.get("verdict")
            if verdict not in VERDICTS:
                bad_verdict += 1
                continue
            # kind 缺失=写于加字段之前，一律归 near_miss（已完成的 20 条正属此类）。
            # 未知 kind 与非法 verdict 同等处理，计入 bad_record——手工编辑或未来
            # schema 演进都可能留下它，静默接受会让两类标注的统计悄悄混淆。
            kind = r.get("kind", DEFAULT_KIND)
            if kind not in KINDS:
                bad_record += 1
                continue
            key = (kind, r["path"])
            ts = r.get("ts")
            ts = ts if isinstance(ts, int) and not isinstance(ts, bool) else None
            prev = seen_ts.get(key)
            # 两条都有 ts 时取新的；否则沿用「后写覆盖」（旧记录无 ts，行为不变）。
            if ts is not None and prev is not None and ts < prev:
                continue
            out[key] = verdict
            if ts is not None:
                seen_ts[key] = ts
    except OSError:
        pass
    if bad_record or bad_verdict:
        print(f"[vault-loader] annotations.jsonl 跳过 {bad_record} 行解析/结构异常 + "
              f"{bad_verdict} 行 verdict 非法（该文件不可重新生成，请检查）",
              file=sys.stderr)
    return out


def _session_index(home: Path, session: str):
    """把一个会话的 metrics 记录扫一遍，建 `prompt_h → 记录列表` 与 llm_pinned 并集。

    一批标注里同一会话只扫一次（逐条整读会话文件是 O(N × 文件大小)，节点 2 P-M1）。
    同一句提问会得到相同的 `prompt_h`，所以按它存**全部**记录，不能只留一条。
    会话文件一个都读不到时返回 None。
    """
    stem = _metrics.session_file_stem(session)
    found = False
    by_ph: dict = {}
    llm: set = set()
    supp: set = set()
    for d in reversed(_metrics.event_month_dirs(home)):
        f = d / f"{stem}.jsonl"
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = True
        for line in text.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            lp = r.get("llm_pinned")
            if isinstance(lp, list):
                llm.update(x for x in lp if isinstance(x, str))
            ss = r.get("supp_shown")
            if isinstance(ss, list):
                supp.update(x for x in ss if isinstance(x, str))
            ph = r.get("prompt_h")
            if isinstance(ph, str):
                by_ph.setdefault(ph, []).append(r)
    return {"by_ph": by_ph, "llm": llm, "supp": supp} if found else None


def _verify(index: dict, a: dict) -> bool:
    """按服务端 metrics 记录核对一条标注。

    `prompt_h` + `shown` / `ft` 取自服务端记录（与 web 入口同一约定，CLAUDE.md metrics 组）。
    `llm_pinned` 只在每会话首个对账轮落盘，所以按「该会话任一条记录的 llm_pinned 含此篇」核对。
    """
    path, kind = a["path"], a["kind"]
    if kind == "llm_pinned":
        return path in index["llm"]
    if kind == "llm_supplement":
        # 补充只在投递那一轮出现，而 2.4.0 开发期的旧 supp_only 记录可能没有 prompt_h：与 llm_pinned 同口径，
        # 按「该会话任一条记录的 supp_shown 含此篇」核对，prompt_h 取自请求体、不核对
        return path in index["supp"]
    for r in index["by_ph"].get(a["prompt_h"], ()):
        shown = r.get("shown")
        if not (isinstance(shown, list) and path in shown):
            continue
        ft = r.get("ft") if isinstance(r.get("ft"), dict) else {}
        if (kind == "admitted_fulltext") == (ft.get("path") == path):
            return True
    return False


def apply_annotations(home: Path, anns) -> "tuple[list, list]":
    """mod 投递的标注：核对后整批写入。返回 (acked ids, rejected 条目)。

    整批只取一次锁、只读一次已有 id、一次追加。取锁超时的那批既不 ack 也不拒收——下一轮
    重投（at-least-once，由 id 去重兜住重复）。已有同 id 的照样 ack：另一个会话已经写过了。

    会话 metrics 文件一个都读不到时同样既不 ack 也不拒收：`--purge` 之后与命名空间分裂
    （记录落在另一棵树）在这里分不开，后者是真实标注，判终局会让 mod 永久删掉它。
    purge 之后的标注也不会写入：会话继续时下一轮会话文件已重新出现、却没有旧记录，届时
    判 `no_record`；会话不再继续则由 mod 的 7 天 TTL 丢弃。
    """
    rejected: list = []
    verified: list = []
    indexes: dict = {}
    for a in anns:
        sess = a["session"]
        if sess not in indexes:
            indexes[sess] = _session_index(home, sess)
        idx = indexes[sess]
        if idx is None:
            continue
        if not _verify(idx, a):
            rejected.append({"what": "annotation", "key": a["id"], "reason": "no_record"})
            continue
        verified.append(a)
    if not verified:
        return [], rejected
    # 两个 admitted_* 的 (session, prompt_h) 与核对上的记录逐字相同；llm_pinned 与 llm_supplement 只
    # 核对了 path 在该会话出现过，prompt_h 取自请求体、没有核对（llm_pinned 由 mod 以外的入口产出，节点 2
    # S-L1；llm_supplement 由 mod 面板产出，context_ids 可能指向投递之外的那一轮）
    recs = [_record(a["path"], a["verdict"], a["kind"], [(a["session"], a["prompt_h"])],
                    a["id"], a.get("ts")) for a in verified]
    p = annotations_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _lock(p, timeout=HOT_LOCK_TIMEOUT, hard_stale_after=HOT_LOCK_HARD_STALE):
            existing = _existing_ids(p)
            lines = []
            for rec in recs:
                if rec["id"] not in existing:
                    existing.add(rec["id"])
                    lines.append(json.dumps(rec, ensure_ascii=False) + "\n")
            if lines:
                with open(p, "a", encoding="utf-8") as f:
                    f.write("".join(lines))
    except TimeoutError:
        return [], rejected
    return [a["id"] for a in verified], rejected
