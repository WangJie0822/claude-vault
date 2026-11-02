"""宿主会话来源的共享契约。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
from typing import Any, Mapping, Optional

MAX_SOURCE_BYTES = 128 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_HOST_WRAPPERS = ("# AGENTS.md instructions", "<environment_context>", "<skill>",
                  "<external_codex_apps_open_page>", "<turn_aborted>")
CLAUDE_USER_SOURCES = ("typed", "queued", "suggestion_accepted")


@dataclass(frozen=True)
class SessionRef:
    host: str
    thread_id: str
    root_session_id: str
    transcript_path: Path
    cwd: Path
    actor_kind: str = "unknown"
    originator: str = ""


@dataclass
class SessionRead:
    messages: list
    timerange: Optional[dict]
    touched_files: list
    completeness: str
    reasons: list
    metadata: dict = field(default_factory=dict)


def resolve_current_session(*, host: str, context: Mapping[str, Any], home: Path) -> SessionRef:
    if host not in ("claude", "codex"):
        raise ValueError("unsupported session host")
    sid = context.get("thread_id") or context.get("session_id")
    if not isinstance(sid, str) or not _ID.fullmatch(sid) or sid == "unknown":
        raise ValueError("exact thread identity is required")
    directory = Path(context.get(host + "_home") or home / ("." + host))
    roots = ([directory / "sessions", directory / "archived_sessions"] if host == "codex"
             else [directory / "projects"])
    explicit = context.get("transcript_path")
    candidates = [Path(explicit)] if explicit else [p for root in roots for p in root.rglob("*" + sid + ".jsonl")]
    found = []
    for p in candidates:
        try:
            p = p.resolve(strict=True)
            if p.suffix != ".jsonl" or not stat.S_ISREG(os.lstat(p).st_mode):
                continue
            if not any(_under(p, root.resolve()) for root in roots):
                continue
            meta = _first_metadata(p, host)
            if meta.get("id") != sid:
                continue
            if host == "codex" and not p.name.startswith("rollout-"):
                continue
            cwd = Path(context.get("cwd") or meta.get("cwd") or ".")
            root_id = meta.get("root_session_id")
            root_id = root_id if isinstance(root_id, str) and _ID.fullmatch(root_id) else sid
            originator = meta.get("originator")
            found.append(SessionRef(host, sid, root_id, p, cwd, _actor(meta),
                                    originator if isinstance(originator, str) else ""))
        except (OSError, ValueError, TypeError):
            continue
    found = list(dict.fromkeys(found))
    if len(found) != 1:
        raise ValueError("session identity is missing, ambiguous, or outside verified roots")
    return found[0]


def _comparison_path(path):
    """只统一 Windows 扩展盘符/UNC 的比较名称，原 Path 仍用于 I/O。"""
    if isinstance(path, PureWindowsPath):
        name = str(path)
        if name.startswith("\\\\?\\"):
            tail = name[4:]
            if re.match(r"[A-Za-z]:\\", tail):
                return PureWindowsPath(tail)
            if tail[:4].casefold() == "unc\\":
                ordinary = PureWindowsPath("\\\\" + tail[4:])
                if ordinary.is_absolute() and not ordinary.drive.startswith(("\\\\?\\", "\\\\.\\")):
                    return ordinary
    return path


def _under(path, root):
    try:
        path, root = _comparison_path(path), _comparison_path(root)
        return path != root and path.is_relative_to(root)
    except ValueError:
        return False


def _rows(path, reasons):
    size = 0
    try:
        with path.open("rb") as f:
            for raw in iter(lambda: f.readline(MAX_SOURCE_BYTES + 1), b""):
                size += len(raw)
                if size > MAX_SOURCE_BYTES:
                    reasons.append("size_limit")
                    break
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8-sig"))
                    if isinstance(obj, dict):
                        yield obj
                    else:
                        reasons.append("malformed_record")
                except (ValueError, UnicodeError, RecursionError):
                    reasons.append("malformed_record")
    except OSError:
        reasons.append("source_unreadable")


def _first_metadata(path, host):
    for obj in _rows(path, []):
        if host == "codex":
            if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                return obj["payload"]
        else:
            return {"id": obj.get("sessionId", path.stem), "cwd": obj.get("cwd", ""),
                    "source": obj.get("source")}
    return {}


def _actor(meta):
    source = meta.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return "subagent"
    if isinstance(source, str) and source in ("cli", "vscode", "exec", "app-server"):
        return "main"
    return "unknown"


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
    except ValueError:
        return None


def _text(content, kinds):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict)
                         and b.get("type") in kinds and isinstance(b.get("text"), str))
    return ""


def _patch_paths(code):
    if not isinstance(code, str) or not code.startswith("*** Begin Patch"):
        return []
    return [m.group(1) for m in re.finditer(
        r"(?m)^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", code)]


def read_session(ref: SessionRef, *, before: Optional[str] = None) -> SessionRead:
    if ref.host not in ("claude", "codex"):
        raise ValueError("unsupported session host")
    cutoff = _date(before) if before is not None else None
    if before is not None and cutoff is None:
        raise ValueError("invalid trigger timestamp")
    meta = _first_metadata(ref.transcript_path, ref.host)
    if ref.host == "codex" and meta.get("id") != ref.thread_id:
        return SessionRead([], None, [], "incomplete", ["identity_mismatch"])
    messages, reasons, times, touched = [], [], [], []
    pending = {}
    unknown_writes = False
    first_ts, raw_cwd = None, meta.get("cwd", "")
    for obj in _rows(ref.transcript_path, reasons):
        ts = obj.get("timestamp")
        date = _date(ts)
        if cutoff is not None and (date is None or date >= cutoff):
            if date is None and obj.get("type") == "response_item":
                reasons.append("timestamp_missing")
            continue
        if date is not None:
            times.append(date)
            if first_ts is None:
                first_ts = ts
        if not raw_cwd and isinstance(obj.get("cwd"), str):
            raw_cwd = obj["cwd"]
        role, text, eligible = None, "", False
        if ref.host == "codex":
            if obj.get("type") != "response_item":
                continue
            p = obj.get("payload")
            if not isinstance(p, dict):
                continue
            kind = p.get("type")
            if kind == "message" and p.get("role") in ("user", "assistant"):
                role = p["role"]
                text = _text(p.get("content"), {"input_text"} if role == "user" else {"output_text"})
                if role == "user" and text.lstrip().startswith(_HOST_WRAPPERS):
                    continue
                eligible = role == "user"
            elif kind in ("function_call", "custom_tool_call"):
                code = p.get("input", p.get("arguments", ""))
                paths = _patch_paths(code) if p.get("name") == "apply_patch" else []
                if paths and isinstance(p.get("call_id"), str):
                    pending[p["call_id"]] = paths
                if not paths or (isinstance(code, str) and "exec_command" in code):
                    unknown_writes = True
            elif kind in ("function_call_output", "custom_tool_call_output"):
                paths = pending.pop(p.get("call_id"), [])
                output = _text(p.get("output"), {"text"})
                # 包装器成功不等于其中的写操作成功；只认补丁工具本身的成功回执。
                if paths and output.startswith("Success. Updated"):
                    touched.extend(Path(s) if Path(s).is_absolute() else ref.cwd / s for s in paths)
                elif paths:
                    unknown_writes = True
        else:
            p = obj.get("message")
            if not isinstance(p, dict):
                continue
            role = p.get("role")
            if role in ("user", "assistant"):
                content = p.get("content")
                if isinstance(content, list):
                    content = [b for b in content if not (isinstance(b, dict)
                        and isinstance(b.get("text"), str)
                        and b["text"].strip().startswith(("<system-reminder>", "<command-message>")))]
                text = _text(content, {"text"})
                eligible = (role == "user" and obj.get("promptSource") in CLAUDE_USER_SOURCES
                            and not any(obj.get(k) for k in ("isMeta", "isSidechain", "isCompactSummary"))
                            and not text.lstrip().startswith("<"))
            for block in p.get("content", []) if isinstance(p.get("content"), list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    unknown_writes = True
        if role in ("user", "assistant") and text.strip():
            messages.append({"role": role, "text": text, "timestamp": ts,
                             "eligible_for_recall": eligible})
    timerange = None
    if times:
        start, end = min(times).astimezone(), max(times).astimezone()
        timerange = {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                     "date": start.strftime("%Y-%m-%d"),
                     "duration_hours": round((end - start).total_seconds() / 3600, 2)}
    metadata = {"cwd": raw_cwd, "first_timestamp": first_ts, "actor_kind": ref.actor_kind,
                "touched_files_status": "unknown" if unknown_writes or pending else "complete"}
    return SessionRead(messages, timerange, list(dict.fromkeys(touched)),
                       "incomplete" if reasons else "complete", list(dict.fromkeys(reasons)), metadata)


def recent_prompts(ref: SessionRef, *, before: str, limit: int) -> list[str]:
    if type(limit) is not int or limit <= 0:
        return []
    result = read_session(ref, before=before)
    if result.completeness != "complete":
        raise ValueError("session source is incomplete: " + ",".join(result.reasons))
    return [m["text"] for m in result.messages if m["role"] == "user" and m["eligible_for_recall"]][-limit:]
