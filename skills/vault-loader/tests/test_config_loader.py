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
    """P0 升级链治理（spec §8.1）：缺失时不再把全量 DEFAULT_CONFIG 写盘（会让默认值
    演进对物化窗口用户永久失效），改写最小占位；但 load_config 返回值仍是完整
    merge 后 dict，且不得残留 `_config_version`/`_comment` 杂键。"""
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    assert not cfg_path.exists()

    cfg = load_config(cfg_path)

    # 返回值仍等于 DEFAULT 合并结果（结构不变）
    assert cfg["enabled"] is True
    assert cfg["session_start"]["max_notes"] == 5
    assert cfg["session_start"]["max_commits"] == 5
    assert cfg["session_start"]["include_tag_matched_notes"] is True
    assert cfg["user_prompt_submit"]["fulltext_threshold"] == 10
    assert cfg_path.exists(), "缺失时应自动写出配置占位"
    assert cfg == DEFAULT_CONFIG, "load_config 返回值应仍等于 DEFAULT 合并结果"
    assert "_config_version" not in cfg, "返回 dict 不得含内部标记键"
    assert "_comment" not in cfg

    # 盘上文件应为最小占位，而非全量 DEFAULT_CONFIG——默认值演进才能对新用户即时生效
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk.get("_config_version") == 1
    assert "_comment" in on_disk
    assert "enabled" not in on_disk, "首跑不应把全量默认写盘，否则默认值演进对旧用户静默失效"
    assert "session_start" not in on_disk
    assert "scoring" not in on_disk
    assert "relevance" not in on_disk


def test_minimal_stub_on_disk_merges_back_to_full_default(tmp_home: Path) -> None:
    """新格式盘上文件（仅含 _config_version/_comment 占位）经 merge 应还原为完整
    默认值，且返回 dict 不得残留 _config_version/_comment 杂键（边界自查）。"""
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "_config_version": 1,
        "_comment": "完整可配键见 SKILL.md；默认值随版本演进自动生效，显式写入的键才会覆盖默认",
    }, ensure_ascii=False), encoding="utf-8")

    cfg = load_config(cfg_path)

    assert cfg == DEFAULT_CONFIG
    assert "_config_version" not in cfg
    assert "_comment" not in cfg


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


def test_bom_config_is_parsed_not_silently_discarded(tmp_home: Path) -> None:
    """UTF-8 BOM 不得让整份 config 被静默丢弃（P4-3）。

    PowerShell 5.1 的 `Out-File -Encoding utf8` 与多个编辑器默认写出带 BOM 的 UTF-8，
    Windows 上是真实路径。读取端用 `utf-8` 解码会把 BOM 留在首字符 → json 解析失败 →
    走 except 分支回退**全默认**，`vault_path` 一并丢失，vault-loader 转去读不存在的
    默认 vault，唯一信号是用户看不到的一行 stderr。`utf-8-sig` 对无 BOM 输入同样正确。
    """
    cfg_path = _config_path(tmp_home)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "vault_path": "/my/vault",
        "relevance": {"min_topical_score": 9},
    }, ensure_ascii=False), encoding="utf-8-sig")
    assert cfg_path.read_bytes().startswith(b"\xef\xbb\xbf"), "前置条件：文件确实带 BOM"

    cfg = load_config(cfg_path)

    assert cfg["vault_path"] == "/my/vault", "带 BOM 时用户 vault_path 不得被静默丢弃"
    assert cfg["relevance"]["min_topical_score"] == 9, "带 BOM 时用户调参不得被静默丢弃"
    # 未覆盖字段仍走默认（证明是正常 deep-merge，而非"整份原样返回"）
    assert cfg["relevance"]["tag_idf_floor"] == DEFAULT_CONFIG["relevance"]["tag_idf_floor"]


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


def test_check_bom_ss_config_still_compared(tmp_home: Path, capsys) -> None:
    """summarize-session config 带 BOM 时，跨 skill 一致性自检不得被静默跳过——
    否则两侧 vault 路径已经分叉却完全无告警（与 test_check_corrupted_ss_config_is_silent
    的"真损坏时静默"不同：BOM 文件是合法内容，只是编码前缀）。"""
    ss_path = _ss_cfg_path(tmp_home)
    ss_path.parent.mkdir(parents=True, exist_ok=True)
    ss_path.write_text(
        json.dumps({"default_vault_path": str(tmp_home / "other-vault")}),
        encoding="utf-8-sig",
    )
    assert ss_path.read_bytes().startswith(b"\xef\xbb\xbf")

    vl_cfg = {"vault_path": str(tmp_home / ".claude" / "knowledge-vault")}
    check_vault_path_consistency(vl_cfg, tmp_home)

    captured = capsys.readouterr()
    assert "[vault-loader] 警告：vault 路径不一致" in captured.err
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


# ── Task 3：config 第0/1层三键 + relevance 归一化 ──────────────────────────────

def test_new_relevance_keys_defaults(tmp_path) -> None:
    from scripts._config_loader import load_config

    cfg = load_config(tmp_path / "config.json")
    rel = cfg["relevance"]
    assert rel["split_cjk_bigram"] is True
    assert rel["relax_pure_cjk_single"] is True
    assert rel["exclude_note_tags"] == ["archived"]


