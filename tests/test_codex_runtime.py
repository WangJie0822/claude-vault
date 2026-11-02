"""显式宿主与固定数据代际：真实文件、声明入口与跨宿主清理边界。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from context_vault import paths
from context_vault.coexist import check_legacy_plugin
from context_vault.events import claim_event
from context_vault.runtime import HookContext, RuntimeKind

ROOT = Path(__file__).resolve().parents[1]


def test_codex_host_is_explicit_and_turn_identity_is_stable(monkeypatch):
    monkeypatch.setenv("PLUGIN_ROOT", "/untrusted")
    payload = {"session_id": "thread-a", "thread_id": "thread-a", "turn_id": "turn-a",
               "hook_event_name": "UserPromptSubmit", "model": "untrusted", "prompt": "q"}
    ctx = HookContext.from_payload(payload, runtime="codex")
    assert ctx.runtime.value == "codex"
    assert ctx.event_id == "turn-a" and ctx.stable_event_id
    assert ctx.thread_id == "thread-a"
    assert ctx.actor_kind == "unknown"
    assert HookContext.from_payload(payload).runtime is RuntimeKind.CLAUDE
    other = HookContext.from_payload({**payload, "turn_id": "turn-b"}, runtime="codex")
    assert other.event_id != ctx.event_id


def test_missing_codex_identity_is_not_invented():
    ctx = HookContext.from_payload({}, runtime="codex")
    assert ctx.thread_id == ctx.root_session_id == ""
    assert ctx.actor_kind == "unknown" and not ctx.stable_event_id


def test_storage_root_maps_fixed_generations(tmp_path):
    resolve = getattr(paths, "storage_root", None)
    assert callable(resolve), "需要唯一的固定存储代际映射"
    for kind in ("state", "metrics"):
        assert resolve(tmp_path, host="codex", kind=kind) == tmp_path / ".context-vault" / kind / "codex" / "v2"
        assert resolve(tmp_path, host="claude", kind=kind) == tmp_path / ".context-vault" / kind / "claude"
    for host, kind in (("../x", "state"), ("codex", "../x")):
        with pytest.raises(ValueError):
            resolve(tmp_path, host=host, kind=kind)


def test_codex_event_claims_are_separate_from_claude(tmp_path):
    assert claim_event("codex", "UserPromptSubmit", "thread", "turn-1", home=tmp_path)
    assert not claim_event("codex", "UserPromptSubmit", "thread", "turn-1", home=tmp_path)
    assert claim_event("codex", "UserPromptSubmit", "thread", "turn-2", home=tmp_path)
    assert claim_event("claude", "UserPromptSubmit", "thread", "turn-1", home=tmp_path)
    marker_root = tmp_path / ".context-vault/state/codex/v2/events"
    assert len(list(marker_root.glob("*.json"))) == 1
    record = json.loads(next(marker_root.glob("*.json")).read_text(encoding="utf-8"))
    assert len(record["claims"]) == 2


def test_claude_legacy_ownership_does_not_pause_codex(tmp_path):
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"enabledPlugins": {"claude-vault@legacy": True}}), encoding="utf-8")
    assert check_legacy_plugin(home=tmp_path).yield_ownership
    assert not check_legacy_plugin(home=tmp_path, host="codex").yield_ownership


def _seed(home):
    cv = home / ".context-vault"
    cv.mkdir()
    (cv / "config.json").write_text("{}", encoding="utf-8")
    protected = []
    for folder, name in (("metrics/codex", ".salt"), ("metrics/codex/2026-10", "old.jsonl"),
                         ("metrics/claude/2026-10", "claude.jsonl"),
                         ("state/claude/fine", "b" * 24 + ".json"),
                         ("state/live", "c" * 24 + ".json")):
        p = cv / folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"protected": true}', encoding="utf-8")
        protected.append(p)
    root = cv / "metrics/codex/v2"
    (root / "2026-10").mkdir(parents=True)
    (root / ".salt").write_bytes(b"synthetic-salt-with-32-bytes-only!")
    (root / "2026-10/new.jsonl").write_text(json.dumps({"_schema": 2, "runtime": "codex", "session_id": "new"}) + "\n", encoding="utf-8")
    (root / "near_miss_counts.json").write_text('{"note.md": 3}', encoding="utf-8")
    fine = cv / "state/codex/v2/fine" / ("a" * 24 + ".json")
    fine.parent.mkdir(parents=True)
    fine.write_text("{}", encoding="utf-8")
    return protected, root, fine


@pytest.mark.parametrize("action", ["--report", "--reset-counts", "--purge"])
def test_codex_metrics_cli_is_confined_to_v2(tmp_path, action):
    protected, root, fine = _seed(tmp_path)
    before = {p: p.read_bytes() for p in protected}
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONUTF8="1")
    result = subprocess.run([sys.executable, str(ROOT / "skills/vault-loader/scripts/analyze_metrics.py"),
                             "--runtime", "codex", action], env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(p.read_bytes() == data for p, data in before.items())
    assert (root / ".salt").read_bytes() == b"synthetic-salt-with-32-bytes-only!"
    if action == "--purge":
        assert not (root / "2026-10/new.jsonl").exists()
        assert not fine.exists()
    elif action == "--reset-counts":
        assert not (root / "near_miss_counts.json").exists()
        assert fine.exists()
    else:
        assert (root / "2026-10/new.jsonl").exists()
        assert fine.exists()


def test_codex_purge_rejects_linked_generation_parent(tmp_path):
    cv = tmp_path / "home/.context-vault"
    (cv / "metrics").mkdir(parents=True)
    (cv / "config.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "elsewhere"
    keep = target / "v2/2026-10/keep.jsonl"
    keep.parent.mkdir(parents=True)
    keep.write_text('{"protected": true}', encoding="utf-8")
    link = cv / "metrics/codex"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(HOME=str(cv.parent), USERPROFILE=str(cv.parent), PYTHONUTF8="1")
    result = subprocess.run([sys.executable, str(ROOT / "skills/vault-loader/scripts/analyze_metrics.py"),
                            "--runtime", "codex", "--purge"], env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
    assert keep.exists(), "不得经 Codex 代际的父目录链接清理其他位置"
    assert keep.read_text(encoding="utf-8") == '{"protected": true}'
    assert result.returncode == 2 and "linked" in result.stderr


def test_codex_purge_cannot_cross_state_generation_link(tmp_path):
    protected, metrics, _fine = _seed(tmp_path)
    state = tmp_path / ".context-vault/state"
    # 独立于 metrics 的链接：预置 v2 已由 _seed 创建，只把空的 fine/v2 摘掉。
    _fine.unlink()
    _fine.parent.rmdir()
    (state / "codex/v2").rmdir()
    link = state / "codex/v2"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(state / "claude"), str(link))
    else:
        link.symlink_to(state / "claude", target_is_directory=True)
    claude_fine = next(p for p in protected if "fine" in p.parts)
    before = claude_fine.read_bytes()
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONUTF8="1")
    result = subprocess.run([sys.executable, str(ROOT / "skills/vault-loader/scripts/analyze_metrics.py"),
                            "--runtime", "codex", "--purge"], env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
    assert claude_fine.exists(), "Codex fine 清理越过状态代际链接，删到了 Claude"
    assert claude_fine.read_bytes() == before
    assert result.returncode == 2 and "linked" in result.stderr


def test_all_purge_keeps_full_fine_scope_after_metrics_dedup(tmp_path):
    _protected, _metrics, _fine = _seed(tmp_path)
    legacy_metrics = tmp_path / ".claude/vault-loader-metrics"
    legacy_metrics.mkdir(parents=True)
    legacy_fine = tmp_path / ".claude/vault-loader-fine" / ("d" * 24 + ".json")
    legacy_fine.parent.mkdir()
    legacy_fine.write_text("{}", encoding="utf-8")
    canonical_fine = tmp_path / ".context-vault/state/claude/fine" / ("b" * 24 + ".json")
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONUTF8="1")
    result = subprocess.run([sys.executable, str(ROOT / "skills/vault-loader/scripts/analyze_metrics.py"),
                            "--runtime", "all", "--purge"], env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not canonical_fine.exists(), "metrics 去重不能缩小 fine 的 all 清理范围"
    assert not legacy_fine.exists() and not _fine.exists()
