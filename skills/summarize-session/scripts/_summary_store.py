"""摘要写盘、cache 同步、备份 / 回滚、尝试计数与单实例锁。判据见 `_summary.py`。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from _frontmatter_field import (get_field_lines, replace_field_lines, set_scalar_field,
                                split_frontmatter)
from _fs import (_acquire_lock, _pid_alive, _read_lock_pid, _release_lock, _replace_with_retry,
                 atomic_write_text)
from _summary import applicable

CACHE_LOCK_TIMEOUT = 10


def safe_note_path(vault: Path, rel: str, entries: dict, *, include_archived: bool = False):
    """写入目标白名单：cache 键 ∧ applicable ∧ `.md` ∧ 无点开头段 ∧ 无 `:` ∧ 末段非链接 ∧ 在 vault 内。"""
    if not isinstance(rel, str) or rel not in entries or not isinstance(entries[rel], dict):
        return None
    if not rel.lower().endswith(".md") or ":" in rel or "\\" in rel:
        return None
    if any((not seg) or seg.startswith(".") or seg == ".." for seg in rel.split("/")):
        return None
    if not applicable(rel, entries[rel], include_archived):
        return None
    p = Path(vault) / rel
    try:
        if p.is_symlink():
            return None
        rp, vr = p.resolve(), Path(vault).resolve()
    except OSError:
        return None
    if vr not in rp.parents or not rp.is_file():
        return None
    return rp


def read_text_exact(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def write_text_exact(path: Path, text: str) -> None:
    """按字节写入（不做行尾转换），tempfile + 带占用重试的 replace。"""
    fd, tmp = tempfile.mkstemp(prefix=".atomic_", suffix=".tmp", dir=str(Path(path).parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        _replace_with_retry(tmp, str(path))
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def current_summary(path: Path):
    from rebuild_index import parse_frontmatter
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return None
    v = parse_frontmatter(head).get("summary")
    return v if isinstance(v, str) else None


def field_lines(path: Path, key: str):
    try:
        return get_field_lines(read_text_exact(path), key)
    except (OSError, UnicodeDecodeError):
        return None


def _write_transformed(path: Path, transform) -> bool:
    try:
        text = read_text_exact(path)
    except (OSError, UnicodeDecodeError):
        return False
    new = transform(text)
    if new is None:
        return False
    if new == text:
        return True
    try:
        write_text_exact(path, new)
    except OSError:
        return False
    return True


def write_summary(path: Path, value) -> bool:
    return _write_transformed(path, lambda t: set_scalar_field(t, "summary", value))


def write_field_lines(path: Path, key: str, lines: list) -> bool:
    return _write_transformed(path, lambda t: replace_field_lines(t, key, lines))


def write_with_backup(vault: Path, rel: str, p: Path, cur, value: str, bf: Path):
    """备份 → 写入 → 同步 cache 的唯一实现（summary_gap --set-file、enrich_summary --apply / --apply-proposals 共用）。
    返回 cache_synced（bool）；写入失败返回 None，原文不变。"""
    append_backup(bf, backup_record(rel, cur, field_lines(p, "summary") or [], value, bf.name))
    if not write_summary(p, value):
        return None
    return sync_cache_summary(vault, rel, value)


def _h1_fallback(vault: Path, rel: str):
    from rebuild_index import _read_entry
    try:
        fresh = _read_entry(Path(vault), rel, 0)
    except (OSError, UnicodeDecodeError):
        return None
    return fresh.get("summary") if fresh.get("summary_src") == "h1" else None


def sync_cache_summary(vault: Path, rel: str, value) -> bool:
    """写回后同步 cache 对应 entry：rebuild 按秒级 mtime 判增量，同秒会跳过重读（spec §3.4）。"""
    cp = Path(vault) / ".meta" / "frontmatter-cache.json"
    if not cp.is_file():
        return False
    lock = str(cp) + ".lock"
    if not _acquire_lock(lock, timeout=CACHE_LOCK_TIMEOUT):
        return False
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        entry = data.get("entries", {}).get(rel)
        if not isinstance(entry, dict):
            return False
        if value is None:
            entry.pop("summary", None)
            entry.pop("summary_src", None)
            fallback = _h1_fallback(vault, rel)   # 与 rebuild 同口径：无 summary 时以正文 H1 兜底
            if fallback:
                entry["summary"], entry["summary_src"] = fallback, "h1"
        else:
            entry["summary"] = value
            entry["summary_src"] = "fm"
        try:
            entry["mtime"] = int((Path(vault) / rel).stat().st_mtime)
        except OSError:
            pass
        atomic_write_text(str(cp), json.dumps(data, ensure_ascii=False))
        return True
    except (OSError, ValueError):
        return False
    finally:
        _release_lock(lock)


MAX_ATTEMPTS = 3
MIN_BODY_BYTES = 200
KEEP_BACKUPS = 20
BACKUP_DIRNAME = "summary-backups"
ATTEMPTS_FILE = "summary-attempts.json"
LOCK_FILE = "enrich-summary.lock"
_STAMP_NAME_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?\.jsonl$")
_BACKUP_NAME_RE = re.compile(r"^(\d{8}-\d{6}(-\d+)?|gap-\d{8})\.jsonl$")
_SUMMARY_LINE_RE = re.compile(r"^summary\s*:")


def body_bytes(path: Path) -> int:
    """frontmatter 之后的字节数（近空判据用，边界判据同 _frontmatter_field）；读失败 -1。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return -1
    parts = split_frontmatter(data.decode("utf-8", "replace"))
    if parts is None:
        return len(data)
    rest = parts[2].split(chr(10), 1)
    return len(rest[1].encode("utf-8")) if len(rest) > 1 else 0


