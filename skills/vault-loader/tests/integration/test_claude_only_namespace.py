"""2.0.0 起宿主恒为 Claude Code：SessionStart 的三处落点（state / metrics / events）
必须与 UPS 同在 claude 命名空间，且与 payload 字段、环境变量 `PLUGIN_ROOT` 无关。

1.x 的 `detect_runtime` 见到 payload 含 `model` 或环境里有 `PLUGIN_ROOT` 就判 Codex；
作者本机 `metrics/codex/` 下 185 条记录中 183 条是 session_start，涉及 156 个会话，
其中 117 个能在本机找到 Claude Code transcript。
必须真起子进程：命名空间是进程级全局，同进程用例对落点分裂结构性不可见。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SS_SCRIPT = SCRIPTS / "session_start_load.py"
UPS_SCRIPT = SCRIPTS / "prompt_submit_load.py"


def _run(script: Path, payload: dict, *, plugin_root_env: str | None = None
         ) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env["CLAUDE_CODE_ENTRYPOINT"] = "cli"     # 不设则在 `claude -p` 下跑测试时入口静默早退
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    if plugin_root_env is None:
        env.pop("PLUGIN_ROOT", None)
    else:
        env["PLUGIN_ROOT"] = plugin_root_env
    return subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload), capture_output=True,
        text=True, encoding="utf-8", env=env, timeout=60,
    )


def _write_canonical_config(home: Path, vault: Path, **extra) -> None:
    d = home / ".context-vault"
    d.mkdir(parents=True, exist_ok=True)
    # session_topic 默认开：不关会让每次 UPS 子进程拉起真实的 `claude -p` 提炼（付费）
    cfg = {"vault_path": str(vault), "metrics": {"enabled": True},
           "relevance": {"session_topic": False}, **extra}
    (d / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _seed_vault(vault: Path, write_frontmatter_cache, project: str) -> None:
    """给 SessionStart 造出可注入内容；无内容时它提前返回、不写 state 与 metrics。"""
    proj_dir = vault / "项目笔记" / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "design.md").write_text("# design", encoding="utf-8")
    write_frontmatter_cache({
        f"项目笔记/{project}/design.md": {
            "tags": ["设计"], "category": "项目笔记",
            "summary": "项目设计", "mtime": 1900000000,
        }
    })


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


def _ss_payload(repo: Path, session: str) -> dict:
    return {"cwd": str(repo), "hook_event_name": "SessionStart", "session_id": session,
            "source": "startup", "model": "claude-opus-5-5"}


def test_session_start_with_model_key_lands_in_claude_namespace(
        tmp_path, tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _write_canonical_config(tmp_home, tmp_vault)
    _seed_vault(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    stray = tmp_path / "stray-plugin-root"
    stray.mkdir()

    r = _run(SS_SCRIPT, _ss_payload(tmp_git_repo, "sess-claude-only"),
             plugin_root_env=str(stray))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), f"前提：本夹具下 SessionStart 应正常注入（stderr={r.stderr!r}）"

    cv = tmp_home / ".context-vault"
    assert _files(cv / "state" / "claude"), "注入去重 state 未落 claude 命名空间"
    assert _files(cv / "metrics" / "claude"), "metrics 未落 claude 命名空间"
    assert _files(cv / "state" / "events" / "claude"), "事件 marker 未落 claude 命名空间"
    leaked = [p for p in cv.rglob("*") if "codex" in p.relative_to(cv).parts]
    assert not leaked, f"写出了 codex 命名空间：{leaked}"


def test_runtimes_claude_disabled_stops_session_start(
        tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    """`runtimes.claude.enabled=false` 第一次能真正关掉 SessionStart（1.x 因错判关不掉）。"""
    _write_canonical_config(tmp_home, tmp_vault, runtimes={"claude": {"enabled": False}})
    _seed_vault(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    r = _run(SS_SCRIPT, _ss_payload(tmp_git_repo, "sess-disabled"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", f"关掉后不得注入：{r.stdout!r}"


def test_retired_codex_switch_does_not_disable_session_start(
        tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    """Review Focus 2：配置里残留的 `runtimes.codex.enabled=false` 已失效，不得关掉注入。"""
    _write_canonical_config(tmp_home, tmp_vault, runtimes={
        "claude": {"enabled": True}, "codex": {"enabled": False}})
    _seed_vault(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    r = _run(SS_SCRIPT, _ss_payload(tmp_git_repo, "sess-retired-switch"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), "残留的 codex 开关不得关掉 Claude 的 SessionStart"


def test_session_start_on_unmigrated_machine_stays_legacy(
        tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    """Review Focus 1：有 0.9.x 数据、未迁移的机器上，SessionStart 仍落 legacy（与 UPS 一致）。"""
    legacy_cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    legacy_cfg.parent.mkdir(parents=True, exist_ok=True)
    legacy_cfg.write_text(json.dumps({"vault_path": str(tmp_vault),
                                      "metrics": {"enabled": True}}), encoding="utf-8")
    _seed_vault(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    r = _run(SS_SCRIPT, _ss_payload(tmp_git_repo, "sess-unmigrated"))
    assert r.returncode == 0, r.stderr

    cv = tmp_home / ".context-vault"
    assert not _files(cv / "state" / "claude"), "未迁移机器的 state 不得落进 canonical"
    assert not _files(cv / "metrics" / "claude"), "未迁移机器的 metrics 不得落进 canonical"
    assert list((tmp_home / ".claude" / "projects").rglob("*.json")), "legacy state 未写出"


@pytest.mark.parametrize("script,event", [
    (SS_SCRIPT, "SessionStart"), (UPS_SCRIPT, "UserPromptSubmit"),
], ids=["session_start", "prompt_submit"])
def test_payload_without_event_name_is_never_claimed(
        script, event, tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    """缺 `hook_event_name` 的 payload 不进事件去重；带它时进（阳性对照）。两个入口都要验。"""
    _write_canonical_config(tmp_home, tmp_vault)
    _seed_vault(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    events = tmp_home / ".context-vault" / "state" / "events" / "claude"
    malformed = {"cwd": str(tmp_git_repo), "session_id": f"sess-malformed-{event}",
                 "source": "startup", "prompt": "项目设计 相关的历史决策"}
    for _ in range(2):
        assert _run(script, malformed).returncode == 0
    assert not [p for p in _files(events) if p.suffix == ".json"], \
        "缺 hook_event_name 的 payload 不得生成事件 marker"

    well_formed = {**malformed, "hook_event_name": event, "prompt_id": "pid-1"}
    assert _run(script, well_formed).returncode == 0
    assert [p for p in _files(events) if p.suffix == ".json"], "阳性对照：带事件名时应生成 marker"


def test_unstable_event_id_is_not_written_to_metrics(
        tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _write_canonical_config(tmp_home, tmp_vault)
    _seed_vault(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    base = {"cwd": str(tmp_git_repo), "hook_event_name": "UserPromptSubmit",
            "prompt": "项目设计 相关的历史决策"}
    assert _run(UPS_SCRIPT, {**base, "session_id": "sess-eid-unstable"}).returncode == 0
    assert _run(UPS_SCRIPT, {**base, "session_id": "sess-eid-stable",
                             "prompt_id": "pid-stable"}).returncode == 0

    records = {}
    for f in _files(tmp_home / ".context-vault" / "metrics" / "claude"):
        if f.suffix == ".jsonl" and f.parent.name[:2].isdigit():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    records.setdefault(rec.get("session"), []).append(rec)
    assert records.get("sess-eid-unstable"), "前提：不稳定 id 那一轮应写出 metrics 记录"
    assert all(rec.get("event_id", "") == "" for rec in records["sess-eid-unstable"])
    assert any(rec.get("event_id") == "pid-stable" for rec in records.get("sess-eid-stable", []))


def test_fixture_config_disables_paid_topic_extraction(tmp_home, tmp_vault):
    """full-review P3A-L6 / P2B-L2：本文件多次起 UPS 子进程，`relevance.session_topic` 默认开，
    PATH 上有 `claude` 时会拉起真实的 `claude -p` 提炼子进程；开发者环境带凭据就是一次付费调用。"""
    _write_canonical_config(tmp_home, tmp_vault, runtimes={"claude": {"enabled": True}})
    cfg = json.loads((tmp_home / ".context-vault" / "config.json").read_text(encoding="utf-8"))
    assert cfg.get("relevance", {}).get("session_topic") is False, cfg
