"""runtime 命名空间守卫（2.0.0 起只支持 Claude Code）。

由 `tests/test_dual_runtime.py` 拆出：其中与 Codex 无关的五条守卫迁入本文件（只把
`detect_runtime(...)` 换成 `HookContext.from_payload(...)`），Codex 判定用例随宿主识别
一并删除。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from context_vault.paths import canonical_config, default_vault, resolve_config_path
from context_vault.runtime import HookContext, RuntimeKind

ROOT = Path(__file__).resolve().parent.parent


def _load_state_module():
    path = ROOT / "skills" / "vault-loader" / "scripts" / "_state.py"
    spec = importlib.util.spec_from_file_location("context_vault_state_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_kind_has_only_claude():
    assert [kind.value for kind in RuntimeKind] == ["claude"]


@pytest.mark.parametrize("payload", [
    {"cwd": "/x", "hook_event_name": "SessionStart", "session_id": "S1", "source": "startup"},
    # 1.x 见到 `model` 就判 Codex。这是 Claude Code 2.1.293 上 SessionStart 被误判的候选
    # 成因之一（未坐实：`claude -p` 两次探针都没有 `model` 键），此处只钉住「带 `model`
    # 也必须判 claude」。
    {"cwd": "/x", "hook_event_name": "SessionStart", "session_id": "S1",
     "source": "startup", "model": "claude-opus-5-5"},
    {"hook_event_name": "UserPromptSubmit", "session_id": "S1", "prompt_id": "P1", "prompt": "q"},
    {"hook_event_name": "UserPromptSubmit", "session_id": "S1", "turn_id": "T1",
     "model": "m", "prompt": "q"},
    {},
])
def test_hook_context_is_always_claude(payload, monkeypatch):
    """payload 字段与环境变量 `PLUGIN_ROOT` 都不得再改变宿主判定。"""
    monkeypatch.setenv("PLUGIN_ROOT", "/somewhere-else")
    assert HookContext.from_payload(payload).runtime is RuntimeKind.CLAUDE


def test_turn_id_is_not_a_stable_event_id():
    """`turn_id` 是 Codex 的字段，2.0.0 起不再充当宿主下发的稳定事件 id。"""
    ctx = HookContext.from_payload({"hook_event_name": "UserPromptSubmit", "session_id": "s",
                                    "turn_id": "turn-1", "prompt": "hello"})
    assert ctx.stable_event_id is False
    assert ctx.event_id != "turn-1"


def test_metrics_event_id_is_blank_when_unstable():
    stable = HookContext.from_payload({"hook_event_name": "UserPromptSubmit",
                                       "session_id": "s", "prompt_id": "P1", "prompt": "q"})
    unstable = HookContext.from_payload({"hook_event_name": "UserPromptSubmit",
                                         "session_id": "s", "prompt": "q"})
    assert stable.metrics_event_id == "P1"
    assert unstable.event_id, "前提：不稳定 id 仍用于事件去重"
    assert unstable.metrics_event_id == "", "内容派生 id 不得写进 metrics"


def test_claude_session_start_and_prompt_share_one_state_namespace(monkeypatch, tmp_path):
    """两种 payload 经 `HookContext` + `_state.configure_context` 得到同一个 state 路径。

    只守路径函数这一层：同进程里两次 `configure_context` 的实参必然同为 "claude"，
    对**入口接线**没有判别力。入口接线（SessionStart 写、UPS 读同一份注入集）由
    `skills/vault-loader/tests/integration/test_session_start_injected_set.py` 的
    `test_ups_sees_session_start_injected_set` 用两个子进程守住。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    state = _load_state_module()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    config = canonical_config(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text('{"_config_version": 2}', encoding="utf-8")

    ss = {"cwd": str(cwd), "hook_event_name": "SessionStart", "session_id": "S1",
          "source": "startup", "transcript_path": "t", "model": "claude-opus-5-5"}
    ups = {"cwd": str(cwd), "hook_event_name": "UserPromptSubmit", "prompt": "q",
           "prompt_id": "P1", "session_id": "S1", "transcript_path": "t"}

    state.configure_context(HookContext.from_payload(ss).runtime.value, "S1")
    ss_path = state.state_path_for_cwd(cwd)
    state.configure_context(HookContext.from_payload(ups).runtime.value, "S1")
    ups_path = state.state_path_for_cwd(cwd)
    assert ss_path == ups_path, f"命名空间分裂：{ss_path} != {ups_path}"
    assert "state/claude/" in ss_path.as_posix()


def test_fallback_event_ids_distinguish_prompts_and_are_transient():
    first = HookContext.from_payload({
        "hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "first",
    })
    second = HookContext.from_payload({
        "hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "second",
    })
    assert first.event_id != second.event_id
    assert first.stable_event_id is False


def test_fresh_config_uses_canonical_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path, fresh = resolve_config_path(tmp_path)
    assert fresh is True
    assert path == canonical_config(tmp_path)
    assert default_vault(tmp_path).parent == tmp_path / ".context-vault"


def test_state_isolated_by_project_not_session(monkeypatch, tmp_path):
    """state 按 cwd 隔离、不按 session；退役的 codex 不得再造出命名空间。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    state = _load_state_module()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    other = tmp_path / "other-repo"
    other.mkdir()
    config = canonical_config(tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text('{"_config_version": 2}', encoding="utf-8")

    state.configure_context("claude", "session-a")
    claude_a = state.state_path_for_cwd(cwd)
    claude_other_project = state.state_path_for_cwd(other)
    state.configure_context("claude", "session-b")
    claude_b = state.state_path_for_cwd(cwd)
    state.configure_context("codex", "session-a")
    retired = state.state_path_for_cwd(cwd)

    assert claude_a == claude_b, "同一 cwd 跨会话必须共用 state，否则注入去重失效"
    assert claude_a != claude_other_project, "不同项目仍须隔离"
    assert "state/claude/" in claude_a.as_posix()
    assert "sessions/" not in claude_a.as_posix(), "路径里不应再有 session 一层"
    assert "state/codex/" not in retired.as_posix(), "codex 已不在白名单内，不得再造出它的命名空间"


def test_legacy_state_namespace_survives_unknown_and_legacy_runtimes(monkeypatch, tmp_path):
    """**未迁移用户**必须保持 0.9.x state 路径。

    场景必须真的造出 legacy 数据——空 tmp_path 是「全新安装」，那里走 canonical 才对。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    legacy_cfg = tmp_path / ".claude" / "skills" / "vault-loader"
    legacy_cfg.mkdir(parents=True)
    (legacy_cfg / "config.json").write_text("{}", encoding="utf-8")

    state = _load_state_module()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    for runtime in ("legacy", "unknown", "claude", "codex", "wat"):
        state.configure_context(runtime, "S1")
        assert ".claude/projects" in state.state_path_for_cwd(cwd).as_posix(), runtime

    fresh = tmp_path / "fresh-home"
    fresh.mkdir()
    monkeypatch.setenv("HOME", str(fresh))
    monkeypatch.setenv("USERPROFILE", str(fresh))
    state.configure_context("claude", "S1")
    assert "state/claude/" in state.state_path_for_cwd(cwd).as_posix()


def test_namespace_switch_requires_committed_migration(monkeypatch, tmp_path):
    """命名空间翻转必须以「迁移已提交」为判据，不能只看 canonical config 存不存在。"""
    from context_vault.paths import use_canonical_namespace

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert use_canonical_namespace(tmp_path) is True

    legacy = tmp_path / ".claude" / "skills" / "vault-loader"
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text("{}", encoding="utf-8")
    canonical = tmp_path / ".context-vault" / "config.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text('{"_config_version": 2}', encoding="utf-8")
    assert use_canonical_namespace(tmp_path) is False, "只写 canonical config 不得让命名空间翻转"

    (tmp_path / ".context-vault" / "migration.json").write_text(
        '{"schema": 1, "status": "committed"}', encoding="utf-8")
    assert use_canonical_namespace(tmp_path) is True

    state = _load_state_module()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state.configure_context("claude", "S1")
    assert "state/claude/" in state.state_path_for_cwd(cwd).as_posix()
