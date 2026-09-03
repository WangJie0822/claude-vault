from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from context_vault.coexist import legacy_plugin_enabled

ROOT = Path(__file__).resolve().parent.parent


def test_detects_enabled_legacy_claude_plugin(tmp_path):
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "enabledPlugins": {
            "claude-vault@claude-vault-marketplace": True,
            "context-vault@context-vault-marketplace": True,
        }
    }), encoding="utf-8")
    assert legacy_plugin_enabled("claude", home=tmp_path)


def test_disabled_legacy_claude_plugin_does_not_block(tmp_path):
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "enabledPlugins": {"claude-vault@market": False}
    }), encoding="utf-8")
    assert not legacy_plugin_enabled("claude", home=tmp_path)


def test_detects_enabled_legacy_codex_plugin(tmp_path):
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[plugins."claude-vault@claude-vault-marketplace"]\n'
        'enabled = true\n',
        encoding="utf-8",
    )
    assert legacy_plugin_enabled("codex", home=tmp_path)


def test_new_session_hook_yields_ownership_to_enabled_legacy_plugin(tmp_path):
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "enabledPlugins": {"claude-vault@legacy-market": True}
    }), encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(ROOT),
        "PYTHONUTF8": "1",
    })
    script = ROOT / "skills/vault-loader/scripts/session_start_load.py"
    payload = json.dumps({
        "cwd": str(tmp_path),
        "hook_event_name": "SessionStart",
        "session_id": "coexist-session",
    })
    proc = subprocess.run(
        [sys.executable, str(script)], input=payload, capture_output=True,
        text=True, encoding="utf-8", env=env, timeout=10,
    )
    output = json.loads(proc.stdout)
    assert "已暂停" in output["systemMessage"]
    assert "additionalContext" not in output.get("hookSpecificOutput", {})


def test_cwd_ancestor_settings_are_checked(tmp_path):
    """项目级（cwd 及其祖先）的 settings 必须被检查。

    既有四条用例全部只传 `home=`，把整段祖先遍历删掉也全绿——这条补上那个缺口。
    """
    from context_vault.coexist import check_legacy_plugin

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    project = tmp_path / "work" / "repo" / "sub"
    project.mkdir(parents=True)
    ancestor = tmp_path / "work" / "repo"
    (ancestor / ".claude").mkdir(parents=True)
    (ancestor / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"claude-vault@market": True}}), encoding="utf-8")

    assert check_legacy_plugin("claude", home=home, cwd=project).yield_ownership is True
    # 不传 cwd 时看不到项目级配置——反向确认上面那条命中的确实是祖先遍历
    assert check_legacy_plugin("claude", home=home).yield_ownership is False


def test_user_level_settings_local_is_checked(tmp_path):
    """用户级 `settings.local.json` 也要检查。

    漏掉它时，用户若在那里启用旧插件，共存检测完全看不见 ⇒ 两个插件同时注入，
    正是本机制要防的事。Windows 上 home 在 C:、项目在 D: 时，祖先遍历也覆盖不到它。
    """
    from context_vault.coexist import check_legacy_plugin

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.local.json").write_text(
        json.dumps({"enabledPlugins": {"claude-vault@market": True}}), encoding="utf-8")
    assert check_legacy_plugin("claude", home=home).yield_ownership is True


def test_unreadable_settings_does_not_yield_ownership(tmp_path):
    """读不出的配置不得被当成「旧插件已启用」。

    旧实现在此 `return True`：任意一层（常是手工编辑、被 gitignore 的
    settings.local.json）语法错误就能让整个插件停摆，还附一条事实错误的提示。
    Claude Code 自己也解析不了那份配置，其中的插件同样不会被它启用。
    """
    from context_vault.coexist import check_legacy_plugin

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{ this is not json", encoding="utf-8")

    result = check_legacy_plugin("claude", home=home)
    assert result.yield_ownership is False, "读不出 != 已启用"
    assert "无法解析" in result.note, "必须如实说明跳过了哪一层，而不是静默"


def test_ancestor_walk_is_depth_bounded(tmp_path):
    """祖先遍历要有深度上限，否则共享上层目录里的一个文件影响其下全部项目。"""
    from context_vault.coexist import _MAX_PARENTS, check_legacy_plugin

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    deep = tmp_path
    for i in range(_MAX_PARENTS + 3):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    far = tmp_path / ".claude"          # 超出上限的祖先
    far.mkdir(parents=True)
    (far / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"claude-vault@market": True}}), encoding="utf-8")

    assert check_legacy_plugin("claude", home=home, cwd=deep).yield_ownership is False


def test_codex_detection_works_without_tomllib(tmp_path, monkeypatch):
    """Python < 3.11 没有 tomllib 时必须降级而不是让整个模块 import 失败。

    硬依赖会连带把事件去重、共存检测、runtime 命名空间三项一起静默停用
    （macOS Xcode CLT 的 python3 是 3.9、Ubuntu 20.04 是 3.8，都很常见）。
    """
    from context_vault import coexist

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[plugins."claude-vault@market"]\nenabled = true\n', encoding="utf-8")

    monkeypatch.setattr(coexist, "tomllib", None)
    assert coexist.check_legacy_plugin("codex", home=home).yield_ownership is True

    (home / ".codex" / "config.toml").write_text(
        '[plugins."other@market"]\nenabled = true\n', encoding="utf-8")
    assert coexist.check_legacy_plugin("codex", home=home).yield_ownership is False
