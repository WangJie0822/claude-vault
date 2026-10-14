#!/usr/bin/env python3
"""摘要缺口的查询与补写——供 /summarize-session 收尾流程调用，零额外模型调用。

与 `enrich_summary.py` 的分工同 keywords_gap / enrich_keywords：本脚本由会话里的模型自己读
笔记、写一句摘要，经 `--set-file` 写回；那边每篇付费调一次 haiku，手动 opt-in。

摘要文本不经命令行：会话模型用 Write 工具把 `{"id": ..., "summary": ...}` 写到临时文件，
本脚本只读该文件——命令行双引号会让 shell 先展开反引号与 `$`（spec S08）。

用法：
  summary_gap.py --vault <path> --list [--limit 10] [--max-bytes 60000]
  summary_gap.py --vault <path> --set-file <json>
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _summary import GAP_KINDS, applicable, classify, note_id, reject_reason, sanitize_summary  # noqa: E402
from _summary_store import (MAX_ATTEMPTS, MIN_BODY_BYTES, body_bytes, bump_attempt,  # noqa: E402
                            current_summary, gap_backup_file, load_attempts, safe_note_path,
                            single_instance_holder, write_with_backup)
from keywords_gap import load_entries  # noqa: E402

SET_FILE_MAX_BYTES = 64 * 1024


def _gaps(vault: pathlib.Path, entries: dict) -> list:
    from rebuild_index import _health_check
    return list(_health_check(entries, vault, indexes_written=[]).get("summary_gap", []))


def gap_kind(vault: pathlib.Path, entries: dict, rel: str):
    """单篇缺口判定，与 rebuild_index._health_check 的 summary_gap 同口径（只判目标篇，不通读全库）。"""
    e = entries.get(rel)
    if not isinstance(e, dict) or not applicable(rel, e):
        return None
    k = classify(rel, e, vault=vault)
    return k if k in GAP_KINDS else None


def list_gaps(vault: pathlib.Path, entries: dict, *, limit: int = 10, max_bytes: int = 60000) -> dict:
    gaps = _gaps(vault, entries)
    attempts = load_attempts(vault)
    items = []
    for g in gaps:
        rel = g["path"]
        if attempts.get(rel, 0) >= MAX_ATTEMPTS:
            continue
        b = body_bytes(vault / rel)
        if b < MIN_BODY_BYTES:
            continue
        items.append({"id": note_id(rel), "path": rel, "kind": g["kind"], "bytes": b})
    items.sort(key=lambda x: (x["bytes"], x["path"]))
    picked, used = [], 0
    for it in items:
        if limit and len(picked) >= limit:
            break
        if picked and used + it["bytes"] > max_bytes:
            break
        picked.append(it)
        used += it["bytes"]
    return {"status": "ok", "total": len(gaps), "eligible": len(items), "items": picked}


def set_from_file(vault: pathlib.Path, json_path) -> dict:
    jp = pathlib.Path(json_path)
    try:
        if jp.stat().st_size > SET_FILE_MAX_BYTES:
            return {"status": "error", "reason": "set_file_too_large"}
        data = json.loads(jp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "error", "reason": "set_file_unreadable"}
    if not isinstance(data, dict):
        return {"status": "error", "reason": "set_file_unreadable"}
    nid, raw = data.get("id"), data.get("summary")
    holder = single_instance_holder(vault)
    if holder:   # 后台 enrich_summary 批次在跑：不与它抢写同一批缺口（DO-M4）
        return dict({"status": "skipped", "reason": "batch_running"}, **holder)
    entries = load_entries(vault)
    if entries is None:
        return {"status": "error", "reason": "cache_missing"}
    rel = next((r for r in entries if note_id(r) == nid), None) if isinstance(nid, str) else None
    if rel is None or gap_kind(vault, entries, rel) is None:
        return {"status": "skipped", "reason": "not_a_gap", "id": nid}
    p = safe_note_path(vault, rel, entries)
    if p is None:
        return {"status": "error", "reason": "path_rejected", "path": rel}
    cur = current_summary(p)
    if cur is not None:
        probe = dict(entries[rel])
        probe["summary"], probe["summary_src"] = cur, "fm"
        if classify(rel, probe) not in GAP_KINDS:
            return {"status": "skipped", "reason": "not_a_gap", "path": rel}
    value = sanitize_summary(raw)
    reason = "invalid" if value is None else reject_reason(value, rel, entries[rel])
    if reason:
        bump_attempt(vault, rel)
        return {"status": "rejected", "reason": reason, "path": rel}
    bf = gap_backup_file(vault)
    synced = write_with_backup(vault, rel, p, cur, value, bf)
    if synced is None:
        return {"status": "error", "reason": "write_failed", "path": rel}
    return {"status": "ok", "path": rel, "summary": value, "cache_synced": synced, "backup": str(bf)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="摘要缺口查询与补写（零额外模型调用）")
    ap.add_argument("--vault", required=True, help="知识库根目录")
    action = ap.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true", dest="do_list",
                        help="列出本次要补的缺口（尝试满 3 次、正文不足 200 字节的不列）")
    action.add_argument("--set-file", dest="set_file", metavar="JSON",
                        help='写回一篇：读 {"id": ..., "summary": ...} 的 JSON 文件（摘要文本不走命令行）')
    ap.add_argument("--limit", type=int, default=10, help="--list 至多列出的篇数（默认 10；0 表示不限）")
    ap.add_argument("--max-bytes", type=int, default=60000,
                    help="--list 列出篇目的正文字节总和上限（默认 60000，至少给一篇）")
    args = ap.parse_args(argv)
    vault = pathlib.Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(json.dumps({"status": "error", "reason": f"vault not found: {vault}"}, ensure_ascii=False))
        return 1
    if args.do_list:
        entries = load_entries(vault)
        if entries is None:
            print(json.dumps({"status": "error",
                              "reason": "frontmatter-cache.json 缺失或损坏，请先运行 rebuild_index.py"},
                             ensure_ascii=False))
            return 1
        print(json.dumps(list_gaps(vault, entries, limit=args.limit, max_bytes=args.max_bytes),
                         ensure_ascii=False))
        return 0
    res = set_from_file(vault, args.set_file)
    print(json.dumps(res, ensure_ascii=False))
    return 1 if res["status"] == "error" else 0


if __name__ == "__main__":
    from _exe_search import harden_exe_search
    harden_exe_search()
    try:  # 输出含中文路径：控制台编码不是 UTF-8 时 print 会崩（同 vault-loader _output.py）
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:  # noqa: BLE001 — 被替换成不支持 reconfigure 的流时保持原样
        pass
    sys.exit(main())
