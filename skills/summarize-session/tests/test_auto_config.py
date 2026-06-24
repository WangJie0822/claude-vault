from __future__ import annotations
import json
from pathlib import Path

import pytest

from _auto_config import load_auto_config, AutoConfig, DEFAULT_AUTO


def test_load_with_no_auto_section_returns_defaults(tmp_path: Path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"default_vault_path": "/v"}))
    auto = load_auto_config(cfg)
    assert auto.enabled is False
    assert auto.dry_run is True
    assert auto.model == "claude-sonnet-4-6"
    assert auto.max_per_run == 8
    assert auto.session_timeout_sec == 480
    assert auto.run_timeout_sec == 3600
    assert auto.log_retention_days == 30
    assert auto.max_failure_count == 3
    assert auto.hard_rules.min_messages == 5
    assert auto.hard_rules.min_size_kb == 20
    assert auto.hard_rules.min_duration_min == 3
    assert auto.hard_rules.require_edit_or_write is True


def test_load_with_partial_auto_merges_defaults(tmp_path: Path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "default_vault_path": "/v",
        "auto": {"enabled": True, "max_per_run": 3}
    }))
    auto = load_auto_config(cfg)
    assert auto.enabled is True
    assert auto.max_per_run == 3
    assert auto.dry_run is True  # 未指定走默认
    assert auto.hard_rules.min_messages == 5  # 未指定走默认


def test_load_with_partial_hard_rules_merges(tmp_path: Path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "auto": {"hard_rules": {"min_messages": 10}}
    }))
    auto = load_auto_config(cfg)
    assert auto.hard_rules.min_messages == 10
    assert auto.hard_rules.min_size_kb == 20  # 走默认


def test_load_missing_file_returns_defaults(tmp_path: Path):
    cfg = tmp_path / "missing.json"
    auto = load_auto_config(cfg)
    assert auto.enabled is False
    assert auto.dry_run is True


def test_default_auto_dict_complete():
    # DEFAULT_AUTO 必须含 spec 第 12 节列出的所有字段
    required = {
        "enabled", "dry_run", "model", "max_per_run",
        "session_timeout_sec", "run_timeout_sec",
        "log_retention_days", "max_failure_count", "hard_rules"
    }
    assert required <= set(DEFAULT_AUTO.keys())
