"""Normalize Claude Code and Codex hook payloads without host-specific imports."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class RuntimeKind(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    UNKNOWN = "unknown"


def detect_runtime(payload: Mapping[str, Any] | None = None,
                   environ: Mapping[str, str] | None = None) -> RuntimeKind:
    """Detect the host from stable payload fields first, environment second."""
    data = {} if payload is None else payload
    env = os.environ if environ is None else environ
    if data.get("turn_id") or data.get("model"):
        return RuntimeKind.CODEX
    if data.get("prompt_id") or data.get("agent_type"):
        return RuntimeKind.CLAUDE
    if env.get("PLUGIN_ROOT"):
        return RuntimeKind.CODEX
    if env.get("CLAUDE_PLUGIN_ROOT"):
        return RuntimeKind.CLAUDE
    # 兜底：payload 形态可辨认（带 hook_event_name）却没有任何 Codex 特征。
    #
    # 依据是两端实测的 hook input schema：Codex 的 SessionStart 与 UserPromptSubmit
    # 的 required 都含 `model`（UPS 另含 `turn_id`），所以 `model` 缺失 ⇒ 不是 Codex。
    #
    # 没有这一条会怎样（实测，Claude Code 2.1.220）：SessionStart 的 payload 只有
    # cwd / hook_event_name / session_id / source / transcript_path —— 既无 `model`
    # 也无 `prompt_id`；而 `CLAUDE_PLUGIN_ROOT` **不在 hook 子进程的环境里**（它只在
    # hooks.json 的命令串里被插值展开）。于是 SessionStart 落 UNKNOWN、同一会话的
    # UserPromptSubmit 靠 `prompt_id` 判 CLAUDE，两者命名空间分裂：SessionStart 写的
    # 注入去重集 UPS 读不到，启动时注入过的笔记会在第一次提问时被再注入一遍。
    if data.get("hook_event_name"):
        return RuntimeKind.CLAUDE
    return RuntimeKind.UNKNOWN


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class HookContext:
    runtime: RuntimeKind
    event: str
    cwd: Path
    session_id: str
    event_id: str
    stable_event_id: bool = True
    prompt: str = ""
    transcript_path: Path | None = None
    prompt_id: str | None = None
    turn_id: str | None = None
    source: str = ""
    actor: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *,
                     environ: Mapping[str, str] | None = None) -> "HookContext":
        runtime = detect_runtime(payload, environ)
        event = _text(payload.get("hook_event_name"))
        cwd_raw = _text(payload.get("cwd")) or os.getcwd()
        session_id = _text(payload.get("session_id")) or "unknown"
        prompt_id = _text(payload.get("prompt_id")) or None
        turn_id = _text(payload.get("turn_id")) or None
        event_id = prompt_id or turn_id
        stable_event_id = bool(event_id)
        if not event_id:
            # Some host hook payloads do not expose an invocation ID. The
            # fallback only groups near-simultaneous duplicate deliveries; it
            # must never permanently suppress later prompts/compactions.
            raw = "\0".join((runtime.value, event, session_id,
                              _text(payload.get("source")),
                              _text(payload.get("prompt"))))
            event_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        transcript = _text(payload.get("transcript_path"))
        return cls(
            runtime=runtime,
            event=event,
            cwd=Path(cwd_raw),
            session_id=session_id,
            event_id=event_id,
            stable_event_id=stable_event_id,
            prompt=_text(payload.get("prompt")),
            transcript_path=Path(transcript) if transcript else None,
            prompt_id=prompt_id,
            turn_id=turn_id,
            source=_text(payload.get("source")),
            actor=_text(payload.get("agent_type")),
        )