def test_relevance_normalization_coerces_bad_types(tmp_path) -> None:
    """deep-merge 无类型校验：笔误配置不得静默关停召回（security finding）。"""
    import json
    from scripts._config_loader import load_config

    p = tmp_path / "config.json"
    p.write_text(json.dumps({"relevance": {
        "exclude_note_tags": "archived",      # 应为 list → 回退默认
        "split_cjk_bigram": "yes",            # 非 bool → bool() 真值化
        "relax_pure_cjk_single": 0,           # → False
        "max_prompt_keywords": "30",          # 字符串 → int
    }}), encoding="utf-8")
    rel = load_config(p)["relevance"]
    assert rel["exclude_note_tags"] == ["archived"]
    assert rel["split_cjk_bigram"] is True
    assert rel["relax_pure_cjk_single"] is False
    assert rel["max_prompt_keywords"] == 30


def test_relevance_normalization_bad_int_falls_back(tmp_path) -> None:
    import json
    from scripts._config_loader import load_config

    p = tmp_path / "config.json"
    p.write_text(json.dumps({"relevance": {"max_prompt_keywords": "abc",
                                           "exclude_note_tags": ["Archived", 123]}}),
                 encoding="utf-8")
    rel = load_config(p)["relevance"]
    assert rel["max_prompt_keywords"] == 30          # 非法 → 默认
    assert rel["exclude_note_tags"] == ["Archived"]  # 逐元素滤非 str


def test_relevance_normalization_clamps_negative_max_keywords(tmp_path) -> None:
    """F-BP-3：负 max_prompt_keywords（可解析为 int 但语义非法）会让下游头尾切片
    hn/tn 转负→退化截断，违「防笔误静默劣化召回」初衷。归一化须 clamp 到非负。"""
    import json
    from scripts._config_loader import load_config

    p = tmp_path / "config.json"
    p.write_text(json.dumps({"relevance": {"max_prompt_keywords": -5}}),
                 encoding="utf-8")
    rel = load_config(p)["relevance"]
    assert rel["max_prompt_keywords"] == 0           # 负 → clamp 到 0（不截断上限）


def test_relevance_non_dict_string_falls_back_to_default(tmp_path) -> None:
    """FIX-4：relevance 整段配成非 dict（如误把 archived 值直接顶替整段）时，
    旧代码 _normalize_relevance 的 .get 调用会抛 AttributeError、逃逸 load_config 的
    except（仅捕 JSONDecodeError/ValueError/OSError）。必须优雅回退 DEFAULT_CONFIG。"""
    import json
    from scripts._config_loader import load_config, DEFAULT_CONFIG

    p = tmp_path / "config.json"
    p.write_text(json.dumps({"relevance": "archived"}), encoding="utf-8")
    rel = load_config(p)["relevance"]  # 不应抛异常
    assert rel == DEFAULT_CONFIG["relevance"]
    assert rel["split_cjk_bigram"] is True


def test_relevance_non_dict_list_falls_back_to_default(tmp_path) -> None:
    import json
    from scripts._config_loader import load_config, DEFAULT_CONFIG

    p = tmp_path / "config.json"
    p.write_text(json.dumps({"relevance": ["x"]}), encoding="utf-8")
    rel = load_config(p)["relevance"]  # 不应抛异常
    assert rel == DEFAULT_CONFIG["relevance"]


def test_load_config_ex_distinguishes_fresh_install_from_corrupt(tmp_path):
    """D1(b)：load_config_ex 必须把「零配置新装」与「config 损坏回退」分开。

    两者在 load_config 的返回值上完全同形（都是 DEFAULT_CONFIG），而后者是全用户级的
    静默失效单点：丢的不只 vault_path，scoring/relevance/keyword_to_tags/opt_out_paths
    的用户调参全部作废。若把「文件不存在」也判为失效，则每个新装用户第一次会话就会
    被误报——这是本方案最大的误报面。
    """
    from scripts._config_loader import load_config_ex

    # ① 文件不存在 = 零配置新装 → 不置位
    fresh = tmp_path / "fresh" / "config.json"
    r1 = load_config_ex(fresh)
    assert r1.fallback_reason is None, "零配置新装不得判为失效"
    assert r1.detail == ""
    assert fresh.exists(), "应写入最小占位"

    # ② 内容合法 = 正常 → 不置位
    ok = tmp_path / "ok.json"
    ok.write_text('{"vault_path": "D:/V"}', encoding="utf-8")
    r2 = load_config_ex(ok)
    assert r2.fallback_reason is None
    assert r2.config["vault_path"] == "D:/V"

    # ③ 解析失败 = 真失效 → 置位 corrupt，且带可诊断细节
    bad = tmp_path / "bad.json"
    bad.write_text('{"vault_path": "D:/V", }', encoding="utf-8")   # 尾逗号
    r3 = load_config_ex(bad)
    assert r3.fallback_reason == "corrupt", "config 损坏必须置位"
    assert r3.detail, "应带异常文本供诊断"
    assert r3.config["vault_path"] == DEFAULT_CONFIG["vault_path"], "损坏时确实回退了默认"
    assert bad.read_text(encoding="utf-8") == '{"vault_path": "D:/V", }', "损坏文件不得被覆盖"


def test_detail_does_not_leak_config_contents(tmp_path):
    """诊断细节不得回显 config 文件内容——它会进用户可见通道。"""
    from scripts._config_loader import load_config_ex

    bad = tmp_path / "secret.json"
    bad.write_text('{"vault_path": "D:/V", "token": "ghp_deadbeef", }', encoding="utf-8")
    r = load_config_ex(bad)
    assert r.fallback_reason == "corrupt"
    assert "ghp_deadbeef" not in r.detail, f"异常文本泄露了 config 内容：{r.detail!r}"
