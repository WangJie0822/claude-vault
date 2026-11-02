"""默认值单点、旧导出兼容与真实Claude入口的导入工作量。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_host_defaults_keep_legacy_exports_and_values():
    from context_vault import host_config, host_defaults

    expected = {
        "fine_rank": False, "fine_supplement": False,
        "coarse_inject": True, "auto_fulltext": False,
        "fine_pin_max": 1, "fine_max_notes": 3, "fine_timeout_sec": 120,
        "fine_max_calls": 20, "fine_session_max_bytes": 1000000,
        "fine_catalog": True, "fine_catalog_max_chars": 80000,
        "fine_recent_prompts": 3, "fine_exclude_tags": ["private"],
        "session_topic_top_n": 30, "session_topic_project_top_k": 20,
        "session_topic_pool_max": 50,
    }
    assert host_defaults.CODEX_RELEVANCE_DEFAULTS == expected
    assert host_defaults.CODEX_UPS_DEFAULTS == {"max_notes": 3}
    assert host_config.CODEX_RELEVANCE_DEFAULTS is host_defaults.CODEX_RELEVANCE_DEFAULTS
    assert host_config.CODEX_UPS_DEFAULTS is host_defaults.CODEX_UPS_DEFAULTS
    loaded = host_config.effective_host_config({}, host="codex", base={"relevance": deepcopy(expected)})
    loaded["relevance"]["fine_exclude_tags"].append("changed")
    assert host_defaults.CODEX_RELEVANCE_DEFAULTS == expected


_CLAUDE_ENTRY = "\n".join([
    "import json, runpy, sys",
    "try:",
    "    runpy.run_path(sys.argv[1], run_name='__main__')",
    "except SystemExit as exc:",
    "    assert exc.code in (None, 0), exc.code",
    "cfg = sys.modules['scripts._config_loader'].DEFAULT_CONFIG",
    "defaults_was_loaded = 'context_vault.host_defaults' in sys.modules",
    "from context_vault import host_defaults",
    "print('CV_DEFAULTS=' + json.dumps({'model_loaded': 'context_vault.host_config' in sys.modules,",
    "    'defaults_loaded': defaults_was_loaded,",
    "    'isolated': cfg['runtimes']['codex']['relevance'] is not host_defaults.CODEX_RELEVANCE_DEFAULTS",
    "        and cfg['runtimes']['codex']['relevance'].get('fine_exclude_tags') is not host_defaults.CODEX_RELEVANCE_DEFAULTS.get('fine_exclude_tags'),",
    "    'runtimes': cfg['runtimes']}), file=sys.stderr)",
])


def test_actual_claude_hook_keeps_defaults_without_loading_model_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    empty = home / "emptybin"
    empty.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PATH=str(empty))
    env.pop("VAULT_LOADER_DISABLE", None)
    vault = home / "Vault"
    builder = ROOT / "skills/vault-loader/tests/fixtures/build_large_vault.py"
    subprocess.run([sys.executable, str(builder), str(vault), "30"], env=env,
                   check=True, capture_output=True, timeout=60)
    config_dir = home / ".context-vault"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"vault_path": str(vault),
        "relevance": {"fine_rank": False, "session_topic": False}}), encoding="utf-8")
    result = subprocess.run([sys.executable, "-c", _CLAUDE_ENTRY,
        str(ROOT / "skills/vault-loader/scripts/prompt_submit_load.py")],
        input=json.dumps({"cwd": str(home), "session_id": "sess-host-defaults",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "召回 扩展词 相关性打分 回归测试 语义检索 关键词匹配 怎么优化实现"}),
        env=env, cwd=home, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    markers = [line[len("CV_DEFAULTS="):] for line in result.stderr.splitlines()
               if line.startswith("CV_DEFAULTS=")]
    assert len(markers) == 1, result.stderr
    report = json.loads(markers[0])
    assert report["defaults_loaded"] and not report["model_loaded"], report
    assert report["isolated"], "默认树和常量的嵌套可变对象不能串改"
    assert report["runtimes"]["codex"]["relevance"]["fine_rank"] is False
    assert report["runtimes"]["codex"]["relevance"]["fine_exclude_tags"] == ["private"]
    assert report["runtimes"]["codex"]["user_prompt_submit"] == {"max_notes": 3}
    assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"], result.stderr
