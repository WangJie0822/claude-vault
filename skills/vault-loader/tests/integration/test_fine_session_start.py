# -*- coding: utf-8 -*-
"""SessionStart 的精排接线（spec 2026-10-13 §7、§8.1、§9）：统一行、注入篇写进本会话已注入集合、
clear/compact 重置、过期清理。真子进程跑入口（与 test_mod_channel_ss.py 同形），fine 状态在测试进程里
按同一命名空间读。"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import _fine_state, _state
from scripts._fine_state import FineState
from scripts._inject_format import PATH_NOTE

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SS = SCRIPTS / "session_start_load.py"
# 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id（test_no_secrets_in_tree）
SID = "-".join(("11111111", "2222", "3333", "4444", "555555555555"))


def _config(home: Path, vault: Path, **extra) -> None:
    d = home / ".context-vault"
    d.mkdir(parents=True, exist_ok=True)
    cfg = {"vault_path": str(vault), "relevance": {"session_topic": False}}
    cfg.update(extra)
    (d / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _seed(vault: Path, wfc, project: str) -> str:
    rel = f"项目笔记/{project}/design.md"
    (vault / "项目笔记" / project).mkdir(parents=True, exist_ok=True)
    (vault / rel).write_text("# design", encoding="utf-8")
    wfc({rel: {"tags": ["设计"], "category": "项目笔记",
               "summary": "项目设计的取舍与模块划分说明，摘要足够长足够长", "mtime": 1900000000}})
    return rel


def _run(home: Path, cwd: Path, source: "str | None" = "startup") -> dict:
    """真子进程跑入口。`source=None` 表示 payload 里不带 source 键。"""
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                "HOME": str(home), "USERPROFILE": str(home)})
    env.pop("VAULT_LOADER_DISABLE", None)
    payload = {"cwd": str(cwd), "session_id": SID, "hook_event_name": "SessionStart"}
    if source is not None:
        payload["source"] = source
    r = subprocess.run([sys.executable, str(SS)], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else {}


def _in_ns(fn):
    _state.configure_context("claude", SID)
    try:
        return fn()
    finally:
        _state.configure_context("unknown")


def _fine() -> FineState:
    return _in_ns(lambda: _fine_state.load_fine(SID))


def _seed_fine(**fields) -> None:
    _in_ns(lambda: _fine_state.update_fine(SID, lambda st: replace(st, **fields)))


SUPP = {"gen_id": "g1", "src_seq": 3, "paths": ["笔记/x.md"], "ts": 1_800_000_000.0}


def test_ss_notes_recorded_as_injected(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    rel = _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    _run(tmp_home, tmp_git_repo)
    assert _fine().injected == [rel]


def test_clear_and_compact_reset_injected_and_supp(tmp_home, tmp_vault, tmp_git_repo,
                                                   write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    rel = _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    for source in ("clear", "compact"):
        _seed_fine(injected=["old.md"], supp=dict(SUPP), turn_seq=3)
        _run(tmp_home, tmp_git_repo, source=source)
        st = _fine()
        assert "old.md" not in st.injected and st.supp is None, source
        assert rel in st.injected and st.turn_seq == 3, source       # 只清这两项，序号不动


def test_resume_keeps_fine_state(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    _seed_fine(injected=["old.md"], supp=dict(SUPP))
    _run(tmp_home, tmp_git_repo, source="resume")
    st = _fine()
    assert "old.md" in st.injected and st.supp is not None


def test_dry_run_ss_writes_no_fine_state(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache):
    _config(tmp_home, tmp_vault, dry_run=True)
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    _run(tmp_home, tmp_git_repo)
    d = _in_ns(_fine_state.fine_dir)
    assert not d.exists() or not any(d.glob("*.json"))


def _setup_ss_disabled(tmp_home, tmp_vault, tmp_git_repo, wfc, **extra) -> FineState:
    """只关 SessionStart 注入（UPS 仍工作）；预置 injected / supp / turn_seq，返回预置后的归一化状态。"""
    _config(tmp_home, tmp_vault, session_start={"enabled": False}, **extra)
    _seed(tmp_vault, wfc, tmp_git_repo.name)       # 前提：没有闸门的话这篇会被注入
    _seed_fine(injected=["old.md"], supp=dict(SUPP), turn_seq=3)
    return _fine()


@pytest.mark.parametrize("source", ["clear", "compact"])
def test_ss_disabled_clear_compact_reset_fine_state_without_injecting(
        tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache, source):
    """只关 SessionStart 注入时 UPS 仍工作：/clear、/compact 清空了对话上下文，已注入集合与补充队列也得清，
    否则之前注入过的篇会被永远当成「已注入」。重置只清这两项，不写路径，也不产生注入输出。"""
    before = _setup_ss_disabled(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache)
    assert before.injected == ["old.md"] and before.supp is not None      # 前提：预置生效
    assert _run(tmp_home, tmp_git_repo, source=source) == {}              # 没有任何注入输出
    st = _fine()
    assert st.injected == [] and st.supp is None
    assert st.turn_seq == 3                                               # 序号不动


# 停用闸门先于一切副作用：ss 关时的「只重置」写盘必须排在 opt_out、项目 CLAUDE.md disable、旧插件让路
# 三道闸门之后。"none" 是对照——没有闸门挡着时同一夹具确实会写出状态文件，否则前三行的「无文件」恒真。
@pytest.mark.parametrize("gate,expect_file", [("opt_out", False), ("claude_md_disable", False),
                                              ("legacy_plugin", False), ("none", True)])
def test_ss_disabled_reset_respects_disable_gates(tmp_home, tmp_vault, tmp_git_repo,
                                                  write_frontmatter_cache, gate, expect_file):
    extra = {"opt_out_paths": [str(tmp_git_repo)]} if gate == "opt_out" else {}
    _config(tmp_home, tmp_vault, session_start={"enabled": False}, **extra)
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    if gate == "claude_md_disable":
        # 注释关键字运行时拼接：这份源文件若自带完整注释串，会被仓库里读项目指令文件的正则误命中
        (tmp_git_repo / "CLAUDE.md").write_text("<!-- vault-loader: " + "disable -->\n", encoding="utf-8")
    if gate == "legacy_plugin":
        (tmp_home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"claude-vault@market": True}}), encoding="utf-8")
    # 无输出：停用闸门命中是静默 return；ss 关 + 旧插件启用时也不应多出「已暂停」提示
    assert _run(tmp_home, tmp_git_repo, source="clear") == {}
    d = _in_ns(_fine_state.fine_dir)
    files = [p for p in d.rglob("*") if p.is_file()] if d.exists() else []
    assert bool(files) is expect_file, files


@pytest.mark.parametrize("source", ["bogus", None])
def test_unknown_or_missing_source_keeps_fine_state(tmp_home, tmp_vault, tmp_git_repo,
                                                    write_frontmatter_cache, source):
    """只有 clear / compact 才重置：宿主给了没见过的 source、或 payload 压根没有 source 键，都按 resume 处理。"""
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    _seed_fine(injected=["old.md"], supp=dict(SUPP))
    _run(tmp_home, tmp_git_repo, source=source)
    st = _fine()
    assert "old.md" in st.injected and st.supp is not None


@pytest.mark.parametrize("source", ["clear", "compact"])
def test_reset_happens_even_when_all_three_groups_empty(tmp_home, tmp_vault, tmp_path, source):
    """主路径上「项目笔记、工作日志、提交」三组全空会静默返回，重置必须排在这个早退之前：
    /clear 之后对话是空的，旧的已注入集合不能因为这次恰好没有可注入的内容而留下。"""
    plain = tmp_path / "plain"                       # 非 git 目录：没有提交；vault 里没有 cache：没有笔记与日志
    plain.mkdir()
    _config(tmp_home, tmp_vault)
    _seed_fine(injected=["old.md"], supp=dict(SUPP), turn_seq=3)
    assert _run(tmp_home, plain, source=source) == {}          # 前提：确实走到了三组全空的静默返回
    st = _fine()
    assert st.injected == [] and st.supp is None and st.turn_seq == 3


def test_ss_disabled_startup_keeps_fine_state(tmp_home, tmp_vault, tmp_git_repo,
                                              write_frontmatter_cache):
    before = _setup_ss_disabled(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache)
    assert _run(tmp_home, tmp_git_repo, source="startup") == {}
    assert _fine() == before


def test_ss_disabled_dry_run_clear_keeps_fine_state(tmp_home, tmp_vault, tmp_git_repo,
                                                    write_frontmatter_cache):
    """dry_run 是「预览、不落盘」：即使 source=clear 也不重置。"""
    before = _setup_ss_disabled(tmp_home, tmp_vault, tmp_git_repo, write_frontmatter_cache,
                                dry_run=True)
    assert _run(tmp_home, tmp_git_repo, source="clear") == {}
    assert _fine() == before


def test_ss_lines_carry_path_note_and_absolute_path(tmp_home, tmp_vault, tmp_git_repo,
                                                    write_frontmatter_cache):
    _config(tmp_home, tmp_vault)
    rel = _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    out = _run(tmp_home, tmp_git_repo)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert PATH_NOTE in ctx and "「design」" in ctx
    assert f"‖ 路径：{(tmp_vault / rel).resolve()}" in ctx
    assert f"[[{rel}]]" not in ctx


def test_cleanup_fine_runs_with_configured_ttl(tmp_home, tmp_vault, tmp_git_repo,
                                               write_frontmatter_cache, monkeypatch):
    """接线守卫：清理确实被调用、且用的是配置里的 state_ttl_hours（清理本身的判据由 Task 7 守）。"""
    from scripts import session_start_load as ssl
    _config(tmp_home, tmp_vault, user_prompt_submit={"state_ttl_hours": 12})
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    seen = []
    monkeypatch.setattr(_fine_state, "cleanup_fine", lambda ttl, now=None: seen.append(ttl) or 0)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.delenv("VAULT_LOADER_DISABLE", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"cwd": str(tmp_git_repo), "session_id": SID, "source": "startup",
         "hook_event_name": "SessionStart"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert ssl.main() == 0
    assert seen == [12.0]


def _inproc(monkeypatch, cwd: Path, source: str = "startup") -> io.StringIO:
    """进程内跑入口：下面两条补充用例用（上面几条沿用 brief 的内联写法）。返回捕获的 stdout。"""
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.delenv("VAULT_LOADER_DISABLE", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"cwd": str(cwd), "session_id": SID, "source": source, "hook_event_name": "SessionStart"})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    return out


def test_cleanup_fine_failure_does_not_break_injection(tmp_home, tmp_vault, tmp_git_repo,
                                                       write_frontmatter_cache, monkeypatch):
    """清理里的 `os.scandir` 会抛 OSError（Task 7 的提示）：调用处必须 fail-open，注入照常。"""
    from scripts import session_start_load as ssl
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)

    def boom(*a, **k):
        raise PermissionError("scandir")

    monkeypatch.setattr(_fine_state, "cleanup_fine", boom)
    out = _inproc(monkeypatch, tmp_git_repo)
    assert ssl.main() == 0
    assert "「design」" in json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("bad_ttl", [True, 0, -3, "abc"])
def test_cleanup_fine_bad_ttl_falls_back_to_24(tmp_home, tmp_vault, tmp_git_repo,
                                               write_frontmatter_cache, monkeypatch, bad_ttl):
    """配置里的 TTL 不是正数（bool / 0 / 负数 / 非数字）时回落 24 小时。尤其 `true`：不拦的话 float(True)
    是 1.0，会把 1 小时前的会话状态全清掉。"""
    from scripts import session_start_load as ssl
    _config(tmp_home, tmp_vault, user_prompt_submit={"state_ttl_hours": bad_ttl})
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    seen = []
    monkeypatch.setattr(_fine_state, "cleanup_fine", lambda ttl, now=None: seen.append(ttl) or 0)
    _inproc(monkeypatch, tmp_git_repo)
    assert ssl.main() == 0
    assert seen == [24.0]


def test_cleanup_fine_runs_even_when_loader_disabled(tmp_home, tmp_vault, tmp_git_repo,
                                                     write_frontmatter_cache, monkeypatch):
    """清理在 `enabled` 闸门之前（brief：不受「本项目关掉了注入」影响）：关掉注入也要清过期文件。"""
    from scripts import session_start_load as ssl
    _config(tmp_home, tmp_vault, enabled=False)
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)
    seen = []
    monkeypatch.setattr(_fine_state, "cleanup_fine", lambda ttl, now=None: seen.append(ttl) or 0)
    out = _inproc(monkeypatch, tmp_git_repo)
    assert ssl.main() == 0
    assert seen == [24.0]
    assert out.getvalue() == ""                       # 前提：确实被 enabled 闸门挡下、没注入


def test_fine_write_failure_does_not_break_injection(tmp_home, tmp_vault, tmp_git_repo,
                                                     write_frontmatter_cache, monkeypatch):
    from scripts import session_start_load as ssl
    _config(tmp_home, tmp_vault)
    _seed(tmp_vault, write_frontmatter_cache, tmp_git_repo.name)

    def boom(*a, **k):
        raise TimeoutError("lock")

    monkeypatch.setattr(_fine_state, "update_fine", boom)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.delenv("VAULT_LOADER_DISABLE", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"cwd": str(tmp_git_repo), "session_id": SID, "source": "clear",
         "hook_event_name": "SessionStart"})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert ssl.main() == 0
    assert "「design」" in json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
