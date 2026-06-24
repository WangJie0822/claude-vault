"""_config_loader 单测：默认值、深合并、损坏处理。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts._config_loader import (
    DEFAULT_CONFIG,
    load_config,
    check_vault_path_consistency,
)


def _config_path(home: Path) -> Path:
    return home / ".claude" / "skills" / "vault-loader" / "config.json"


def test_missing_file_returns_default_and_writes(tmp_home: Path) -> None:
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    assert not cfg_path.exists()

    cfg = load_config(cfg_path)

    assert cfg["enabled"] is True
    assert cfg["session_start"]["max_notes"] == 5
    assert cfg["session_start"]["max_commits"] == 5
    assert cfg["session_start"]["include_tag_matched_notes"] is True
    assert cfg["user_prompt_submit"]["fulltext_threshold"] == 10
    assert cfg_path.exists(), "缺失时应自动写出默认配置"


def test_full_override(tmp_home: Path) -> None:
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "enabled": False,
        "session_start": {"max_notes": 3},
    }))

    cfg = load_config(cfg_path)

    assert cfg["enabled"] is False
    assert cfg["session_start"]["max_notes"] == 3
    assert cfg["session_start"]["recent_worklog_days"] == DEFAULT_CONFIG["session_start"]["recent_worklog_days"], \
        "未覆盖字段应保留默认"


def test_corrupted_json_returns_default_keeps_file(tmp_home: Path) -> None:
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{not valid json")

    cfg = load_config(cfg_path)

    assert cfg == DEFAULT_CONFIG
    assert cfg_path.read_text() == "{not valid json", "损坏文件不得被覆盖"


def test_deep_merge_nested_dict(tmp_home: Path) -> None:
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "scoring": {"exact_project_dir": 99},
        "keyword_to_tags": {"foo": ["bar"]},
    }))

    cfg = load_config(cfg_path)

    assert cfg["scoring"]["exact_project_dir"] == 99
    assert cfg["scoring"]["tag_target_set_hit"] == DEFAULT_CONFIG["scoring"]["tag_target_set_hit"]
    assert "foo" in cfg["keyword_to_tags"]
    # 中性化后 DEFAULT_CONFIG["keyword_to_tags"]={} → deep-merge 后只剩用户提供的 "foo"
    assert "assistant" not in cfg["keyword_to_tags"], "中性化后默认 keyword_to_tags 为空，无 assistant 键"


def test_display_section_defaults(tmp_home: Path) -> None:
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config(cfg_path)
    assert cfg["display"]["user_visible"] is True
    assert cfg["display"]["verbosity"] in ("compact", "list")
    assert cfg["display"]["show_size"] is True


def test_display_partial_override_deep_merge(tmp_home: Path) -> None:
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"display": {"user_visible": False}}))
    cfg = load_config(cfg_path)
    assert cfg["display"]["user_visible"] is False
    assert cfg["display"]["show_size"] is True


def test_old_config_without_display_gets_default(tmp_home: Path) -> None:
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"dry_run": False, "vault_path": "/x"}))
    cfg = load_config(cfg_path)
    assert "display" in cfg and cfg["display"]["user_visible"] is True


def test_relevance_section_defaults() -> None:
    rel = DEFAULT_CONFIG["relevance"]
    assert rel["strip_slash_command"] is True
    assert rel["min_topical_score"] == 4
    assert rel["fulltext_topical_threshold"] == 6
    assert rel["confidence_bands"]["high"] == 6
    assert rel["short_summary_chars"] == 20
    # 后续优化新增字段（英文切分 + 兜底提示）
    assert rel["split_english_token"] is True
    assert rel["en_subtoken_min"] == 4         # 3 经实证为召回灾难，默认 4
    assert rel["fallback_hint"] is True
    assert rel["skip_non_user_prompts"] is True  # 拦截非用户输入（task-notification）


def test_old_config_without_relevance_gets_default(tmp_home: Path) -> None:
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"vault_path": "/x"}))
    cfg = load_config(cfg_path)
    assert cfg["relevance"]["min_topical_score"] == 4   # 旧 config 经 deep-merge 继承默认
    assert cfg["vault_path"] == "/x"                      # 用户值保留


def test_old_config_with_partial_relevance_gets_new_field_defaults(tmp_home: Path) -> None:
    """旧 config 只带部分 relevance 字段 → 新增字段经 deep-merge 补默认。"""
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"relevance": {"min_topical_score": 5}}))
    cfg = load_config(cfg_path)
    assert cfg["relevance"]["min_topical_score"] == 5            # 用户值保留
    assert cfg["relevance"]["split_english_token"] is True       # 新字段补默认
    assert cfg["relevance"]["en_subtoken_min"] == 4
    assert cfg["relevance"]["fallback_hint"] is True


def test_default_config_has_no_private_tags():
    from scripts._config_loader import DEFAULT_CONFIG
    # keyword_to_tags 不得含任何私人项目映射
    assert DEFAULT_CONFIG["keyword_to_tags"] == {}


def test_default_vault_path_is_neutral():
    from scripts._config_loader import DEFAULT_CONFIG
    vp = DEFAULT_CONFIG["vault_path"]
    # 路径末尾必须为 .claude/knowledge-vault（中性默认，非私人 ~/Vault 硬编码）
    assert vp.replace("\\", "/").endswith(".claude/knowledge-vault")
    # 不得是旧私人硬编码（仅含 Vault 而不含 knowledge-vault）
    assert "Vault" not in vp or "knowledge-vault" in vp


def test_default_dry_run_false():
    from scripts._config_loader import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["dry_run"] is False  # D7：默认真注入


def test_opt_out_paths_cross_platform():
    from scripts._config_loader import DEFAULT_CONFIG
    paths = DEFAULT_CONFIG["opt_out_paths"]
    assert any("tmp" in p.lower() for p in paths)
    assert any("temp" in p.lower() for p in paths)  # Windows


# ── check_vault_path_consistency 自检测试 ──────────────────────────────────────

def _ss_cfg_path(home: Path) -> Path:
    return home / ".claude" / "skills" / "summarize-session" / "config.json"


def test_check_no_ss_config_is_silent(tmp_home: Path, capsys) -> None:
    """summarize-session config 不存在时静默（不打印告警）。"""
    vl_cfg = {"vault_path": str(tmp_home / ".claude" / "knowledge-vault")}
    check_vault_path_consistency(vl_cfg, tmp_home)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_check_consistent_paths_is_silent(tmp_home: Path, capsys) -> None:
    """两侧路径一致时静默。"""
    kv = str(tmp_home / ".claude" / "knowledge-vault")
    ss_path = _ss_cfg_path(tmp_home)
    ss_path.parent.mkdir(parents=True, exist_ok=True)
    ss_path.write_text(json.dumps({"default_vault_path": kv}), encoding="utf-8")
    vl_cfg = {"vault_path": kv}
    check_vault_path_consistency(vl_cfg, tmp_home)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_check_inconsistent_paths_warns(tmp_home: Path, capsys) -> None:
    """两侧路径不一致时打印一行 stderr 告警。"""
    ss_path = _ss_cfg_path(tmp_home)
    ss_path.parent.mkdir(parents=True, exist_ok=True)
    ss_path.write_text(
        json.dumps({"default_vault_path": str(tmp_home / "other-vault")}),
        encoding="utf-8",
    )
    vl_cfg = {"vault_path": str(tmp_home / ".claude" / "knowledge-vault")}
    check_vault_path_consistency(vl_cfg, tmp_home)
    captured = capsys.readouterr()
    assert "[vault-loader] 警告：vault 路径不一致" in captured.err
    assert "knowledge-vault" in captured.err
    assert "other-vault" in captured.err


def test_check_corrupted_ss_config_is_silent(tmp_home: Path, capsys) -> None:
    """summarize-session config 损坏时静默（fail-open）。"""
    ss_path = _ss_cfg_path(tmp_home)
    ss_path.parent.mkdir(parents=True, exist_ok=True)
    ss_path.write_text("{not valid json", encoding="utf-8")
    vl_cfg = {"vault_path": str(tmp_home / ".claude" / "knowledge-vault")}
    check_vault_path_consistency(vl_cfg, tmp_home)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_check_empty_default_vault_path_is_silent(tmp_home: Path, capsys) -> None:
    """summarize-session config 有字段但值为空时静默。"""
    ss_path = _ss_cfg_path(tmp_home)
    ss_path.parent.mkdir(parents=True, exist_ok=True)
    ss_path.write_text(json.dumps({"default_vault_path": ""}), encoding="utf-8")
    vl_cfg = {"vault_path": str(tmp_home / ".claude" / "knowledge-vault")}
    check_vault_path_consistency(vl_cfg, tmp_home)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_check_exception_is_swallowed(tmp_home: Path, capsys) -> None:
    """vl_config 含非法路径时不抛出异常（fail-open）。"""
    ss_path = _ss_cfg_path(tmp_home)
    ss_path.parent.mkdir(parents=True, exist_ok=True)
    ss_path.write_text(json.dumps({"default_vault_path": "/some/path"}), encoding="utf-8")
    # vault_path 缺失时应静默不崩
    check_vault_path_consistency({}, tmp_home)  # vault_path 缺失
    captured = capsys.readouterr()
    # 不期望告警（路径解析失败静默跳过）
    assert "崩溃" not in captured.err
