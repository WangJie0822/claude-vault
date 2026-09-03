from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from context_vault.paths import canonical_config, default_vault, resolve_config_path
from context_vault.runtime import HookContext, RuntimeKind, detect_runtime

ROOT = Path(__file__).resolve().parent.parent


def _load_state_module():
    path = ROOT / "skills" / "vault-loader" / "scripts" / "_state.py"
    spec = importlib.util.spec_from_file_location("context_vault_state_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_detection_prefers_payload_over_compat_env():
    env = {"CLAUDE_PLUGIN_ROOT": "/compat"}
    # 两个 Codex 判据分别单独验：原用例把 turn_id 与 model 一起给，
    # 于是「model 单独能否判出 Codex」这一支从来没被走到过。
    assert detect_runtime({"turn_id": "turn-1"}, env) is RuntimeKind.CODEX
    assert detect_runtime({"model": "gpt"}, env) is RuntimeKind.CODEX
    assert detect_runtime({"prompt_id": "prompt-1"}, env) is RuntimeKind.CLAUDE


def test_claude_session_start_payload_is_not_unknown():
    """Claude SessionStart 的真实 payload 不得落 UNKNOWN。

    实测键集合（Claude Code 2.1.220）只有 cwd / hook_event_name / session_id /
    source / transcript_path —— 无 `model` 也无 `prompt_id`；而 `CLAUDE_PLUGIN_ROOT`
    **不在 hook 子进程环境里**（只在 hooks.json 命令串里被插值展开），故 env 传空。
    落 UNKNOWN 会让它与同会话 UPS（有 prompt_id ⇒ CLAUDE）的命名空间分裂。
    """
    session_start = {
        "cwd": "/x", "hook_event_name": "SessionStart", "session_id": "S1",
        "source": "startup", "transcript_path": "t",
    }
    assert detect_runtime(session_start, {}) is RuntimeKind.CLAUDE
    # Codex 的 SessionStart required 含 model ⇒ 仍须判 CODEX，兜底不得把它抢走
    assert detect_runtime({**session_start, "model": "gpt-5"}, {}) is RuntimeKind.CODEX
    # 兜底不能吞掉「真的无法判定」：没有 hook 形态、也没有 env 时仍是 UNKNOWN
    assert detect_runtime({}, {}) is RuntimeKind.UNKNOWN


def test_claude_session_start_and_prompt_share_one_state_namespace(monkeypatch, tmp_path):
    """同一 Claude 会话的两个 hook 必须落到同一个 state 文件。

    这是上一条的端到端后果：判定分裂时 SessionStart 写的注入去重集 UPS 读不到，
    启动时注入过的笔记会在第一次提问时被再注入一遍。
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
          "source": "startup", "transcript_path": "t"}
    ups = {"cwd": str(cwd), "hook_event_name": "UserPromptSubmit", "prompt": "q",
           "prompt_id": "P1", "session_id": "S1", "transcript_path": "t"}

    state.configure_context(detect_runtime(ss, {}).value, "S1")
    ss_path = state.state_path_for_cwd(cwd)
    state.configure_context(detect_runtime(ups, {}).value, "S1")
    ups_path = state.state_path_for_cwd(cwd)
    assert ss_path == ups_path, f"命名空间分裂：{ss_path} != {ups_path}"


def test_hook_context_normalizes_codex_event_id(tmp_path):
    context = HookContext.from_payload({
        "hook_event_name": "UserPromptSubmit",
        "cwd": str(tmp_path),
        "session_id": "thread-1",
        "turn_id": "turn-1",
        "model": "gpt-5",
        "prompt": "hello",
    })
    assert context.runtime is RuntimeKind.CODEX
    assert context.event_id == "turn-1"
    assert context.prompt_id is None
    assert context.stable_event_id is True


def test_fallback_event_ids_distinguish_prompts_and_are_transient():
    env = {"CLAUDE_PLUGIN_ROOT": "/plugin"}
    first = HookContext.from_payload({
        "hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "first",
    }, environ=env)
    second = HookContext.from_payload({
        "hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "second",
    }, environ=env)
    assert first.event_id != second.event_id
    assert first.stable_event_id is False


def test_fresh_config_uses_canonical_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path, fresh = resolve_config_path(tmp_path)
    assert fresh is True
    assert path == canonical_config(tmp_path)
    assert default_vault(tmp_path).parent == tmp_path / ".context-vault"


def test_state_isolated_by_runtime_and_project_not_session(monkeypatch, tmp_path):
    """state 按 runtime + cwd 隔离，**不按 session**。

    隔离要解决的是「两个 runtime 互相踩踏」，runtime 一层就够。再切一层 session 会
    让跨会话去重失效（同一批笔记每开一次会话重注入一遍，0.9.x 的行为是按 cwd），
    且每会话一个目录单调增长、无任何清理路径。
    """
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
    diag_a = state.diagnostics_path_for_cwd(cwd)
    state.configure_context("claude", "session-b")
    claude_b = state.state_path_for_cwd(cwd)
    diag_b = state.diagnostics_path_for_cwd(cwd)
    state.configure_context("codex", "session-a")
    codex_a = state.state_path_for_cwd(cwd)

    assert claude_a == claude_b, "同一 cwd 跨会话必须共用 state，否则注入去重失效"
    assert claude_a != codex_a, "两个 runtime 之间仍须隔离"
    assert claude_a != claude_other_project, "不同项目仍须隔离"
    assert diag_a == diag_b
    assert "state/claude/" in claude_a.as_posix()
    assert "state/codex/" in codex_a.as_posix()
    assert "sessions/" not in claude_a.as_posix(), "路径里不应再有 session 一层"


def test_legacy_state_namespace_survives_unknown_and_legacy_runtimes(monkeypatch, tmp_path):
    """**未迁移用户**必须保持 0.9.x state 路径。

    「未迁移用户」= 盘上有 0.9.x 数据、且没跑过迁移。所以场景必须真的造出 legacy
    数据——空 tmp_path 是「全新安装」，那种情况下走 canonical 才是对的，拿它当
    「未迁移」的对照会把正确实现判红。

    "legacy" 与 "unknown" 两个取值无条件归 legacy 布局；未知取值同样回落 legacy，
    而不是造出一个没人会读的孤儿命名空间。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    legacy_cfg = tmp_path / ".claude" / "skills" / "vault-loader"
    legacy_cfg.mkdir(parents=True)
    (legacy_cfg / "config.json").write_text("{}", encoding="utf-8")

    state = _load_state_module()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    legacy_tail = ".claude/projects"
    for runtime in ("legacy", "unknown", "claude", "wat"):
        state.configure_context(runtime, "S1")
        assert legacy_tail in state.state_path_for_cwd(cwd).as_posix(), runtime

    # 对照：全新安装（无 legacy 数据）下 claude 应走 canonical，否则本用例
    # 对「是否真的按 legacy 数据判定」没有判别力。
    fresh = tmp_path / "fresh-home"
    fresh.mkdir()
    monkeypatch.setenv("HOME", str(fresh))
    monkeypatch.setenv("USERPROFILE", str(fresh))
    state.configure_context("claude", "S1")
    assert "state/claude/" in state.state_path_for_cwd(cwd).as_posix()


def test_codex_manifest_has_required_components():
    manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "context-vault"
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "Context Vault"
    # `defaultPrompt` 用数组：本机 11 个真实 Codex 插件 manifest 无一例外都是数组。
    assert isinstance(manifest["interface"]["defaultPrompt"], list)
    # ⚠️ **不断言 `"hooks" not in manifest`。**
    # 「Codex 会自动发现 hooks/hooks.json」是一个**未经验证的推断**，不是已确认的契约。
    # 把它写成 assert 有两个害处：在拿到实测之前给人「已经验过」的错觉；一旦实测发现
    # 必须显式声明 hooks，这条用例会反过来**阻止正确的修法**。
    # 这里只要求「若声明了 hooks，形态得合法」，把假设留在注释里而不是断言里。
    if "hooks" in manifest:
        assert isinstance(manifest["hooks"], (str, dict))


def test_namespace_switch_requires_committed_migration(monkeypatch, tmp_path):
    """命名空间翻转必须以「迁移已提交」为判据，不能只看 canonical config 存不存在。

    `/summarize-session --set-default <path>` 只写 `~/.context-vault/config.json`、
    **不搬任何数据**。若拿 config 存在与否当判据，这个「改一下默认库路径」的动作
    就会把 state / metrics / session manifest 三处一起切到空目录：指标与不可再生的
    人工标注从报表消失、`--catch-up` 重列全部历史会话、注入去重重置，全程无提示。
    """
    from context_vault.paths import use_canonical_namespace

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    # 全新安装：没有 legacy 数据 ⇒ 直接用 canonical
    assert use_canonical_namespace(tmp_path) is True

    # 装过 0.9.x：只写 canonical config（相当于 --set-default）不得翻转
    legacy = tmp_path / ".claude" / "skills" / "vault-loader"
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text("{}", encoding="utf-8")
    canonical = tmp_path / ".context-vault" / "config.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text('{"_config_version": 2}', encoding="utf-8")
    assert use_canonical_namespace(tmp_path) is False, \
        "只写 canonical config 不得让命名空间翻转"

    # 真正跑完迁移后才翻转
    (tmp_path / ".context-vault" / "migration.json").write_text(
        '{"schema": 1, "status": "committed"}', encoding="utf-8")
    assert use_canonical_namespace(tmp_path) is True

    # state 侧同步生效（判据必须是同一个，不能各写各的）
    state = _load_state_module()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    state.configure_context("claude", "S1")
    assert "state/claude/" in state.state_path_for_cwd(cwd).as_posix()
