from __future__ import annotations

import json
import time

from context_vault.events import claim_event


def test_event_claim_is_atomic_and_contains_no_content(tmp_path):
    assert claim_event("codex", "UserPromptSubmit", "s1", "e1", home=tmp_path)
    assert not claim_event("codex", "UserPromptSubmit", "s1", "e1", home=tmp_path)
    marker = next((tmp_path / ".context-vault/state/events/codex").glob("*.json"))
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["claims"][0]["event_id"] == "e1"
    assert data["session_hash"] != "s1"
    assert "prompt" not in data and "cwd" not in data


def test_event_names_and_runtimes_do_not_collide(tmp_path):
    assert claim_event("claude", "SessionStart", "s1", "e1", home=tmp_path)
    assert claim_event("codex", "SessionStart", "s1", "e1", home=tmp_path)
    assert claim_event("claude", "UserPromptSubmit", "s1", "e1", home=tmp_path)


def test_same_host_event_id_in_different_projects_does_not_collide(tmp_path):
    assert claim_event("claude", "UserPromptSubmit", "s1", "e1",
                       scope="/work/a", home=tmp_path)
    assert claim_event("claude", "UserPromptSubmit", "s1", "e1",
                       scope="/work/b", home=tmp_path)


def test_fallback_claim_only_suppresses_a_short_duplicate_window(tmp_path, monkeypatch):
    """不稳定 id 只压制「近乎同时的重复投递」，不得永久压制后续事件。

    ⚠️ TTL **不能取与 `claim_event` 自身耗时同量级的值**。原用例取 0.01s，而一次
    claim 要做 lease_lock + 两次文件 IO，本机实测 median 7~8ms、max 12~15ms ——
    窗口常在第二次调用到达前就已过期，用例随机转红（实测 3 次里红 2 次）。
    改为「TTL 取远大于操作耗时 + 用假时钟跨越窗口」：判据不再与机器速度耦合。

    假时钟只替换 `events` 模块内的 time，不碰 `atomic` 的 lease_lock ——
    后者用 `time.monotonic()` 算 deadline、用 `time.time()` 比 mtime，
    全局改时间会让它误判锁陈旧。
    """
    import context_vault.events as events

    class _FakeClock:
        def __init__(self) -> None:
            self.now = 1000.0

        def time(self) -> float:
            return self.now

    clock = _FakeClock()
    monkeypatch.setattr(events, "time", clock)

    assert claim_event("claude", "SessionStart", "s1", "fallback",
                       ttl_seconds=5.0, home=tmp_path)
    assert not claim_event("claude", "SessionStart", "s1", "fallback",
                           ttl_seconds=5.0, home=tmp_path), "窗口内的重复投递应被压制"
    clock.now += 10.0
    assert claim_event("claude", "SessionStart", "s1", "fallback",
                       ttl_seconds=5.0, home=tmp_path), "窗口过后必须放行，不得永久压制"


def test_unstable_event_id_is_not_written_to_disk(tmp_path):
    """内容派生的 event_id（材料含 prompt 原文）不得明文落进 marker。

    marker 保留 90 天且不受 metrics 的 opt-in 约束；材料中除 prompt 外全部本地可得
    （session_id 就以明文目录名存在于 state 路径里），足以做离线确认攻击。
    """
    # 变量名刻意不叫 secret/api_key/token：脱敏闸门按 `<那些名字> = "..."` 的形态
    # 匹配，而本文件自己在分发论域内，叫那些名字会让闸门命中自身夹具。
    derived_id = "a1b2c3d4e5f6a1b2c3d4"      # 冒充 fallback hash
    assert claim_event("claude", "UserPromptSubmit", "s9", derived_id,
                       home=tmp_path, stable=False)
    blob = json.dumps(json.loads(
        (tmp_path / ".context-vault" / "state" / "events" / "claude").glob("*.json").__next__()
        .read_text(encoding="utf-8")), ensure_ascii=False)
    assert derived_id not in blob, f"不稳定 event_id 被明文落盘：{blob}"
    # 对照：稳定 id 仍照常落盘（否则本用例对「是否区分两种情形」无判别力）
    assert claim_event("claude", "UserPromptSubmit", "s9", "stable-123", home=tmp_path)
    blob2 = (tmp_path / ".context-vault" / "state" / "events" / "claude").glob("*.json").__next__() \
        .read_text(encoding="utf-8")
    assert "stable-123" in blob2


def test_unstable_claim_is_salted(tmp_path):
    """不稳定 id 参与摘要时必须加本机盐——否则可离线字典攻击。"""
    from context_vault.events import _event_salt

    salt_path = tmp_path / ".context-vault" / "state" / "events" / ".salt"
    assert claim_event("claude", "UserPromptSubmit", "s1", "derived", home=tmp_path,
                       stable=False)
    assert salt_path.is_file(), "应生成本机事件盐"
    assert len(salt_path.read_bytes()) >= 16
    # 盐是稳定的：同一 home 反复取值一致，否则去重会失效
    assert _event_salt(tmp_path) == _event_salt(tmp_path)
