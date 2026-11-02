"""宿主模型选择的纯配置契约；不读文件、不发起模型请求。"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import re
from typing import Any, Mapping, Optional

from .host_defaults import CODEX_RELEVANCE_DEFAULTS, CODEX_UPS_DEFAULTS


_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _object(value) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _host(host: str) -> None:
    if host not in ("claude", "codex"):
        raise ValueError("unsupported host")


def validate_model(value, *, host: str, inherit: bool = False):
    """独立 argv 值校验；不把模型名当作费用或授权等级。"""
    _host(host)
    if inherit and value is None:
        return None
    pattern = _MODEL if host == "codex" else re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63}")
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("invalid model")
    return value


def _optional_name(value, field: str):
    if value == "":
        return None
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("invalid " + field)
    return value


@dataclass(frozen=True)
class ModelSelection:
    backend: str
    model: Optional[str]
    profile: Optional[str]
    provider: Optional[str]
    source: str


class HostConfig(dict):
    """由受信宿主入口生成；JSON 同名键不能伪造宿主元数据。"""
    def __init__(self, *args, host="claude", model_selection=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.host = host
        self.model_selection = model_selection


def resolve_model_selection(raw: Mapping[str, Any], *, host: str,
                            task_model: Optional[str] = None,
                            session_model: Optional[str] = None) -> ModelSelection:
    _host(host)
    raw = _object(raw)
    rel = _object(raw.get("relevance"))
    runtime = _object(_object(raw.get("runtimes")).get(host))
    profile = _optional_name(runtime.get("profile", ""), "profile") if host == "codex" else None
    provider = _optional_name(runtime.get("model_provider", ""), "model_provider") if host == "codex" else None
    if task_model is not None:
        model, source = task_model, "task"
    elif session_model is not None:
        model, source = session_model, "session"
    elif host == "codex" and runtime.get("model", "") != "":
        model, source = runtime["model"], "runtime"
    elif "fine_model" in rel:
        model, source = rel["fine_model"], "legacy-explicit"
    elif host == "codex":
        model, source = None, "codex-native"
    else:
        model, source = "haiku", "claude-default"
    if host == "claude":
        # 保留 Claude 既有非法值回落策略；Codex 显式值不静默替换。
        try:
            model = validate_model(model, host=host)
        except ValueError:
            model = "haiku"
    elif source != "codex-native":
        model = validate_model(model, host=host)
    return ModelSelection(host, model, profile, provider, source)


def effective_host_config(raw: Mapping[str, Any], *, host: str,
                          base: Mapping[str, Any]) -> dict:
    _host(host)
    cfg = deepcopy(dict(base))
    if host == "claude":
        return cfg
    raw = _object(raw)
    runtime = _object(_object(raw.get("runtimes")).get(host))
    overrides = _object(runtime.get("relevance"))
    rel = _object(cfg.get("relevance"))
    rank_allowed = rel.get("fine_rank", True) is not False and bool(rel.get("session_topic", True))
    supplement_allowed = rel.get("fine_supplement", True) is not False
    for key, value in overrides.items():
        if key in CODEX_RELEVANCE_DEFAULTS:
            expected = CODEX_RELEVANCE_DEFAULTS[key]
            if type(value) is type(expected):
                rel[key] = deepcopy(value)
    enabled = cfg.get("enabled", True) is True and runtime.get("enabled", True) is True
    rank = enabled and rank_allowed and overrides.get("fine_rank") is True
    rel["fine_rank"] = rel["session_topic"] = rank
    rel["fine_supplement"] = rank and supplement_allowed and overrides.get("fine_supplement") is True
    selected = resolve_model_selection(raw, host=host)
    rel["fine_model"] = selected.model
    cfg["relevance"] = rel
    ups = _object(cfg.get("user_prompt_submit"))
    raw_ups = _object(runtime.get("user_prompt_submit"))
    if type(raw_ups.get("max_notes")) is int:
        ups["max_notes"] = max(0, min(8, raw_ups["max_notes"]))
    cfg["user_prompt_submit"] = ups
    cfg["enabled"] = enabled
    return HostConfig(cfg, host=host, model_selection=selected)