def new_stamp_file(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i in range(100):
        p = directory / (f"{stamp}.jsonl" if i == 0 else f"{stamp}-{i}.jsonl")
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        return p
    raise OSError("cannot allocate a stamped file")


def backup_dir(vault: Path) -> Path:
    d = Path(vault) / ".meta" / BACKUP_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    if d.resolve() != Path(vault).resolve() / ".meta" / BACKUP_DIRNAME:
        raise OSError("summary backup dir is redirected")
    return d


def new_backup_file(vault: Path) -> Path:
    return new_stamp_file(backup_dir(vault))


def gap_backup_file(vault: Path) -> Path:
    return backup_dir(vault) / ("gap-" + time.strftime("%Y%m%d") + ".jsonl")


def append_backup(path: Path, rec: dict) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def backup_record(rel: str, old, old_lines: list, new, batch: str) -> dict:
    return {"path": rel, "old": old, "old_lines": list(old_lines or []), "new": new,
            "ts": time.time(), "batch": batch}


def _name_time(name: str):
    m = re.match(r"^(\d{8}-\d{6})", name)
    if m:
        return time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S"))
    m = re.match(r"^gap-(\d{8})", name)
    if m:
        return time.mktime(time.strptime(m.group(1), "%Y%m%d"))
    return None


def prune_backups(vault: Path, *, keep: int = KEEP_BACKUPS, protect=(), is_git=None) -> int:
    """只在 vault 是 git 仓库时清理；只删名字合规的普通文件；不删受保护与未来时间戳的。"""
    if is_git is None:
        from git_commit_vault import is_git_repo
        is_git = is_git_repo(str(vault))
    if not is_git:
        return 0
    d = backup_dir(vault)
    now = time.time()
    keep_names = {Path(p).name for p in protect}
    files = []
    for p in d.iterdir():
        if not _BACKUP_NAME_RE.match(p.name) or p.name in keep_names:
            continue
        if p.is_symlink() or not p.is_file():
            continue
        t = _name_time(p.name)
        if t is None or t > now:
            continue
        files.append((t, p.name, p))
    files.sort(reverse=True)
    removed = 0
    for _, _, p in files[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _valid_old_lines(lines) -> bool:
    if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
        return False
    if any(("\n" in x or "\r" in x) for x in lines):
        return False
    if not lines:
        return True
    return bool(_SUMMARY_LINE_RE.match(lines[0])) and all(x[:1] in (" ", "\t") for x in lines[1:])


def restore_backup(vault: Path, path, entries: dict) -> dict:
    vault = Path(vault)
    p = Path(path)
    try:
        rp = p.resolve()
        expected_dir = vault.resolve() / ".meta" / BACKUP_DIRNAME
    except OSError:
        return {"status": "error", "reason": "invalid_backup_path"}
    if p.is_symlink() or rp.parent != expected_dir or not _BACKUP_NAME_RE.match(rp.name) or not rp.is_file():
        return {"status": "error", "reason": "invalid_backup_path"}
    try:
        raw_lines = rp.read_text(encoding="utf-8").split(chr(10))   # 只认 LF：splitlines 会被 U+2028 切断
    except (OSError, ValueError):
        return {"status": "error", "reason": "backup_unreadable"}
    recs, skipped = [], []
    for n, ln in enumerate(raw_lines, 1):
        if not ln.strip():
            continue
        try:
            recs.append(json.loads(ln))
        except ValueError:   # 坏一行只跳过这一行，不让整份不可恢复
            skipped.append({"path": None, "reason": "bad_record", "line": n})
    out_bf = new_backup_file(vault)
    restored = []
    for rec in reversed(recs):
        rel = rec.get("path") if isinstance(rec, dict) else None
        if not isinstance(rel, str) or not _valid_old_lines(rec.get("old_lines")):
            skipped.append({"path": rel, "reason": "bad_record"})
            continue
        np_ = safe_note_path(vault, rel, entries, include_archived=True)
        if np_ is None:
            skipped.append({"path": rel, "reason": "path_rejected"})
            continue
        cur = current_summary(np_)
        if cur != rec.get("new"):
            skipped.append({"path": rel, "reason": "changed"})
            continue
        cur_lines = field_lines(np_, "summary") or []
        if not write_field_lines(np_, "summary", rec["old_lines"]):
            skipped.append({"path": rel, "reason": "write_failed"})
            continue
        restored_value = current_summary(np_)
        append_backup(out_bf, backup_record(rel, cur, cur_lines, restored_value, out_bf.name))
        sync_cache_summary(vault, rel, restored_value)
        restored.append(rel)
    return {"status": "ok", "restored": restored, "skipped": skipped, "backup": str(out_bf)}


def load_attempts(vault: Path) -> dict:
    p = Path(vault) / ".meta" / ATTEMPTS_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items()
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)}


