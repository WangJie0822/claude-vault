"""Normalize Claude Code hook payloads without host-specific imports.

⚠️ 本文件同时是 7 处「插件根」判据的哨兵：vault-loader 的 `_state` / `_metrics` /
`_config_loader`、两个 hook 入口、summarize-session 的 `enrich_keywords`、config
skill 的 `config_inventory` 都用「`<插件根>/context_vault/runtime.py` 是否存在」判断
自己是否跑在插件布局里。**不得删除或改名**：判据不成立时 `use_canonical_namespace()`
恒为 False，全体用户被静默切回 0.9.x 的 legacy 布局。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class RuntimeKind(str, Enum):
    """宿主。2.0.0 起只支持 Claude Code。

    保留枚举而不是改成裸字符串：`.value` 是 state / metrics / events 三处命名空间的
    路径段，调用方一律取 `.value`。1.x 按 payload 的 `turn_id`/`model` 与环境变量
    `PLUGIN_ROOT` 判出 CODEX，Claude Code 的 SessionStart 因此在部分模式下被错判、
    记录落进 codex 命名空间——宿主固定后这条错判不复存在。
    """
    CLAUDE = "claude"


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
    source: str = ""
    actor: str = ""

    @property
    def metrics_event_id(self) -> str:
        """写进 metrics 记录的事件 id：只有宿主给的稳定 id 才落盘。

        不稳定 id 是 `sha256(runtime, event, session_id, source, prompt)` 的截断值，
        材料含 prompt 原文且未加盐（见 `events.claim_event` 的 docstring），而 metrics
        承诺 prompt 只存加盐 hash。两个入口都传本属性，调用点不含分支。
        """
        return self.event_id if self.stable_event_id else ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "HookContext":
        event = _text(payload.get("hook_event_name"))
        cwd_raw = _text(payload.get("cwd")) or os.getcwd()
        session_id = _text(payload.get("session_id")) or "unknown"
        prompt_id = _text(payload.get("prompt_id")) or None
        event_id = prompt_id or ""
        stable_event_id = bool(event_id)
        if not event_id:
            # 宿主不下发 prompt_id（SessionStart 恒如此）时回落内容派生 id。它只用于
            # 合并近乎同时的重复投递，绝不能永久压制后续的提问或压缩。
            raw = "\0".join((RuntimeKind.CLAUDE.value, event, session_id,
                             _text(payload.get("source")),
                             _text(payload.get("prompt"))))
            event_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        transcript = _text(payload.get("transcript_path"))
        return cls(
            runtime=RuntimeKind.CLAUDE,
            event=event,
            cwd=Path(cwd_raw),
            session_id=session_id,
            event_id=event_id,
            stable_event_id=stable_event_id,
            prompt=_text(payload.get("prompt")),
            transcript_path=Path(transcript) if transcript else None,
            prompt_id=prompt_id,
            source=_text(payload.get("source")),
            actor=_text(payload.get("agent_type")),
        )
