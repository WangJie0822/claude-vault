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
                extra: dict | None = None) -> tuple[float, str]:
    payload = {"cwd": str(cwd), "prompt": prompt}
    payload.update(extra or {})
    hook_input = json.dumps(payload)
    env = os.environ.copy()
    # 子进程强制 UTF-8（镜像生产；Windows 默认 GBK 会令 hook 输出 emoji/中文失败）
    env.setdefault("PYTHONUTF8", "1")
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
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(large_vault)}))

    _UPS_PROMPT = "召回 扩展词 相关性打分 回归测试 语义检索 关键词匹配 怎么优化实现"
    # 必须传 session_id：生产的 hook payload 一定带它，而 `has_recent_topic_attempt`
    # 里 `if not session_id: return False` 使空值下负缓存**恒失效** —— 不传就变成每轮都
    # 拉起一次 session_topic 提炼子进程，测的是生产中不存在的最坏情况。
    # session_topic 默认关时这条路径恒不执行，所以此前不传也测不出差别；1.1.0 默认开
    # 之后它就成了形态偏差。首轮那一次 spawn 的开销由下面的
    # test_first_turn_topic_spawn_overhead_bounded 单独守，不是被这里放过。
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
