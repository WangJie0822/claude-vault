# -*- coding: utf-8 -*-
from scripts import _metrics


def test_counts_accumulate_across_separate_calls(tmp_path):
    """钉住 pre-flight 抓到的缺陷：hook 是一次性短进程，每轮只 +1。

    早期实现每次调用都做 `count >= 3` 的无条件裁剪，于是每次都从 0 加到 1、
    再被自己裁掉写回空盘——计数**永远**到不了任何阈值，near-miss 提示在生产中
    完全失效。PoC 实证 15 轮后盘上仍为 {}。本用例是该缺陷的形态级守卫。
    """
    for _ in range(10):
        _metrics.bump_near_miss_counts(tmp_path, ["hot.md"])
    assert _metrics.load_near_miss_counts(tmp_path)["hot.md"] == 10


def test_counts_keep_low_freq_entries(tmp_path):
    """低频条目不得被丢弃——容量没超就全留，裁剪只在超上限时发生。"""
    for _ in range(4):
        _metrics.bump_near_miss_counts(tmp_path, ["hot.md"])
    _metrics.bump_near_miss_counts(tmp_path, ["cold.md"])
    c = _metrics.load_near_miss_counts(tmp_path)
    assert c["hot.md"] == 4
    assert c["cold.md"] == 1


def test_nudge_fires_at_threshold_then_cools_down(tmp_path):
    for _ in range(10):
        _metrics.bump_near_miss_counts(tmp_path, ["hot.md"])
    assert _metrics.nudge_due(tmp_path, threshold=10) == ["hot.md"]
    _metrics.mark_nudged(tmp_path)
    assert _metrics.nudge_due(tmp_path, threshold=10) == [], "冷却期内不得重复提示"


def test_cooldown_is_global_not_per_cwd(tmp_path):
    """冷却文件必须落在 metrics 目录，而非 ~/.claude/projects/<cwd_hash>/。
    本机 25 个 cwd 目录，per-cwd 冷却会把「每周一次」放大 25 倍。"""
    for _ in range(10):
        _metrics.bump_near_miss_counts(tmp_path, ["hot.md"])
    _metrics.mark_nudged(tmp_path)
    p = _metrics.metrics_dir(tmp_path) / "nudge_ts.json"
    assert p.exists()
    assert "projects" not in str(p)


def test_counts_respect_entry_cap(tmp_path):
    """每次调用只 bump 一个 path —— 必须复刻真实生产形态。

    **不要写成 `[f"n{i}.md"] * 3`**：那让同一 path 在单次调用内连加三次，
    会绕开「跨调用累积」这条真正的路径，测试假绿。
    """
    for i in range(600):
        _metrics.bump_near_miss_counts(tmp_path, [f"n{i}.md"])
    assert len(_metrics.load_near_miss_counts(tmp_path)) <= 500


def test_high_freq_survives_cap_eviction(tmp_path):
    """超上限裁剪必须按 count 降序，高频条目不能被新涌入的低频挤掉。"""
    for _ in range(20):
        _metrics.bump_near_miss_counts(tmp_path, ["important.md"])
    for i in range(600):
        _metrics.bump_near_miss_counts(tmp_path, [f"n{i}.md"])
    assert _metrics.load_near_miss_counts(tmp_path)["important.md"] == 20


def test_purge_also_clears_counts_and_cooldown(tmp_path):
    """purge 承诺「一键清空」，顶层辅助文件同样要清，只留 .salt。"""
    _metrics.get_salt(tmp_path)
    for _ in range(3):
        _metrics.bump_near_miss_counts(tmp_path, ["x.md"])
    _metrics.mark_nudged(tmp_path)
    _metrics.purge(tmp_path)
    d = _metrics.metrics_dir(tmp_path)
    assert not (d / "near_miss_counts.json").exists()
    assert not (d / "nudge_ts.json").exists()
    assert (d / ".salt").exists()

# ===== P2（full-review High）：near_miss_counts.json 并发丢更新 =====

def _bump_worker(home, path, times, barrier=None):
    """**必须定义在模块级**——Windows 无 fork，multiprocessing 只能 spawn，
    spawn 要求 target 可按「模块名 + 限定名」重新 import；局部函数必然抛
    PicklingError（与被测逻辑无关，纯粹是测试自己跑不起来）。
    范式照抄 test_metrics_writer.py::_concurrent_worker。
    """
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from scripts import _metrics as m
    if barrier is not None:
        barrier.wait()          # 严格对齐起跑线，最大化读-改-写窗口重叠
    for _ in range(times):
        m.bump_near_miss_counts(_P(home), [path])


def test_concurrent_bumps_do_not_lose_updates(tmp_path):
    """读-改-写无保护时会整条丢更新，且是**静默**的。

    终审实测原实现：barrier 严格同步的 2/3/4 进程各 +1，10/10 轮均只落 1；
    12 进程 3 轮丢 11/12。危害不是数字略偏，而是计数永远攒不到阈值 ⇒
    near-miss 提示静默失效。这与「无条件裁剪」是同一失效表现的不同成因。

    near_miss_counts.json 是**跨 session 全局共享**文件，碰撞面比 write_record
    大得多（后者只在同一 session resume 到两个终端时才碰）。
    """
    from multiprocessing import Barrier, Process

    N, TIMES = 4, 15
    barrier = Barrier(N)
    procs = [Process(target=_bump_worker,
                     args=(str(tmp_path), "hot.md", TIMES, barrier))
             for _ in range(N)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    got = _metrics.load_near_miss_counts(tmp_path).get("hot.md", 0)
    # 判据是**有界损失**而非精确相等，这是设计使然、不是为了让用例变绿：
    # 取锁有重试预算，超预算即主动放弃本次 +1（绝不无限等，hook 卡死不可接受）。
    # 独立探针确认丢失量**恰好等于取锁失败次数**（3=3 / 0=0 / 1=1），全部来自这条
    # 设计路径，不含正确性缺陷；本用例又是 barrier 同步 + 零间隔重竞争的最苛刻构造，
    # 远比生产（每轮 hook 只 +1、分散在不同 prompt 间）严酷。
    # 该界仍有足够检测力：原实现同一场景只落 **3/60**（丢 95%），远低于 90%。
    lo = int(N * TIMES * 0.9)
    assert got >= lo, f"并发丢更新超出可接受界：期望 >= {lo}，实际 {got}"
