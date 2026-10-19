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
- 开关类断言改用 monkeypatch 拦截精排发起（2.4.0 起是 `spawn_fine`），直接断言调用次数与
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
    （如 `spawn_fine`）。subprocess 形态测不出「没有真实拉起子进程」
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
    # spare.md 不命中提问：本轮注入 quota.md 之后它是唯一的精排候选。2.4.0 的发起门禁是「排除后候选非空」
    # （spec 2026-10-13 F62），只有 quota.md 一篇时任何轮次都不发起，「不得发起」类断言恒真、成了空网。
    write_frontmatter_cache({
        "技术笔记/quota.md": {
            "tags": ["配额", "fulltext"], "category": "技术笔记",
            "summary": "fulltext 配额的实现与坑", "mtime": 1900000000,
        },
        "技术笔记/spare.md": {
            "tags": ["烹饪"], "category": "技术笔记",
            "summary": "红烧肉的做法与火候控制要点", "mtime": 1900000000,
        },
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "quota.md").write_text("# quota", encoding="utf-8")
    (tmp_vault / "技术笔记" / "spare.md").write_text("# spare", encoding="utf-8")
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault),
                               **extra}), encoding="utf-8")


def _patch_spawn_recorder(monkeypatch):
    """拦截精排发起 `spawn_fine(cwd, session_id, payload)`，记录每次调用的 (args, kwargs)。"""
    calls: list[tuple[tuple, dict]] = []

    def _fake(*args, **kwargs):
        calls.append((args, kwargs))
        return "spawned"

    monkeypatch.setattr("scripts.prompt_submit_load.spawn_fine", _fake)
    return calls


def test_enabled_by_default_spawns(tmp_home: Path, tmp_vault: Path,
                                   write_frontmatter_cache,
                                   monkeypatch, capsys):
    """1.1.0 起默认开：**空配置**（不写 relevance.session_topic）必须拉起提炼子进程。

    钉的是「DEFAULT_CONFIG 的默认值」这一条路径，而不是「显式传 True」——后者由
    test_session_topic_on_triggers_spawn_with_candidates 覆盖，把默认值改回 False
    它照样全绿，抓不到这次变更被回退。

    用拦截取代「读 state 文件里有没有 topics 键」——后者依赖真实子进程完成写盘，
    在隔离测试环境里恒不发生，见模块 docstring。
    """
    cwd = tmp_home.parent / "p-default-on"
    cwd.mkdir()
    _setup(tmp_home, tmp_vault, write_frontmatter_cache, {})
    calls = _patch_spawn_recorder(monkeypatch)
    rc, out, err = _run_main_inprocess(monkeypatch, capsys, cwd, "先修 fulltext 配额")
    assert rc == 0
    assert len(calls) == 1, "session_topic 默认开启时应恰好触发一次提炼"


def test_explicit_opt_out_blocks_spawn(tmp_home: Path, tmp_vault: Path,
                                       write_frontmatter_cache,
                                       monkeypatch, capsys):
    """显式 false 必须仍能关掉：默认值反转后，**关闭**这条路径是唯一的逃生阀。

    默认开之后若只把上面那条断言反向，关闭路径就再无覆盖——deep-merge 把显式值
    读丢、或接线改成无视配置，都不会有任何用例转红。
    """
    cwd = tmp_home.parent / "p-optout"
    cwd.mkdir()
    _setup(tmp_home, tmp_vault, write_frontmatter_cache,
           {"relevance": {"session_topic": False}})
    calls = _patch_spawn_recorder(monkeypatch)
    rc, out, err = _run_main_inprocess(monkeypatch, capsys, cwd, "先修 fulltext 配额")
    assert rc == 0
    assert calls == [], "显式 session_topic:false 时不得发起精排"


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
    # spawn_fine(cwd, session_id, payload)：候选改在子进程内构造（spec 2026-10-13 §6.3），父进程只交本轮输入
    assert args[0] == cwd and args[1] == "sess-T"
    payload = args[2]
    assert payload["mode"] == "pin" and payload["prompt"] == "先修 fulltext 配额"
    assert payload["src_seq"] == 1 and "技术笔记/quota.md" in payload["injected"]


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
    # 关逐轮补充：本用例走真实子进程入口，topics 已有词时补充模式会真起一条付费子进程
    _setup(tmp_home, tmp_vault, write_frontmatter_cache,
           {"relevance": {"session_topic": True, "fine_supplement": False}})
    # 2.4.0 统一行连短摘要也照出（附注「摘要缺失」），共用夹具的摘要里写着「配额」会让本断言误红；
    # 换一份不含主题词的摘要，tag 里的「配额」保留——主题词若折进提问词，仍会经「命中：」回显出来
    write_frontmatter_cache({
        "技术笔记/quota.md": {
            "tags": ["配额", "fulltext"], "category": "技术笔记",
            "summary": "fulltext 的实现与坑", "mtime": 1900000000,
        }
    })
    from scripts._topic import save_session_topic
    save_session_topic(cwd, "sess-T", ["配额"])
    r = _run(cwd, "先修 fulltext")
    assert r.returncode == 0
    ctx = (json.loads(r.stdout or "{}").get("hookSpecificOutput") or {}).get(
        "additionalContext", "")
    assert "配额" not in ctx, "主题词泄漏进了注入正文"


def test_broken_topic_state_does_not_break_injection(tmp_home: Path, tmp_vault: Path,
                                                     write_frontmatter_cache, monkeypatch):
    """fail-open：topics 结构损坏时照常注入。

    PATH 指向空目录：topics 读成空 ⇒ 本轮按首轮点名发起精排，真实子进程入口下不得真起付费的 claude。"""
    empty = tmp_home / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    cwd = tmp_home.parent / "p-broken"
    cwd.mkdir()
    _setup(tmp_home, tmp_vault, write_frontmatter_cache,
           {"relevance": {"session_topic": True}})
    # 2026-09-10：必须写进主题词自己的文件。此前这里写 state_path_for_cwd，
    # 而读路径改落点后根本不打开那个文件 —— 用例照常全绿却什么都没验证
    # （full-review 抓出的空网守卫；同批订正了 test_session_topic.py 六处，
    # 唯独漏了本文件）。
    from scripts._state import session_topics_path
    from scripts._topic import load_session_topic, save_session_topic
    p = session_topics_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)

    # 前置探针：先放一份**合法**数据并确认读得回来。
    # 没有这一步，本用例区分不了「读到了损坏数据仍 fail-open」与「压根没读到」——
    # 文件不存在时链路同样不挂，两种情形下 rc/stdout 完全一样。探针失败即说明
    # 写入路径不在读路径上（正是此前那个空网的形态）。
    save_session_topic(cwd, "sess-T", ["探针词"])
    assert load_session_topic(cwd, "sess-T", 24) == ["探针词"], \
        "写入路径不在读路径上，本用例退化为空网守卫"

    p.write_text('{"topics": "坏结构"}', encoding="utf-8")
    assert load_session_topic(cwd, "sess-T", 24) == [], "损坏数据应降级为空"
    r = _run(cwd, "先修 fulltext 配额")
    assert r.returncode == 0
    assert r.stdout.strip(), "损坏的 topics 让整条注入链路挂了"
