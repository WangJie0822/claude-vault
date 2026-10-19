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

from scripts._state import session_topics_path, state_path_for_cwd
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
    data = json.loads(session_topics_path(tmp_path).read_text(encoding="utf-8"))
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
    p = session_topics_path(tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["topics"]["s"]["ts"] = time.time() - 99 * 3600
    p.write_text(json.dumps(data), encoding="utf-8")
    assert load_session_topic(tmp_path, "s", 24) == []


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_session_topic(tmp_path, "s", 24) == []


# ---------------------------------------------------------------------------
# has_recent_topic_attempt（F2，整分支终审 2026-09-02；2.4.0 起不是 spawn 门禁，只判 topics entry 是否存在）
# ---------------------------------------------------------------------------


def test_attempt_false_when_never_saved(tmp_path: Path) -> None:
    """从未提炼过（无 state 文件）：没有 topics entry。"""
    assert has_recent_topic_attempt(tmp_path, "s", 24) is False


def test_attempt_true_after_successful_save(tmp_path: Path) -> None:
    """成功提炼后：既有 topic_words 非空，也有尝试记录——二者应一致。"""
    save_session_topic(tmp_path, "s", ["召回", "打分"])
    assert has_recent_topic_attempt(tmp_path, "s", 24) is True


def test_attempt_true_after_failed_save(tmp_path: Path) -> None:
    """核心区别：entry 的词表为空时，`load_session_topic` 与 `has_recent_topic_attempt`
    必须给出不同的答案——前者看不出"entry 无词"与"没有 entry"的区别，后者只问 entry 是否存在。
    （1.x 里失败也落一个空词表 entry、本函数据此做 spawn 门禁；2.4.0 起失败不再写 topics，
    门禁也不再看本函数，这里用空词表 entry 模拟「有 entry 无词」。）"""
    save_session_topic(tmp_path, "s", [])
    assert load_session_topic(tmp_path, "s", 24) == [], "失败时词表确实为空"
    assert has_recent_topic_attempt(tmp_path, "s", 24) is True, (
        "词表为空的 entry 也要被识别为“有 entry”")


def test_attempt_false_after_ttl_expired(tmp_path: Path) -> None:
    """entry 也受 TTL 约束：过期后视为没有 entry。"""
    save_session_topic(tmp_path, "s", [])
    p = session_topics_path(tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["topics"]["s"]["ts"] = time.time() - 99 * 3600
    p.write_text(json.dumps(data), encoding="utf-8")
    assert has_recent_topic_attempt(tmp_path, "s", 24) is False


def test_attempt_isolated_per_session(tmp_path: Path) -> None:
    """会话隔离：A 的 entry 不得被 B 读到。"""
    save_session_topic(tmp_path, "sess-A", [])
    assert has_recent_topic_attempt(tmp_path, "sess-B", 24) is False


@pytest.mark.parametrize("body", ["{ 坏 json", "[]", '{"topics": "not-a-dict"}',
                                  '{"topics": {"s": "not-a-dict"}}'])
def test_attempt_corrupt_state_never_raises(tmp_path: Path, body: str) -> None:
    """损坏一律降级为 False，绝不抛异常。"""
    p = session_topics_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    assert has_recent_topic_attempt(tmp_path, "s", 24) is False


@pytest.mark.parametrize("body", ["{ 坏 json", "[]", '{"topics": "not-a-dict"}',
                                  '{"topics": {"s": "not-a-dict"}}',
                                  f'{{"topics": {{"s": {{"words": "not-a-list", "ts": {time.time()}}}}}}}'])
def test_corrupt_state_never_raises(tmp_path: Path, body: str) -> None:
    """损坏一律降级为空，绝不抛异常 —— hook fail-open 不变量。"""
    p = session_topics_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    assert load_session_topic(tmp_path, "s", 24) == []


def test_save_does_not_clobber_injected_paths(tmp_path: Path) -> None:
    """写 topics 不得冲掉去重用的 paths。

    2026-09-10 起两者**不再共用一个文件**（topics 按项目根、paths 按 cwd），
    所以这条用例的原判别力已经消失——分家之后互不冲掉是平凡成立的。
    补上「确实分属两个文件」的断言，它才重新对「把 topics 落点改回
    `state_path_for_cwd`」这个变异有判别力。
    """
    from scripts._state import load_already_injected, save_injected
    save_injected(tmp_path, ["a.md", "b.md"])
    save_session_topic(tmp_path, "s", ["召回"])
    assert load_already_injected(tmp_path, 24) == {"a.md", "b.md"}
    assert load_session_topic(tmp_path, "s", 24) == ["召回"]
    assert session_topics_path(tmp_path) != state_path_for_cwd(tmp_path), \
        "topics 与注入去重必须分属两个文件，否则本用例退化为平凡断言"
    assert "topics" not in json.loads(
        state_path_for_cwd(tmp_path).read_text(encoding="utf-8")), \
        "state.json 不应再出现 topics 字段"


def test_save_never_touches_injection_state_file(tmp_path: Path) -> None:
    """`save_session_topic` 不得碰注入去重的 state 文件。

    这条取代了原 `test_save_uses_zero_timestamp_setdefault`（F5，2026-09-02）。
    原契约是「顶层 `timestamp` 必须 setdefault 成 0」，理由是不得刷新 paths 的
    TTL 而变相续命注入去重。2026-09-10 topics 迁到独立文件后，该约定的**前提**
    （与 paths 同住一个文件）已不存在，`setdefault` 那行也随之删掉——若仍断言
    `data["timestamp"] == 0` 就是在为一个不再成立的理由做守卫。

    新契约更强也更直接：**根本不碰那个文件**。它同时守住了原来要防的东西
    （碰不到就谈不上续命 TTL），且对「落点改回 state_path_for_cwd」这一变异
    直接转红。
    """
    save_session_topic(tmp_path, "s", ["召回"])
    assert not state_path_for_cwd(tmp_path).exists(), \
        "save_session_topic 不应创建/写入注入去重的 state 文件"
    data = json.loads(session_topics_path(tmp_path).read_text(encoding="utf-8"))
    assert "timestamp" not in data, \
        "topics 文件里没有 paths，不应再有那个为 paths TTL 而设的 timestamp 字段"


def test_save_respects_byte_limit(tmp_path: Path) -> None:
    """超过字节上限时，应裁 topics 保住 paths —— 防止整份 state 被重置。"""
    from scripts._state import load_already_injected, save_injected, MAX_STATE_BYTES
    # 先填满接近上限
    large_paths = [f"path_{i:04d}.md" for i in range(400)]  # ~8KB
    save_injected(tmp_path, large_paths)
    # 写一个很大的 topics（长列表），超过上限
    save_session_topic(tmp_path, "s", ["w" * 100 for _ in range(200)])
    # 检查文件大小是否合理（应被裁过）
    # 大小上限现在要看 topics 自己的文件——分家后它不再写进 state.json，
    # 继续量 state.json 等于量一个与本用例无关的对象。
    p = session_topics_path(tmp_path)
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

    # ② 文件不超限（同上：分家后要量 topics 文件，不是 state.json）
    p = session_topics_path(tmp_path)
    size = p.stat().st_size
    assert size <= MAX_STATE_BYTES, \
        f"topics 文件超限：{size} > {MAX_STATE_BYTES}"

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
# spawn 与子进程（2.4.0：spawn_fine；子进程主体的用例在 test_fine_child.py / test_pin_child.py，
# 模板与解析在 test_fine_select.py，_call_model 在 test_fine_call_model.py）
# ---------------------------------------------------------------------------
import subprocess

from scripts import _topic
from scripts._topic import parse_topic_words, spawn_fine

PAYLOAD = {"v": 1, "mode": "pin", "gen_id": "0123456789abcdef", "src_seq": 1, "slot": None,
           "prompt": "藏在prompt里的敏感内容", "transcript_path": "", "vault_path": "",
           "cfg": {}, "injected": ["笔记/摘要A.md"], "excluded": [], "prev_words": [], "extra_paths": []}


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


def test_spawn_returns_no_claude_when_cli_missing(tmp_path: Path, monkeypatch) -> None:
    """没有 claude CLI 时静默返回 no_claude，绝不抛异常。"""
    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: None)
    assert spawn_fine(tmp_path, "s", PAYLOAD) == "no_claude"


class FakePopen:
    started: dict = {}

    def __init__(self, *a, **kw):
        # stdin 是父进程写好、回到开头的临时文件：在 Popen 时刻读出来，核对子进程会读到什么
        stdin = kw.get("stdin")
        FakePopen.started = {"argv": a[0], "kw": kw,
                             "stdin_bytes": stdin.read() if hasattr(stdin, "read") else None}
        self.stdin = mock.MagicMock()

    def poll(self):
        return None


def test_spawn_does_not_block(tmp_path: Path, monkeypatch) -> None:
    """承重守卫：spawn 必须立即返回（UPS 有 300 ms 预算，精排一次约 10 s）。

    阈值 1.0s 的依据（本机实测，各 n=30，FakePopen 不起真进程）：健康态 spawn（当时还含 in-flight 写）
    median 12.3ms / max 39.5ms；阻塞态（真等子进程跑完 LLM）中位 21s。两侧几何中心约 1.24s，取 1.0s。
    2.4.0 起 spawn_fine 不再写 topics 占位，健康态只会更快；阈值不收紧——它要抓的是「改成等子进程」，
    那是 10 s 量级。改阈值前请先重测两个区间。
    """
    import time as time_mod
    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", FakePopen)
    t0 = time_mod.perf_counter()
    assert spawn_fine(tmp_path, "s", PAYLOAD) == "spawned"
    elapsed = time_mod.perf_counter() - t0
    assert elapsed < 1.0, f"spawn 阻塞了：{elapsed*1000:.1f}ms"
    started = FakePopen.started
    assert "-m" in started["argv"] and "scripts._topic" in started["argv"], f"argv 未固定：{started['argv']}"
    # stdin 是写好的临时文件而不是管道（P-H1：管道交接在 payload 超过 4096 B 时要等子进程读）
    assert started["kw"]["stdin"] is not subprocess.PIPE and hasattr(started["kw"]["stdin"], "fileno"), (
        f"stdin 应是写好的临时文件：{started['kw']}")
    assert started["stdin_bytes"] == json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")
    assert (started["kw"]["stdout"] == subprocess.DEVNULL
            and started["kw"]["stderr"] == subprocess.DEVNULL), f"stdio 未按预期设置：{started['kw']}"
    # cwd 必须恰为插件内的 skills/vault-loader：_call_model 在精排子进程里用裸 shutil.which("claude")
    # 后直接执行，它不搜用户项目目录的唯一前提就是这个 cwd（full-review P1-M1）
    assert Path(started["kw"]["cwd"]) == Path(_topic.__file__).resolve().parents[1]


def test_spawn_sends_payload_via_stdin_not_argv(tmp_path: Path, monkeypatch) -> None:
    """F4：prompt 原文与本会话的 path 经 stdin 传，不进 argv（进程表全程可见、无长度上限）。"""
    got = {}

    class _P:
        def __init__(self, *a, **kw):
            got["argv"] = a[0]
            got["stdin"] = kw["stdin"].read()        # 父进程写好、回到开头的临时文件

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", _P)
    assert spawn_fine(tmp_path, "sess-X", PAYLOAD) == "spawned"
    argv_joined = " ".join(str(a) for a in got["argv"])
    assert "藏在prompt里的敏感内容" not in argv_joined and "摘要A" not in argv_joined
    assert json.loads(got["stdin"].decode("utf-8")) == PAYLOAD


# 替身子进程：先睡 1.2 s 再读 stdin，读到的全部字节原子写到 argv[1]
_STANDIN_CHILD = "\n".join([
    "import os, sys, time",
    "time.sleep(1.2)",
    "data = sys.stdin.buffer.read()",
    "out = sys.argv[1]",
    "with open(out + '.part', 'wb') as f:",
    "    f.write(data)",
    "os.replace(out + '.part', out)",
])


def _wait_for(pred, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_spawn_handoff_does_not_wait_for_child_to_read_stdin(tmp_path: Path, monkeypatch) -> None:
    """P-H1 判别式：payload 远超管道缓冲时，`spawn_fine` 也不等子进程开始读 stdin 就返回，且子进程读到的字节
    与 payload 逐字节相同，临时文件不残留。

    子进程换成一个先睡 1.2 s 再读 stdin 的替身（只替换 argv，stdin、标志、cwd 照原样交给真实 `Popen`）。
    交接若还是「管道 + 同步写」，写满缓冲后要等替身睡醒开始读，耗时 ≥ 1.2 s；交接是写好的临时文件时只剩
    `Popen` 本身（本机健康态约 0–10 ms，P-H1 实测）。阈值 0.5 s 取在两者之间。payload 取 128 KiB：Windows
    匿名管道缓冲 4096 B，Linux / macOS 默认 64 KiB，三个平台上回退成管道都会阻塞。
    """
    import sys as sys_mod
    import tempfile

    stub = tmp_path / "standin_child.py"
    stub.write_text(_STANDIN_CHILD + "\n", encoding="utf-8")
    out = tmp_path / "received.bin"
    tmpdir = tmp_path / "tempdir"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))      # 临时文件落在这里，便于查残留
    real_popen = subprocess.Popen
    seen: list = []

    def _standin_popen(argv, **kw):
        seen.append(list(argv))
        return real_popen([sys_mod.executable, str(stub), str(out)], **kw)

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", _standin_popen)
    payload = dict(PAYLOAD, prompt="x" * (128 * 1024))
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert len(data) > 64 * 1024

    t0 = time.perf_counter()
    outcome = spawn_fine(tmp_path, "sess-handoff", payload)
    elapsed = time.perf_counter() - t0

    assert outcome == "spawned"
    assert len(seen) == 1 and seen[0][1:3] == ["-m", "scripts._topic"], seen   # 替换的确是精排子进程
    assert elapsed < 0.5, f"spawn_fine 等了子进程读 stdin：{elapsed * 1000:.0f} ms"
    assert _wait_for(out.exists), "替身子进程 60 s 内没有写出收到的字节"
    assert out.read_bytes() == data
    assert _wait_for(lambda: not any(tmpdir.iterdir())), f"临时文件残留：{list(tmpdir.iterdir())}"


