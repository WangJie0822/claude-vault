# -*- coding: utf-8 -*-
"""会话主题词的 state 读写。

搭现有 state 文件而非新建目录：CLAUDE.md 记过「切一层 session 会让目录单调增长
无清理」。故 topics 字典**限最近 5 个 session**，使文件大小有界。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest

from scripts._state import state_path_for_cwd
from scripts._topic import (MAX_TOPIC_SESSIONS, MAX_TOPIC_WORDS,
                            load_session_topic, save_session_topic,
                            has_recent_topic_attempt)


def test_roundtrip(tmp_path: Path) -> None:
    save_session_topic(tmp_path, "sess-A", ["召回", "打分", "闸门"])
    assert load_session_topic(tmp_path, "sess-A", 24) == ["召回", "打分", "闸门"]


def test_other_session_not_visible(tmp_path: Path) -> None:
    """主题是会话级的：A 的主题绝不能被 B 读到，否则并行会话互相污染。"""
    save_session_topic(tmp_path, "sess-A", ["召回"])
    assert load_session_topic(tmp_path, "sess-B", 24) == []


def test_keeps_only_most_recent_sessions(tmp_path: Path) -> None:
    """超出上限时按 ts 淘汰最旧的 —— 这是文件大小有界的唯一保证。"""
    for i in range(MAX_TOPIC_SESSIONS + 3):
        save_session_topic(tmp_path, f"s{i}", [f"w{i}"])
        time.sleep(0.01)          # 拉开 ts，避免同秒歧义
    data = json.loads(state_path_for_cwd(tmp_path).read_text(encoding="utf-8"))
    assert len(data["topics"]) == MAX_TOPIC_SESSIONS
    assert load_session_topic(tmp_path, "s0", 24) == [], "最旧的应被淘汰"
    assert load_session_topic(tmp_path, f"s{MAX_TOPIC_SESSIONS + 2}", 24) != []


def test_keeps_most_recent_when_same_ts(tmp_path: Path) -> None:
    """ts 相同时，后写入的 session 应胜出。这防止「同秒写入时保留最旧」的方向错误。"""
    frozen_ts = time.time()
    with mock.patch("scripts._topic.time.time", return_value=frozen_ts):
        for i in range(MAX_TOPIC_SESSIONS + 1):
            save_session_topic(tmp_path, f"s{i}", [f"w{i}"])
    # 最新写入的 s{MAX_TOPIC_SESSIONS} 应该留下
    assert load_session_topic(tmp_path, f"s{MAX_TOPIC_SESSIONS}", 24) != [], \
        f"最新的 s{MAX_TOPIC_SESSIONS} 应被保留"
    # 最旧的 s0 应该被踢
    assert load_session_topic(tmp_path, "s0", 24) == [], "最旧的 s0 应被淘汰"


def test_word_count_capped(tmp_path: Path) -> None:
    save_session_topic(tmp_path, "s", [f"w{i}" for i in range(50)])
    assert len(load_session_topic(tmp_path, "s", 24)) == MAX_TOPIC_WORDS


def test_ttl_expired_returns_empty(tmp_path: Path) -> None:
    save_session_topic(tmp_path, "s", ["召回"])
    p = state_path_for_cwd(tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["topics"]["s"]["ts"] = time.time() - 99 * 3600
    p.write_text(json.dumps(data), encoding="utf-8")
    assert load_session_topic(tmp_path, "s", 24) == []


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_session_topic(tmp_path, "s", 24) == []


# ---------------------------------------------------------------------------
# has_recent_topic_attempt（F2，整分支终审 2026-09-02）
# ---------------------------------------------------------------------------


def test_attempt_false_when_never_saved(tmp_path: Path) -> None:
    """从未提炼过（无 state 文件）：无尝试记录，允许 spawn。"""
    assert has_recent_topic_attempt(tmp_path, "s", 24) is False


def test_attempt_true_after_successful_save(tmp_path: Path) -> None:
    """成功提炼后：既有 topic_words 非空，也有尝试记录——二者应一致。"""
    save_session_topic(tmp_path, "s", ["召回", "打分"])
    assert has_recent_topic_attempt(tmp_path, "s", 24) is True


def test_attempt_true_after_failed_save(tmp_path: Path) -> None:
    """核心场景：提炼失败时词表为空，但 `load_session_topic` 与
    `has_recent_topic_attempt` 必须给出不同的答案——前者看不出"失败"与"从未尝试"
    的区别，后者才是 spawn 门禁真正要问的问题。"""
    save_session_topic(tmp_path, "s", [])
    assert load_session_topic(tmp_path, "s", 24) == [], "失败时词表确实为空"
    assert has_recent_topic_attempt(tmp_path, "s", 24) is True, (
        "失败也要被记为“已尝试过”，否则 spawn 门禁形同虚设")


def test_attempt_false_after_ttl_expired(tmp_path: Path) -> None:
    """尝试记录本身也受 TTL 约束：过期后视为"从未尝试"，允许重新 spawn
    （不会永久关掉这个功能）。"""
    save_session_topic(tmp_path, "s", [])
    p = state_path_for_cwd(tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["topics"]["s"]["ts"] = time.time() - 99 * 3600
    p.write_text(json.dumps(data), encoding="utf-8")
    assert has_recent_topic_attempt(tmp_path, "s", 24) is False


def test_attempt_isolated_per_session(tmp_path: Path) -> None:
    """会话隔离：A 失败过不得影响 B 的 spawn 门禁。"""
    save_session_topic(tmp_path, "sess-A", [])
    assert has_recent_topic_attempt(tmp_path, "sess-B", 24) is False


@pytest.mark.parametrize("body", ["{ 坏 json", "[]", '{"topics": "not-a-dict"}',
                                  '{"topics": {"s": "not-a-dict"}}'])
def test_attempt_corrupt_state_never_raises(tmp_path: Path, body: str) -> None:
    """损坏一律降级为 False（fail-open：允许尝试），绝不抛异常。"""
    p = state_path_for_cwd(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    assert has_recent_topic_attempt(tmp_path, "s", 24) is False


@pytest.mark.parametrize("body", ["{ 坏 json", "[]", '{"topics": "not-a-dict"}',
                                  '{"topics": {"s": "not-a-dict"}}',
                                  f'{{"topics": {{"s": {{"words": "not-a-list", "ts": {time.time()}}}}}}}'])
def test_corrupt_state_never_raises(tmp_path: Path, body: str) -> None:
    """损坏一律降级为空，绝不抛异常 —— hook fail-open 不变量。"""
    p = state_path_for_cwd(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    assert load_session_topic(tmp_path, "s", 24) == []


def test_save_does_not_clobber_injected_paths(tmp_path: Path) -> None:
    """写 topics 不得冲掉去重用的 paths —— 两者共用一个文件。"""
    from scripts._state import load_already_injected, save_injected
    save_injected(tmp_path, ["a.md", "b.md"])
    save_session_topic(tmp_path, "s", ["召回"])
    assert load_already_injected(tmp_path, 24) == {"a.md", "b.md"}
    assert load_session_topic(tmp_path, "s", 24) == ["召回"]


def test_save_uses_zero_timestamp_setdefault(tmp_path: Path) -> None:
    """F5（整分支终审，2026-09-02）：顶层 `timestamp` 字段必须走
    `setdefault("timestamp", 0)`，与既有两个写入方对齐——`_state.py::save_fallback_ts`
    与 `save_diag_ts` 都这么写，理由就在它们旁边：不得刷新 paths 的 timestamp，
    否则会变相续命注入去重 TTL。首次写入（无既有 state 文件）时 `save_session_topic`
    不应把顶层 timestamp 设成当前 epoch。"""
    save_session_topic(tmp_path, "s", ["召回"])
    data = json.loads(state_path_for_cwd(tmp_path).read_text(encoding="utf-8"))
    assert data["timestamp"] == 0, (
        f"顶层 timestamp 不应被 save_session_topic 刷新为当前时间，实际 {data['timestamp']}")


def test_save_respects_byte_limit(tmp_path: Path) -> None:
    """超过字节上限时，应裁 topics 保住 paths —— 防止整份 state 被重置。"""
    from scripts._state import load_already_injected, save_injected, MAX_STATE_BYTES
    # 先填满接近上限
    large_paths = [f"path_{i:04d}.md" for i in range(400)]  # ~8KB
    save_injected(tmp_path, large_paths)
    # 写一个很大的 topics（长列表），超过上限
    save_session_topic(tmp_path, "s", ["w" * 100 for _ in range(200)])
    # 检查文件大小是否合理（应被裁过）
    p = state_path_for_cwd(tmp_path)
    assert p.exists()
    size = p.stat().st_size
    assert size <= MAX_STATE_BYTES, f"文件大小 {size} 超过上限 {MAX_STATE_BYTES}"
    # 关键：paths 应该还在，topics 被裁
    paths = load_already_injected(tmp_path, 24)
    assert len(paths) > 0, "paths 不应被冲掉"


def test_long_words_do_not_overflow_state(tmp_path: Path) -> None:
    """超长词应被截断，防止 topics 体量溢出导致整份 state 被重置。

    触发场景：超长主题词（20000 字符）× MAX_TOPIC_SESSIONS + 2 个 session
    → 不裁则文件超限 → update_json 读取阶段置空 → paths 丢失

    正确修复：在源头限制词长，topics 永远有界 → 文件不超限 → paths 保住
    """
    from scripts._state import load_already_injected, save_injected

    # 先填充 paths 作为要保护的关键数据
    paths_to_save = [f"critical_path_{i}.md" for i in range(20)]
    save_injected(tmp_path, paths_to_save)

    # 灌入 MAX_TOPIC_SESSIONS + 2 个超长词的 session
    # 每个词 20000 字符，不裁则体量约 30 万字符，会溢出 102400 字节的上限
    long_word = "x" * 20000
    for i in range(MAX_TOPIC_SESSIONS + 2):
        save_session_topic(tmp_path, f"s{i}", [long_word])

    # 三个断言：① paths 要还在 ② 文件不超限 ③ 每个词被截断了
    from scripts._state import MAX_STATE_BYTES

    # ① paths 不能丢
    saved_paths = load_already_injected(tmp_path, 24)
    assert len(saved_paths) == len(paths_to_save), \
        f"paths 不应被冲掉：期望 {len(paths_to_save)} 条，实际 {len(saved_paths)}"

    # ② 文件不超限
    p = state_path_for_cwd(tmp_path)
    size = p.stat().st_size
    assert size <= MAX_STATE_BYTES, \
        f"state 文件超限：{size} > {MAX_STATE_BYTES}"

    # ③ 每个词被截断（长度应 ≤ 定义的上限）
    from scripts._topic import MAX_TOPIC_WORD_LEN
    for i in range(MAX_TOPIC_SESSIONS + 2):
        words = load_session_topic(tmp_path, f"s{i}", 24)
        if words:  # 可能被淘汰了，但如果还在就要检查长度
            for w in words:
                assert len(w) <= MAX_TOPIC_WORD_LEN, \
                    f"词长度 {len(w)} 超过上限 {MAX_TOPIC_WORD_LEN}"


@pytest.mark.parametrize("bad", [None, "str", 123, [1, 2], [""], [None]])
def test_malformed_words_never_raise(tmp_path: Path, bad) -> None:
    save_session_topic(tmp_path, "s", bad)
    assert isinstance(load_session_topic(tmp_path, "s", 24), list)


# ---------------------------------------------------------------------------
# spawn 与子进程
# ---------------------------------------------------------------------------
import subprocess

from scripts._topic import (build_topic_prompt, parse_topic_words,
                            spawn_topic_extraction)


def test_parse_topic_words_comma_separated() -> None:
    assert parse_topic_words("召回, 打分,闸门") == ["召回", "打分", "闸门"]


def test_parse_topic_words_strips_noise() -> None:
    """模型爱加解释性前缀/编号，必须剥掉后再用。"""
    assert parse_topic_words("关键词：召回、打分\n") == ["召回", "打分"]
    assert parse_topic_words("1. 召回\n2. 打分") == ["召回", "打分"]


@pytest.mark.parametrize("raw", ["", None, "   ", "\n\n"])
def test_parse_topic_words_empty(raw) -> None:
    assert parse_topic_words(raw) == []


def test_parse_topic_words_capped() -> None:
    assert len(parse_topic_words(",".join(f"w{i}" for i in range(50)))) == MAX_TOPIC_WORDS


def test_build_prompt_contains_candidates_and_prompt() -> None:
    p = build_topic_prompt("先修 fulltext 配额", [("a.md", "讲配额的笔记")])
    assert "先修 fulltext 配额" in p and "讲配额的笔记" in p


def test_spawn_returns_false_when_cli_missing(tmp_path: Path, monkeypatch) -> None:
    """没有 claude CLI 时静默返回 False，绝不抛异常。"""
    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: None)
    assert spawn_topic_extraction(tmp_path, "s", "prompt", [], {}) is False


def test_spawn_does_not_block(tmp_path: Path, monkeypatch) -> None:
    """承重守卫：spawn 必须立即返回。UPS 有 300ms 预算，而 LLM 要 21 秒。"""
    import time as time_mod
    started = {}

    class FakePopen:
        def __init__(self, *a, **kw):
            started["argv"] = a[0]
            started["kw"] = kw

        def poll(self):
            return None

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", FakePopen)
    t0 = time_mod.perf_counter()
    ok = spawn_topic_extraction(tmp_path, "s", "prompt", [("a.md", "x")], {})
    assert ok is True
    assert (time_mod.perf_counter() - t0) < 0.05, "spawn 阻塞了"
    # argv 应为 [python, -m, scripts._topic, cwd, session_id]——F4（终审 2026-09-02）
    # 后不再含 prompt/payload，见 test_spawn_sends_prompt_via_stdin_not_argv。
    assert "-m" in started["argv"] and "scripts._topic" in started["argv"], \
        f"argv 未固定：{started['argv']}"
    # kwargs 的 stdio：stdout/stderr 仍隔离；stdin 改为 PIPE（F4：内容改经 stdin 传）
    assert (started["kw"]["stdin"] == subprocess.PIPE and
            started["kw"]["stdout"] == subprocess.DEVNULL and
            started["kw"]["stderr"] == subprocess.DEVNULL), \
        f"stdio 未按预期设置：{started['kw']}"
    # cwd 应指向 skills/vault-loader（父路径）
    assert "vault-loader" in started["kw"]["cwd"], f"cwd 错误：{started['kw']['cwd']}"


def test_spawn_sends_prompt_via_stdin_not_argv(tmp_path: Path, monkeypatch) -> None:
    """F4（整分支终审，2026-09-02）：prompt 原文与候选笔记路径+摘要改走 stdin，
    不再放进 argv——argv 在本机进程表（`ps`/任务管理器）全程可见且无长度上限，
    与本项目 metrics 层"只存加盐 hash、刻意不落 transcript_path"的隐私口径不一致。
    """
    import io

    started = {}

    class FakeStdin(io.BytesIO):
        def close(self):
            started["stdin_bytes"] = self.getvalue()
            super().close()

    class FakePopen:
        def __init__(self, *a, **kw):
            started["argv"] = a[0]
            started["kw"] = kw
            self.stdin = FakeStdin()

        def poll(self):
            return None

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", FakePopen)
    ok = spawn_topic_extraction(tmp_path, "sess-X", "藏在prompt里的敏感内容",
                                [("a.md", "摘要A")], {})
    assert ok is True
    # argv 里不得出现 prompt 原文或候选摘要
    argv_joined = " ".join(str(a) for a in started["argv"])
    assert "藏在prompt里的敏感内容" not in argv_joined
    assert "摘要A" not in argv_joined
    # 真正的内容经 stdin 传递（JSON：{"prompt": ..., "candidates": [[path, summary], ...]}）
    payload = json.loads(started["stdin_bytes"].decode("utf-8"))
    assert payload["prompt"] == "藏在prompt里的敏感内容"
    assert payload["candidates"] == [["a.md", "摘要A"]]


def test_spawn_failure_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    def boom(*_a, **_kw):
        raise OSError("spawn failed")

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", boom)
    assert spawn_topic_extraction(tmp_path, "s", "prompt", [], {}) is False


def test_child_writes_topic(tmp_path: Path, monkeypatch) -> None:
    """子进程入口：拿到模型输出后写进 state。prompt/候选经 `stdin_text` 注入
    （F4 后 `run_extraction_child` 的 argv 只剩 cwd/session_id）。"""
    from scripts._topic import run_extraction_child
    monkeypatch.setattr("scripts._topic._call_model", lambda *_a, **_k: "召回, 打分")
    stdin_text = json.dumps({"prompt": "先修配额", "candidates": [["a.md", "摘要"]]})
    rc = run_extraction_child([str(tmp_path), "sess-C"], stdin_text=stdin_text)
    assert rc == 0
    assert load_session_topic(tmp_path, "sess-C", 24) == ["召回", "打分"]


def test_child_model_failure_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """模型失败时词表为空——但 F2（整分支终审，2026-09-02）要求仍要落一个
    带 ts 的失败标记，使 `has_recent_topic_attempt` 能感知"已尝试过"。"""
    from scripts._topic import run_extraction_child, has_recent_topic_attempt
    monkeypatch.setattr("scripts._topic._call_model", lambda *_a, **_k: None)
    stdin_text = json.dumps({"prompt": "p", "candidates": []})
    assert run_extraction_child([str(tmp_path), "sess-D"], stdin_text=stdin_text) == 0
    assert load_session_topic(tmp_path, "sess-D", 24) == []
    assert has_recent_topic_attempt(tmp_path, "sess-D", 24) is True, (
        "F2：失败也要落盘占位，否则每轮 UPS 都会重新拉起子进程")


def test_spawn_detach_on_windows(tmp_path: Path, monkeypatch) -> None:
    """Windows 上 detach 标志必须设置，进程不随父进程退出。"""
    import os as os_module
    started = {}

    class FakePopen:
        def __init__(self, *a, **kw):
            started["kw"] = kw

        def poll(self):
            return None

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", FakePopen)
    monkeypatch.setattr("scripts._topic.os.name", "nt")
    spawn_topic_extraction(tmp_path, "s", "p", [], {})
    assert "creationflags" in started["kw"], "Windows 上应设 creationflags"
    assert started["kw"]["creationflags"] == (
        subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    ), f"creationflags 值错误：{started['kw']['creationflags']}"


def test_spawn_detach_on_posix(tmp_path: Path, monkeypatch) -> None:
    """POSIX 上 start_new_session 必须设置，进程创建新 session。"""
    import os as os_module
    import sys as sys_module

    # 在 Windows 上此测试无意义（无法创建 PosixPath），故跳过
    if sys_module.platform.startswith("win"):
        pytest.skip("POSIX 测试在 Windows 上不可用")

    started = {}

    class FakePopen:
        def __init__(self, *a, **kw):
            started["kw"] = kw

        def poll(self):
            return None

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", FakePopen)
    monkeypatch.setattr("scripts._topic.os.name", "posix")
    spawn_topic_extraction(tmp_path, "s", "p", [], {})
    assert started["kw"]["start_new_session"] is True, \
        f"POSIX 上应设置 start_new_session=True，实际 {started['kw']}"


def test_parse_topic_words_perf_on_large_input() -> None:
    """性能守卫：100KB 输入应在 50ms 内完成解析（防 O(n²) 回归）。"""
    import time as time_mod
    # 构造足够大的输入：100KB 的重复词列表
    large_input = ", ".join(f"w{i % 1000}" for i in range(20000))
    t0 = time_mod.perf_counter()
    result = parse_topic_words(large_input)
    elapsed = time_mod.perf_counter() - t0
    # 应该在 50ms 内完成
    assert elapsed < 0.05, f"解析耗时过长：{elapsed:.3f}s，输入大小 {len(large_input)} bytes"
    # 结果应该是合理的
    assert len(result) == MAX_TOPIC_WORDS
    assert "w0" in result  # 至少包含开头的词


def test_parse_topic_words_strips_control_chars() -> None:
    """控制字符应被剥掉：ANSI 转义、NUL、C0/C1 控制符。"""
    # ANSI 颜色码
    assert parse_topic_words("\x1b[31m召回\x1b[0m") == ["召回"]
    # NUL 字节
    assert parse_topic_words("召回\x00打分") == ["召回", "打分"]
    # 其他控制符（C0）
    assert parse_topic_words("召回\x01\x02打分") == ["召回", "打分"]


def test_child_call_model_failure_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    """fail-open: _call_model 抛异常时 run_extraction_child 返回 0 不逃逸。

    F2（整分支终审，2026-09-02）：即使是这种"本不该发生"的内部异常（`_call_model`
    自身已用 try/except 包了一层、正常情况下只会返回 None），也要落一个失败标记——
    否则这条路径会绕开 F2 的负缓存，持续异常时仍是每轮都重新 spawn。`words` 仍为
    空列表（`load_session_topic` 看不出"失败也落盘"与"从未落盘"的区别），但
    `has_recent_topic_attempt` 必须能感知到"已尝试过"。
    """
    from scripts._topic import run_extraction_child, has_recent_topic_attempt
    monkeypatch.setattr("scripts._topic._call_model",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("model error")))
    stdin_text = json.dumps({"prompt": "p", "candidates": []})
    rc = run_extraction_child([str(tmp_path), "sess-E"], stdin_text=stdin_text)
    assert rc == 0, "model 异常时应返回 0"
    assert load_session_topic(tmp_path, "sess-E", 24) == [], "词表仍应为空"
    assert has_recent_topic_attempt(tmp_path, "sess-E", 24) is True, (
        "F2：内部异常也要落盘占位，否则持续异常时仍是每轮都重新 spawn")


def test_child_argv_length_insufficient_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    """fail-open: argv 长度不足时 run_extraction_child 返回 0 不逃逸。"""
    from scripts._topic import run_extraction_child
    monkeypatch.setattr("scripts._topic._call_model", lambda *_a, **_k: "召回")
    # 只传 1 个参数，而函数期望 2 个（cwd, session_id；F4 后 prompt/candidates 改经 stdin）
    rc = run_extraction_child([str(tmp_path)])
    assert rc == 0, "argv 不足时应返回 0"
