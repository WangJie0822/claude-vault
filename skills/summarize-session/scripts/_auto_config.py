"""自动化模式配置加载与默认值合并。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_HARD_RULES: dict[str, Any] = {
    "min_messages": 5,
    "min_size_kb": 20,
    "min_duration_min": 3,
    "require_edit_or_write": True,
}

DEFAULT_AUTO: dict[str, Any] = {
    "enabled": False,
    "dry_run": True,
    "model": "claude-sonnet-4-6",
    "max_per_run": 8,
    "session_timeout_sec": 480,
    "run_timeout_sec": 3600,
    "log_retention_days": 30,
    "max_failure_count": 3,
    "hard_rules": DEFAULT_HARD_RULES,
}


@dataclass
class HardRules:
    min_messages: int
    min_size_kb: float
    min_duration_min: float
    require_edit_or_write: bool


@dataclass
class AutoConfig:
    enabled: bool
    dry_run: bool
    model: str
    max_per_run: int
    session_timeout_sec: int
    run_timeout_sec: int
    log_retention_days: int
    max_failure_count: int
    hard_rules: HardRules


def _merge(default: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(default)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_auto_config(config_path: Path) -> AutoConfig:
    """读取 config.json,与 DEFAULT_AUTO 合并,返回结构化对象。"""
    if not Path(config_path).exists():
        merged = DEFAULT_AUTO
    else:
        try:
            raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
        merged = _merge(DEFAULT_AUTO, raw.get("auto", {}))

    hr = merged["hard_rules"]
    return AutoConfig(
        enabled=bool(merged["enabled"]),
        dry_run=bool(merged["dry_run"]),
        model=str(merged["model"]),
        max_per_run=int(merged["max_per_run"]),
        session_timeout_sec=int(merged["session_timeout_sec"]),
        run_timeout_sec=int(merged["run_timeout_sec"]),
        log_retention_days=int(merged["log_retention_days"]),
        max_failure_count=int(merged["max_failure_count"]),
        hard_rules=HardRules(
            min_messages=int(hr["min_messages"]),
            min_size_kb=float(hr["min_size_kb"]),
            min_duration_min=float(hr["min_duration_min"]),
            require_edit_or_write=bool(hr["require_edit_or_write"]),
        ),
    )