def test_spawn_failure_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    def boom(*_a, **_kw):
        raise OSError("spawn failed")

    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", boom)
    assert spawn_fine(tmp_path, "s", PAYLOAD) == "spawn_error"
    assert not session_topics_path(tmp_path).exists()


def test_spawn_fine_writes_no_topics_placeholder(tmp_path: Path, monkeypatch) -> None:
    """F01/F02：spawn 不再向 topics 写 pending 占位；在飞与否由调用方的在飞标记表示。"""
    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", FakePopen)
    assert spawn_fine(tmp_path, "sess-inflight", PAYLOAD) == "spawned"
    assert not session_topics_path(tmp_path).exists()
    assert has_recent_topic_attempt(tmp_path, "sess-inflight", 24) is False


def test_spawn_detach_on_windows(tmp_path: Path, monkeypatch) -> None:
    """Windows 上 detach 标志必须设置，进程不随父进程退出。"""
    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", FakePopen)
    monkeypatch.setattr("scripts._topic.os.name", "nt")
    spawn_fine(tmp_path, "s", PAYLOAD)
    assert FakePopen.started["kw"]["creationflags"] == (
        subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)


def test_spawn_detach_on_posix(tmp_path: Path, monkeypatch) -> None:
    """POSIX 上 start_new_session 必须设置，进程创建新 session。"""
    import sys as sys_module
    if sys_module.platform.startswith("win"):
        pytest.skip("POSIX 测试在 Windows 上不可用")
    monkeypatch.setattr("scripts._topic.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr("scripts._topic.subprocess.Popen", FakePopen)
    monkeypatch.setattr("scripts._topic.os.name", "posix")
    spawn_fine(tmp_path, "s", PAYLOAD)
    assert FakePopen.started["kw"]["start_new_session"] is True


def test_parse_topic_words_perf_on_large_input() -> None:
    """性能守卫：100KB 输入应在 50ms 内完成解析（防 O(n²) 回归）。"""
    import time as time_mod
    large_input = ", ".join(f"w{i % 1000}" for i in range(20000))
    t0 = time_mod.perf_counter()
    result = parse_topic_words(large_input)
    elapsed = time_mod.perf_counter() - t0
    assert elapsed < 0.05, f"解析耗时过长：{elapsed:.3f}s，输入大小 {len(large_input)} bytes"
    assert len(result) == MAX_TOPIC_WORDS
    assert "w0" in result


def test_parse_topic_words_strips_control_chars() -> None:
    """控制字符应被剥掉：ANSI 转义、NUL、C0/C1 控制符。"""
    assert parse_topic_words("\x1b[31m召回\x1b[0m") == ["召回"]
    assert parse_topic_words("召回\x00打分") == ["召回", "打分"]
    assert parse_topic_words("召回\x01\x02打分") == ["召回", "打分"]


def test_child_argv_length_insufficient_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    """fail-open: argv 长度不足时 run_extraction_child 返回 0 不逃逸。"""
    from scripts._topic import run_extraction_child
    monkeypatch.setattr("scripts._topic._call_model", lambda *_a, **_k: "召回")
    assert run_extraction_child([str(tmp_path)]) == 0
