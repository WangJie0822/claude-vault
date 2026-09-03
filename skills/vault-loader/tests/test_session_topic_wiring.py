# -*- coding: utf-8 -*-
"""层 3 端到端接线：开关、注入正文纯净性、fail-open、dry-run 隔离。

评审订正（2026-09-02，四条 Finding）：原版 `test_disabled_by_default_no_topic_key`
与 `test_topic_words_never_appear_in_injection` 均是空网守卫——前者依赖「detached
子进程会在测试断言执行前完成并写 state」，后者用了一个不匹配 fixture 笔记任何字段
的哨兵词（「独特主题词ZZZ」），而渲染层只回显**实际命中笔记**的关键词。两个前提在
隔离测试环境里都不成立（子进程没有测试 tmp_home 下的 claude 登录凭据、会静默调用
失败；哨兵词永远不会出现在 `_hit_keywords()` 的返回值里），于是两条断言恒真，
对真实回归零判别力。

订正后的判据：
- 开关类断言改用 monkeypatch 拦截 `spawn_topic_extraction`，直接断言调用次数与
  参数，不依赖真实子进程是否完成。
- 泄漏类断言改用**会命中 fixture 笔记 tag 的词**（"配额"）作为主题词，让"折进
  prompt_keywords"这类真实回归必然被断言到（评审已用该词复现过一次真实泄漏：
  additionalContext 里出现过 "关键词命中：fulltext, 配额"）。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prompt_submit_load.py"


def _run(cwd: Path, prompt: str, session: str = "sess-T"):
    """subprocess 形态：用于不需要拦截内部函数的场景（fail-open / 真实命中回显）。"""
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    payload = {"cwd": str(cwd), "prompt": prompt,
               "session_id": session, "prompt_id": "pid-T"}
    return subprocess.run([sys.executable, str(SCRIPT)],
                          input=json.dumps(payload), capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=20)


def _run_main_inprocess(monkeypatch, capsys, cwd: Path, prompt: str,
                        session: str = "sess-T"):
    """在当前 pytest 进程内直接调 `main()`，允许 monkeypatch 拦截内部函数
    （如 `spawn_topic_extraction`）。subprocess 形态测不出「没有真实拉起子进程」
    这类否定式断言——detached 子进程在隔离测试环境里本就不会完成/落地，
    子进程是否被调用这件事必须在**父进程内**拦截才能观察到。
    """
    import scripts.prompt_submit_load as P
    payload = {"cwd": str(cwd), "prompt": prompt,
               "session_id": session, "prompt_id": "pid-T"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    rc = P.main()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _setup(tmp_home: Path, tmp_vault: Path, write_frontmatter_cache, extra: dict):
    write_frontmatter_cache({
        "技术笔记/quota.md": {
            "tags": ["配额", "fulltext"], "category": "技术笔记",
            "summary": "fulltext 配额的实现与坑", "mtime": 1900000000,
        }
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "quota.md").write_text("# quota", encoding="utf-8")
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault),
                               **extra}), encoding="utf-8")


def _patch_spawn_recorder(monkeypatch):
    """拦截 `spawn_topic_extraction`，记录每次调用的 (args, kwargs)。"""
    calls: list[tuple[tuple, dict]] = []

    def _fake(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr("scripts.prompt_submit_load.spawn_topic_extraction", _fake)
    return calls


def test_disabled_by_default_no_topic_gate(tmp_home: Path, tmp_vault: Path,
                                           write_frontmatter_cache,
                                           monkeypatch, capsys):
    """默认关：session_topic 未开启时，一次都不得拉起提炼子进程。

    用拦截取代「读 state 文件里有没有 topics 键」——后者依赖真实子进程完成写盘，
    在隔离测试环境里恒不发生，见模块 docstring。
    """
    cwd = tmp_home.parent / "p-off"
    cwd.mkdir()
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, {})
    calls = _patch_spawn_recorder(monkeypatch)
    rc, out, err = _run_main_inprocess(monkeypatch, capsys, cwd, "先修 fulltext 配额")
    assert rc == 0
    assert calls == [], "session_topic 默认关闭时不得调用 spawn_topic_extraction"


def test_dry_run_blocks_spawn(tmp_home: Path, tmp_vault: Path,
                              write_frontmatter_cache, monkeypatch, capsys):
    """Finding 1：dry_run=True 时即使 session_topic 开启，也不得真实拉起提炼子进程
    ——dry_run 的不变量是「灰度期只看会注入什么、不产生真实副作用」
    （SKILL.md 承诺"不真实注入"），spawn 会真实调用 LLM 与写盘，属真实副作用。
    """
    cwd = tmp_home.parent / "p-dryrun"
    cwd.mkdir()
    _setup(tmp_home, tmp_vault, write_frontmatter_cache,
           {"dry_run": True, "relevance": {"session_topic": True}})
    calls = _patch_spawn_recorder(monkeypatch)
    rc, out, err = _run_main_inprocess(monkeypatch, capsys, cwd, "先修 fulltext 配额")
    assert rc == 0
    assert calls == [], "dry_run 下不得真实拉起提炼子进程"


def test_session_topic_on_triggers_spawn_with_candidates(tmp_home: Path, tmp_vault: Path,
                                                          write_frontmatter_cache,
                                                          monkeypatch, capsys):
    """阳性对照：开关打开、非 dry_run、首轮命中笔记且尚无主题时，必须恰好触发一次
    提炼且候选非空——证明上面两条「零调用」断言不是因为 spawn 永远不会被调用
    （否则那两条断言会对任何实现都恒真，同样是空网）。
    """
    cwd = tmp_home.parent / "p-on"
    cwd.mkdir()
    _setup(tmp_home, tmp_vault, write_frontmatter_cache,
           {"relevance": {"session_topic": True}})
    calls = _patch_spawn_recorder(monkeypatch)
    rc, out, err = _run_main_inprocess(monkeypatch, capsys, cwd, "先修 fulltext 配额")
    assert rc == 0
    assert len(calls) == 1, "session_topic 开启且尚无主题时应恰好触发一次提炼"
    args, _kwargs = calls[0]
    # spawn_topic_extraction(cwd, session_id, prompt, cands, config)
    assert args[0] == cwd
    assert args[1] == "sess-T"
    assert args[2] == "先修 fulltext 配额"
    cands = args[3]
    assert cands, "候选列表不得为空"
    assert all(isinstance(c, tuple) and len(c) == 2 for c in cands)


def test_repeated_extraction_failure_does_not_spawn_every_round(
        tmp_home: Path, tmp_vault: Path, write_frontmatter_cache, monkeypatch, capsys):
    """F2（整分支终审，2026-09-02）：提炼持续失败时，第二轮 UPS 不得再次拉起子进程。

    模拟"上一轮提炼已失败"：直接调用 `save_session_topic(cwd, session_id, [])`——
    这正是 `run_extraction_child` 现在无论成功失败都会做的事（F2 修复：失败时
    `words` 为空列表，也落一个带当前 ts 的标记，而非此前的"什么都不写"）。第二轮
    同一 session 再次提交 prompt 时，`topic_words` 仍为空集合（未提炼出任何词），
    但 `has_recent_topic_attempt` 应为 True，spawn 不应再被调用。

    用 monkeypatch 拦截 `spawn_topic_extraction`，不依赖真实子进程完成——硬约束
    也要求不得真机调用 claude CLI。F2 修复前，本测试的 `calls` 恒为长度 1
    （每轮都重新 spawn，无上限）。
    """
    cwd = tmp_home.parent / "p-retry"
    cwd.mkdir()
    _setup(tmp_home, tmp_vault, write_frontmatter_cache,
           {"relevance": {"session_topic": True}})
    from scripts._topic import save_session_topic
    save_session_topic(cwd, "sess-T", [])   # 模拟上一轮提炼失败后的落盘标记

    calls = _patch_spawn_recorder(monkeypatch)
    rc, out, err = _run_main_inprocess(monkeypatch, capsys, cwd, "先修 fulltext 配额")
    assert rc == 0
    assert calls == [], (
        "提炼失败后、TTL 窗口内不得再次 spawn——F2 修复前这里恒为长度 1（每轮都重试）")


def test_topic_words_never_appear_in_injection(tmp_home: Path, tmp_vault: Path,
                                               write_frontmatter_cache):
    """承重守卫（spec §3.3.2 C5-1）：主题词只参与打分，绝不进注入正文。

    用**会命中 fixture 笔记 tag 的词**（"配额"）而非不相关哨兵词：渲染层只回显
    `_hit_keywords()` 实际命中笔记的关键词，不匹配任何字段的词无论是否泄漏都不会
    出现在 additionalContext 里，对"折进 prompt_keywords"这类真实回归零判别力
    （评审已用同一个词复现过真实泄漏）。prompt 故意不含"配额"，确保它只能经由
    主题词这一条路径进入。
    """
    cwd = tmp_home.parent / "p-inj"
    cwd.mkdir()
    _setup(tmp_home, tmp_vault, write_frontmatter_cache,
           {"relevance": {"session_topic": True}})
    from scripts._topic import save_session_topic
    save_session_topic(cwd, "sess-T", ["配额"])
    r = _run(cwd, "先修 fulltext")
    assert r.returncode == 0
    ctx = (json.loads(r.stdout or "{}").get("hookSpecificOutput") or {}).get(
        "additionalContext", "")
    assert "配额" not in ctx, "主题词泄漏进了注入正文"


def test_broken_topic_state_does_not_break_injection(tmp_home: Path, tmp_vault: Path,
                                                     write_frontmatter_cache):
    """fail-open：topics 结构损坏时照常注入。"""
    cwd = tmp_home.parent / "p-broken"
    cwd.mkdir()
    _setup(tmp_home, tmp_vault, write_frontmatter_cache,
           {"relevance": {"session_topic": True}})
    from scripts._state import state_path_for_cwd
    p = state_path_for_cwd(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"topics": "坏结构"}', encoding="utf-8")
    r = _run(cwd, "先修 fulltext 配额")
    assert r.returncode == 0
    assert r.stdout.strip(), "损坏的 topics 让整条注入链路挂了"