def bump_attempt(vault: Path, rel: str) -> int:
    d = load_attempts(vault)
    d[rel] = d.get(rel, 0) + 1
    try:
        atomic_write_text(str(Path(vault) / ".meta" / ATTEMPTS_FILE), json.dumps(d, ensure_ascii=False))
    except OSError:
        pass
    return d[rel]


def reset_attempts(vault: Path, rels=None) -> int:
    """清掉尝试计数：rels 为 None 时全部清空；返回清掉的条数。"""
    d = load_attempts(vault)
    if rels is None:
        cleared, d = len(d), {}
    else:
        cleared = sum(1 for r in rels if d.pop(r, None) is not None)
    if cleared:
        try:
            atomic_write_text(str(Path(vault) / ".meta" / ATTEMPTS_FILE), json.dumps(d, ensure_ascii=False))
        except OSError:
            return 0
    return cleared


def acquire_single_instance(vault: Path):
    p = Path(vault) / ".meta" / LOCK_FILE
    # 锁文件首行写 PID（_fs._acquire_lock 格式）。timeout=0：持锁进程还活着就立即失败——锁再旧也不抢，
    # 跑超时的批次不会被第二个实例接管、重复付费；持锁进程已死（被硬杀）立即接管，不必等过期（PERF-M4）
    return p if _acquire_lock(str(p), timeout=0) else None


def single_instance_holder(vault: Path):
    """锁被活进程持有时返回 {"lock", "pid", "age_sec"}，否则 None——报错时给出可诊断信息、供收尾补缺判互斥。"""
    p = Path(vault) / ".meta" / LOCK_FILE
    try:
        age = int(time.time() - p.stat().st_mtime)
    except OSError:
        return None
    pid = _read_lock_pid(str(p))
    if not pid or not _pid_alive(pid):
        return None
    return {"lock": str(p), "pid": pid, "age_sec": age}


def release_single_instance(lock: Path) -> None:
    try:
        Path(lock).unlink()
    except OSError:
        pass
