"""性能基线：500 笔记下 SessionStart < 500 ms、UserPromptSubmit < 300 ms。"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests._neutral import NEUTRAL_CWD

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"
FIXTURE_BUILDER = Path(__file__).resolve().parents[1] / "fixtures" / "build_large_vault.py"

# 采样数与统计量（L-10）。此前是「3 次取最差」并把它叫作 p95——3 个样本里的最大值
# 不是 p95，而是**尖峰探测器**：解释器冷启动、fixture 建完后的冷文件缓存、其他进程
# 抢 I/O，任何一次抖动都会主导结论。这个判据在本项目已经造成过两次方向相反的误判
# （先把真回归当成「既有环境失败」放过，后又把噪声当成「确定性回归」）。
#
# 改用 median：中位数对单侧尖峰不敏感，反映的是「通常有多快」——这才是性能守卫想钉的。
# 阈值维持不变（没有为了让用例变绿而放松），只是把统计量换成不被离群值主导的那个。
# 判定真回归仍须 A/B 交替独立测量，见 CLAUDE.md「开发与测试」。
SAMPLES = 7

# 预热次数（不计入样本）。fixture 刚写完 500 个 .md + cache，紧接着的第一次调用要读
# 尚未进入文件系统缓存的内容，还可能与后台刷盘抢 I/O——实测这一次比后续稳定值高一倍
# 以上，正是它把「3 次取最差」顶过阈值的。生产里 hook 读的是长期存在的 Vault，
# 对应的是**热缓存**状态，所以丢掉冷启动那次才是更忠实的建模，不是为了让用例变绿。
WARMUP = 1


def test_fixture_writes_real_bodies(tmp_path: Path) -> None:
    """perf fixture 必须生成真实 .md 正文，否则任何读正文的代码都测不到。"""
    from tests.fixtures.build_large_vault import build_large_vault

    vault = tmp_path / "V"
    build_large_vault(vault, n_notes=30, seed=1)

    mds = [p for p in vault.rglob("*.md")]
    assert len(mds) == 30, f"应生成 30 个真实 .md，实际 {len(mds)}"

    sizes = sorted(p.stat().st_size for p in mds)
    assert sizes[0] > 0, "不得生成空文件"
    # 分布右偏：最大篇应显著大于中位篇（复现真实 Vault 的长尾）
    assert sizes[-1] > sizes[len(sizes) // 2] * 3, "正文长度分布缺少长尾"

    joined = "\n".join(p.read_text(encoding="utf-8") for p in mds)
    assert "```" in joined, "正文应含 fenced code block（真实 Vault 42.6% 字符在代码块内）"
    assert any("一" <= ch <= "鿿" for ch in joined), "正文应含 CJK 字符"


@pytest.fixture
def large_vault(tmp_home: Path) -> Path:
    """构造 500 笔记 Vault。"""
    vault = tmp_home / "Vault"
    subprocess.run(
        [sys.executable, str(FIXTURE_BUILDER), str(vault), "500"],
        check=True,
        # builder 卡住时不设上限会让整个测试跑静默挂死到 CI 超时，无堆栈可看
        timeout=300,
    )
    return vault


def _run_script(script_name: str, cwd: Path, prompt: str = "",
                extra: dict | None = None, env_extra: dict | None = None) -> tuple[float, str]:
    payload = {"cwd": str(cwd), "prompt": prompt}
    payload.update(extra or {})
    hook_input = json.dumps(payload)
    env = os.environ.copy()
    # 子进程强制 UTF-8（镜像生产；Windows 默认 GBK 会令 hook 输出 emoji/中文失败）
    env.setdefault("PYTHONUTF8", "1")
    env.update(env_extra or {})
    t0 = time.perf_counter()
    r = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script_name)],
        input=hook_input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=10,
    )
    elapsed = time.perf_counter() - t0
    return elapsed, r.stdout


def test_session_start_under_500ms(tmp_home: Path, large_vault: Path) -> None:
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(large_vault)}))

    for _ in range(WARMUP):
        _run_script("session_start_load.py", NEUTRAL_CWD)
    samples = [_run_script("session_start_load.py", NEUTRAL_CWD)[0] for _ in range(SAMPLES)]
    observed = statistics.median(samples)
    assert observed < 0.5, (
        f"SessionStart 性能超标: median {observed:.3f}s（{SAMPLES} 样本，500 笔记 fixture）\n"
        f"全部样本: {[f'{s:.3f}' for s in sorted(samples)]}"
    )


def test_prompt_submit_under_300ms(tmp_home: Path, large_vault: Path) -> None:
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(large_vault),
                               "relevance": {"fine_rank": False}}))

    _UPS_PROMPT = "召回 扩展词 相关性打分 回归测试 语义检索 关键词匹配 怎么优化实现"
    # 必须传 session_id：生产的 hook payload 一定带它。
    # 2.4.0 起精排逐轮化，默认配置下每个通过闸门的提问都会发起一次精排。本用例显式关掉精排
    # （`relevance.fine_rank: false`），测的是「只有粗排」的热路径，与 2.3.0 及之前的口径可比，
    # 也不会拉起真实 claude。精排开着的新稳态（每轮 spawn + fine 状态读写 + 会话设置）由
    # test_prompt_submit_steady_state_with_fine_under_300ms 守，会话首轮那一次 spawn 由
    # test_first_turn_topic_spawn_overhead_bounded 守。
    _SID = "sess-perf-ups"
    for _ in range(WARMUP):
        _run_script("prompt_submit_load.py", NEUTRAL_CWD, prompt=_UPS_PROMPT,
                    extra={"session_id": _SID})
    samples = [
        _run_script(
            "prompt_submit_load.py", NEUTRAL_CWD,
            prompt="召回 扩展词 相关性打分 回归测试 语义检索 关键词匹配 怎么优化实现",
            extra={"session_id": _SID},
        )[0]
        for _ in range(SAMPLES)
    ]

    # 变量名曾叫 `p95`，但取的一直是 median（L-10 换统计量时漏改名）。
    # 那个名字会让 assert 失败消息读起来像在报 p95，据此解读数据会错。
    observed = statistics.median(samples)
    # 诚实标注：300ms 是 **500 篇合成 fixture 的参考基线**，本用例通过**不代表**生产规模
    # 也在 300ms 内。2026-08-04 本机实测（同 prompt、同 builder，各 n=9 子进程端到端）：
    #   500 篇 fixture      ：median 289ms / 359ms（两轮），min 247ms / 319ms
    #   真实 Vault(728 active)：median 423ms / 441ms（两轮），min 364ms / 385ms —— **两轮的
    #                          最好值都已超 300ms 预算**
    # （测量时本机有并发任务，绝对值偏高；两者同轮同机对照，相对关系可信。）
    #
    # 别按「篇数 ×k ⇒ 耗时 ×k」外推——线性假设不成立。真实 Vault 的 active 篇数只是
    # fixture 的 ×1.46（728/500），但同口径下决策层耗时是 ×2.2~2.3（decide_injection
    # median 200~242ms vs 87~104ms，3 轮）。原因是真实笔记的 frontmatter 密度（tags /
    # keywords / summary 长度）远高于合成 fixture，单篇打分成本本身就更高。
    #
    # 超支主导项是解释器启动 + O(N) 打分主循环 + 进程 spawn。此处保持 500 篇是为避免
    # 更大规模在内存压力下 flaky；真正的 scaling 天花板需要倒排索引，属独立议题。
    # ⚠️ 本用例对**机器负载**敏感。红了先看有没有并发任务在跑，别急着当成回归。
    # 2026-09-07 本机实测（同 fixture、同 prompt、median of 7）：
    #   空闲：约 0.20s，5/5 通过，余量约 30%；`run_gates.py` 全量跑时同样全绿
    #   两个 subagent 并发时：0.32~0.49s，必红
    # 判定是否**确定性**回归必须做 A/B 交替的 median of N，并看 median 与 min 是否
    # **同步位移**——只有同步位移才是确定性差异（CLAUDE.md「开发与测试」）。
    #
    # 同日的一次实证教训值得记住：固定「先跑新版、后跑旧版」的顺序做 A/B，在负载
    # 单调下降时会给**先跑者**系统性劣势——第一组数据显示新版慢 1.21s，反序复测却
    # 显示新版快 0.30s，两组方向相反。真正成立的是「先跑的那个更慢」，与代码无关。
    # 所以 A/B 必须交替（ABBA），且不要在有并发 agent 的机器上下性能结论。
    assert observed < 0.3, (
        f"UserPromptSubmit 性能超标: {observed:.3f}s（500 笔记参考基线，中位数）")


def test_prompt_submit_with_mod_request_under_300ms(tmp_home: Path, large_vault: Path) -> None:
    """交互终端里 mod 每轮都写请求文件：UPS 因此多走 `_report_llm` 两次、`_fill_report` /
    `_fill_provenance` 与快照写入。上面两条用例的 session_id / prompt_id 不是 UUID，
    `_session_ops.request_path` 返回 None，这些代码一行都不执行（full-review T-H1：让新代码
    每次调用慢 100 ms，有请求的 UPS 到 0.38 s，那三条性能守卫照样全绿）。

    先证明测的是 mod 在场路径（每轮快照 prompt_id 对得上、status 为 scored、`llm.state` 为 ok，
    即两次 describe 都读到了预置的 entry），再谈耗时。PATH 指向空目录：预置失效时这里会尝试
    发起提炼，不得真起付费的 claude 子进程（那种情形下 `llm.state` 是 unavailable，断言会先红）。
    """
    import hashlib
    from scripts import _state, _topic

    cfg = tmp_home / ".context-vault" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    # 2.4.0：关掉逐轮补充。预置的 topics 已是首轮成功（state=ok），于是本会话不再发起精排、
    # `llm.state` 稳定为 ok；精排开着的新稳态另有 test_prompt_submit_steady_state_with_fine_under_300ms
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(large_vault),
                               "relevance": {"fine_supplement": False}}))
    # 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id
    sid = "-".join(("11111111", "2222", "3333", "4444", "555555555555"))
    _state.configure_context("claude")
    try:
        _topic.save_session_topic(NEUTRAL_CWD, sid, ["相关性打分"], pinned=[], accounted=False,
                                  state="ok")
    finally:
        _state.configure_context("unknown")
    live = tmp_home / ".context-vault" / "state" / "live"
    live.mkdir(parents=True, exist_ok=True)
    sid_h = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:24]
    empty = tmp_home / "emptybin"
    empty.mkdir()
    prompt = "召回 扩展词 相关性打分 回归测试 语义检索 关键词匹配 怎么优化实现"
    samples = []
    for i in range(WARMUP + SAMPLES):
        pid = "-".join(("aaaaaaaa", "bbbb", "cccc", "dddd", f"{i:012d}"))
        (live / f"{sid_h}-{pid}.req.json").write_text('{"v": 1}', encoding="utf-8")
        elapsed, _ = _run_script(
            "prompt_submit_load.py", NEUTRAL_CWD, prompt=prompt,
            extra={"session_id": sid, "prompt_id": pid, "hook_event_name": "UserPromptSubmit"},
            env_extra={"PATH": str(empty)})
        snap = json.loads((live / f"{sid_h}.json").read_text(encoding="utf-8"))
        assert (snap["prompt_id"], snap["status"]) == (pid, "scored"), snap.get("status")
        assert snap["llm"] is not None and snap["llm"]["state"] == "ok", snap["llm"]
        if i >= WARMUP:
            samples.append(elapsed)
    observed = statistics.median(samples)
    assert observed < 0.3, (
        f"mod 在场的 UserPromptSubmit 性能超标: median {observed:.3f}s（{SAMPLES} 样本，"
        f"500 笔记 fixture）\n全部样本: {[f'{s:.3f}' for s in sorted(samples)]}")


def test_prompt_submit_with_metrics_enabled_stays_within_budget(
        tmp_home: Path, large_vault: Path) -> None:
    """metrics 开启后 UPS 仍须在预算内，且必须证明**真的**写了 metrics。

    此前性能守卫从未在 `metrics.enabled=True` 下跑过——决策面落盘（`stage()`/
    `flush()`、读盐、`near_miss_counts` 取锁、prune 频率闸门）全都加在 UPS 热路径
    上，却没有任何性能门禁覆盖它们。

    预算给到 0.5s（关闭态是 0.3s）而非卡死在 0.3s：这条守卫要拦的是「每次 flush
    都去扫整个月份目录」「prune 退化成每次都跑」这类**量级**回归，不是几毫秒抖动。
    卡太死只会得到一条常年偶发红、然后被无视的用例——那等于没有守卫。

    **落盘断言是本用例的关键**：没有它，metrics 若因任何原因静默没启用（配置键
    写错、opt-in 判定改动、import 失败被 fail-open 吞掉），这里测到的其实是关闭态
    的耗时，照样绿。一条测不到目标路径的性能守卫比没有更糟——它提供虚假的安全感。
    """
    from scripts import _metrics

    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "dry_run": False,
        "vault_path": str(large_vault),
        # 2.4.0：关掉精排，只测 metrics 落盘的增量；PATH 上的真实 claude 也因此不会被拉起
        "relevance": {"fine_rank": False},
        "metrics": {"enabled": True, "near_miss_k": 10, "admitted_k": 20,
                    "retention_days": 90, "nudge_threshold": 10,
                    "nudge_ttl_hours": 168},
    }))

    prompt = "召回 扩展词 相关性打分 回归测试 语义检索 关键词匹配 怎么优化实现"
    sid = "sess-perf-metrics"
    total = 0
    for _ in range(WARMUP):
        _run_script("prompt_submit_load.py", NEUTRAL_CWD, prompt=prompt,
                    extra={"session_id": sid, "prompt_id": f"pid-w{total}"})
        total += 1
    samples = []
    for i in range(SAMPLES):
        elapsed, _ = _run_script(
            "prompt_submit_load.py", NEUTRAL_CWD, prompt=prompt,
            extra={"session_id": sid, "prompt_id": f"pid-{i}"})
        samples.append(elapsed)
        total += 1

    # 先证明测的是 metrics 开启态，再谈耗时——顺序刻意如此：落盘断言不过时，
    # 耗时数字毫无意义，不该让它先被读到。
    lines = 0
    for d in _metrics.event_month_dirs(tmp_home):
        for f in d.glob("*.jsonl"):
            lines += sum(1 for ln in f.read_text(encoding="utf-8").splitlines()
                         if ln.strip())
    assert lines == total, (
        f"metrics 未按预期落盘：期望 {total} 条记录（{WARMUP} 预热 + {SAMPLES} 采样），"
        f"实际 {lines} 条。本用例测的可能根本不是 metrics 开启态。")

    observed = statistics.median(samples)
    assert observed < 0.5, (
        f"metrics 开启态 UserPromptSubmit 性能超标: median {observed:.3f}s"
        f"（{SAMPLES} 样本，500 笔记 fixture，关闭态预算 0.3s）\n"
        f"全部样本: {[f'{s:.3f}' for s in sorted(samples)]}")


# ---------------------------------------------------------------------------
# 2.4.0 精排逐轮化后的新稳态（spec §14「性能」）

_FAKE_CLAUDE_LINES = [
    "import sys",
    "sys.stdin.buffer.read()",
    "for line in ('PINNED: NONE', 'SUPPLEMENT: NONE', 'WORDS: recall, ranking'):",
    "    print(line)",
]


def _fake_claude_bin(root: Path) -> Path:
    """PATH 上的替身 `claude`：读完 stdin、回一份合法的精排输出（两段都是 NONE），不联网、不计费。
    Windows 上 `shutil.which` 按 PATHEXT 找到 `claude.cmd`，POSIX 上找到可执行的 `claude`。"""
    d = root / "fakebin"
    d.mkdir(exist_ok=True)
    py = d / "fake_claude.py"
    py.write_text("\n".join(_FAKE_CLAUDE_LINES) + "\n", encoding="utf-8")
    if os.name == "nt":
        # 字节写入：write_text 在 Windows 上会把 \r\n 再转成 \r\r\n，落盘多一个游离 CR
        (d / "claude.cmd").write_bytes(f'@"{sys.executable}" "{py}" %*\r\n'.encode("utf-8"))
    else:
        sh = d / "claude"
        sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n', encoding="utf-8")
        sh.chmod(0o755)
    return d


def _uuid(tail: int) -> str:
    # 占位 UUID 拆开拼接：发布闸门按 8-4-4-4-12 hex 拦截疑似真实 session id
    return "-".join(("11111111", "2222", "3333", "4444", f"{tail:012d}"))


def _wait_fine_idle(sid: str, timeout: float = 60.0) -> bool:
    """等本会话的精排子进程跑完（在飞标记消失）。不计入耗时样本：真实使用里两次提问的间隔
    通常长于一次精排，「上一个还在跑」时的并发负载不是这条守卫要测的。"""
    from scripts import _fine_state, _state
    deadline = time.time() + timeout
    _state.configure_context("claude")
    try:
        while time.time() < deadline:
            if _fine_state.inflight_active(sid, time.time()) is None:
                return True
            time.sleep(0.2)
        return False
    finally:
        _state.configure_context("unknown")


def _fine_steady_state(tmp_home: Path, large_vault: Path, sid: str, seed_injected=()) -> tuple:
    """精排开着的稳态：mod 在场（每轮带会话设置）、首轮点名已成功，每轮发起一次逐轮补充，子进程用替身
    claude 真起、真跑完。`seed_injected` 预置进本会话 fine 状态的「已注入」（它们原样进每轮的 payload）。
    返回 (样本耗时列表, 跑完后的 fine 状态)。"""
    import dataclasses
    import hashlib
    from scripts import _fine_state, _state, _topic

    cfg = tmp_home / ".context-vault" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    # 外发上限调大：500 篇 fixture 的目录层每次可达数十 KB，8 轮可能顶到默认 1,000,000 字节，
    # 顶到后本会话停用逐轮补充、后几轮不再 spawn，测的就不是新稳态了
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(large_vault),
                               "relevance": {"fine_session_max_bytes": 20000000}}))
    _state.configure_context("claude")
    try:
        _topic.save_session_topic(NEUTRAL_CWD, sid, ["相关性打分"], pinned=[], accounted=False,
                                  state="ok")
        if seed_injected:
            seeded = _fine_state.update_fine(
                sid, lambda st: dataclasses.replace(st, injected=list(seed_injected)))
            assert seeded is not None and seeded.injected == list(seed_injected)
    finally:
        _state.configure_context("unknown")
    live = tmp_home / ".context-vault" / "state" / "live"
    live.mkdir(parents=True, exist_ok=True)
    sid_h = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:24]
    path_env = str(_fake_claude_bin(tmp_home)) + os.pathsep + os.environ.get("PATH", "")
    prompt = "召回 扩展词 相关性打分 回归测试 语义检索 关键词匹配 怎么优化实现"
    samples = []
    for i in range(WARMUP + SAMPLES):
        pid = "-".join(("aaaaaaaa", "bbbb", "cccc", "dddd", f"{i:012d}"))
        (live / f"{sid_h}-{pid}.req.json").write_text(
            json.dumps({"v": 1, "settings": {"fine_max_notes": 2}}), encoding="utf-8")
        elapsed, _ = _run_script(
            "prompt_submit_load.py", NEUTRAL_CWD, prompt=prompt,
            extra={"session_id": sid, "prompt_id": pid, "hook_event_name": "UserPromptSubmit"},
            env_extra={"PATH": path_env})
        snap = json.loads((live / f"{sid_h}.json").read_text(encoding="utf-8"))
        assert (snap["prompt_id"], snap["status"]) == (pid, "scored"), snap.get("status")
        if i >= WARMUP:
            samples.append(elapsed)
        assert _wait_fine_idle(sid), f"第 {i} 轮的精排子进程 60 s 内没有结束"
    _state.configure_context("claude")
    try:
        st = _fine_state.load_fine(sid)
    finally:
        _state.configure_context("unknown")
    return samples, st


def test_prompt_submit_steady_state_with_fine_under_300ms(tmp_home: Path, large_vault: Path) -> None:
    """新稳态：mod 在场（每轮带会话设置）、首轮点名已成功，每轮都发起一次逐轮补充并读写一次 fine
    状态。子进程真起、真跑完（替身 claude），不联网不计费。

    先证明测的是这条路径——每轮快照 status 为 scored、本会话调用次数等于轮数、会话设置已落进
    fine 状态、子进程没有失败、最后一次子进程回报的模式是 supp 且成功——再谈耗时。调用次数、失败计数
    与会话设置三项在 pin 与 supp 两种模式下完全一样，认不出「逐轮补充」，认模式的只有 stats_q：
    最后一轮之后没有下一轮来并账，所以最后一条统计还留在队列里。"""
    samples, st = _fine_steady_state(tmp_home, large_vault, _uuid(7))
    assert st.calls == WARMUP + SAMPLES, f"不是每轮都发起了精排：calls={st.calls}"
    assert (st.fail_streak, st.disabled_reason) == (0, None), (st.fail_streak, st.disabled_reason,
                                                               st.last_fail)
    assert st.session_settings.get("fine_max_notes") == 2, st.session_settings
    assert st.stats_q and st.stats_q[-1]["mode"] == "supp" and st.stats_q[-1]["ok"] is True, st.stats_q
    observed = statistics.median(samples)
    assert observed < 0.3, (
        f"精排新稳态 UserPromptSubmit 性能超标: median {observed:.3f}s（{SAMPLES} 样本，"
        f"500 笔记 fixture）\n全部样本: {[f'{s:.3f}' for s in sorted(samples)]}")


def test_prompt_submit_steady_state_with_large_fine_payload_under_300ms(
        tmp_home: Path, large_vault: Path) -> None:
    """同上的稳态，但交给精排子进程的 payload 超过 4096 B（2.4.0 full-review P-H1）。

    上一条用例的 payload 远小于 4096 B（短 prompt、fixture 短路径），而 Windows 匿名管道缓冲恰是 4096 B：
    交接若是「管道 + 同步写」，越过它的那一轮 UPS 要在 emit 之前等子进程启动并导入完 `_topic`（实测约 92 ms，
    会话中后期本会话已注入约 28 条中文长路径就会越过）。这里预置 60 条中文长路径进本会话的「已注入」，
    它们原样进每一轮的 payload；先证明 payload 确实越过 4096 B、这 60 条到最后一轮仍在状态里（每轮都带着），
    再谈耗时。判别「交接是否同步」的是 `test_session_topic.py` 里的替身子进程用例，这条守的是预算。"""
    seed = [f"项目笔记/车载语音/语音助手交互链路排查记录与根因分析第{i:02d}篇.md" for i in range(60)]
    seed_bytes = len(json.dumps(seed, ensure_ascii=False).encode("utf-8"))
    assert seed_bytes > 4096, seed_bytes        # payload 的 injected 字段整份含它们，payload 只会更大
    samples, st = _fine_steady_state(tmp_home, large_vault, _uuid(8), seed_injected=seed)
    # injected 只增不减（clear / compact 之外），超过 300 条时先截最旧的：最旧的这 60 条到最后还在，
    # 说明每一轮读到的状态里都有它们，每一轮的 payload 都带着
    assert set(seed) <= set(st.injected), "预置的已注入路径中途丢了，后几轮的 payload 可能已不足 4096 B"
    assert st.calls == WARMUP + SAMPLES, f"不是每轮都发起了精排：calls={st.calls}"
    assert (st.fail_streak, st.disabled_reason) == (0, None), (st.fail_streak, st.disabled_reason,
                                                               st.last_fail)
    assert st.stats_q and st.stats_q[-1]["mode"] == "supp" and st.stats_q[-1]["ok"] is True, st.stats_q
    observed = statistics.median(samples)
    assert observed < 0.3, (
        f"payload 超过 4096 B 的精排稳态 UserPromptSubmit 性能超标: median {observed:.3f}s"
        f"（{SAMPLES} 样本，500 笔记 fixture）\n全部样本: {[f'{s:.3f}' for s in sorted(samples)]}")


def test_first_turn_topic_spawn_overhead_bounded(tmp_home: Path, large_vault: Path) -> None:
    """会话首轮那一次 spawn（F51：此前上方注释引用了这条用例，它却不存在）。每个样本换一个新会话，
    所以每一轮都是会话首轮、都走首轮模式发起一次精排。先证明每个会话都真的发起并跑完了
    （topics 写回了替身给的主题词、调用计数为 1），再谈耗时。"""
    from scripts import _fine_state, _state, _topic

    cfg = tmp_home / ".context-vault" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(large_vault)}))
    path_env = str(_fake_claude_bin(tmp_home)) + os.pathsep + os.environ.get("PATH", "")
    prompt = "召回 扩展词 相关性打分 回归测试 语义检索 关键词匹配 怎么优化实现"
    samples = []
    for i in range(WARMUP + SAMPLES):
        sid = _uuid(100 + i)
        elapsed, _ = _run_script(
            "prompt_submit_load.py", NEUTRAL_CWD, prompt=prompt,
            extra={"session_id": sid, "prompt_id": f"pid-first-{i}",
                   "hook_event_name": "UserPromptSubmit"},
            env_extra={"PATH": path_env})
        assert _wait_fine_idle(sid), f"第 {i} 个会话的首轮精排 60 s 内没有结束"
        _state.configure_context("claude")
        try:
            words = _topic.load_session_topic(NEUTRAL_CWD, sid, 24)
            fine = _fine_state.load_fine(sid)
        finally:
            _state.configure_context("unknown")
        assert (words, fine.calls) == (["recall", "ranking"], 1), (words, fine.calls)
        # 首轮必须走首轮模式（pin）：supp 模式同样会写回主题词、同样计一次调用，上面两项认不出区别
        assert fine.stats_q and fine.stats_q[-1]["mode"] == "pin" and fine.stats_q[-1]["ok"] is True, \
            fine.stats_q
        if i >= WARMUP:
            samples.append(elapsed)
    observed = statistics.median(samples)
    assert observed < 0.3, (
        f"会话首轮（含一次精排 spawn）UserPromptSubmit 性能超标: median {observed:.3f}s"
        f"（{SAMPLES} 样本，500 笔记 fixture）\n全部样本: {[f'{s:.3f}' for s in sorted(samples)]}")
