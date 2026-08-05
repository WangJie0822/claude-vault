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
    )
    return vault


def _run_script(script_name: str, cwd: Path, prompt: str = "") -> tuple[float, str]:
    hook_input = json.dumps({"cwd": str(cwd), "prompt": prompt})
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
    for _ in range(WARMUP):
        _run_script("prompt_submit_load.py", NEUTRAL_CWD, prompt=_UPS_PROMPT)
    samples = [
        _run_script(
            "prompt_submit_load.py", NEUTRAL_CWD,
            prompt="召回 扩展词 相关性打分 回归测试 语义检索 关键词匹配 怎么优化实现",
        )[0]
        for _ in range(SAMPLES)
    ]

    p95 = statistics.median(samples)
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
    assert p95 < 0.3, f"UserPromptSubmit 性能超标: {p95:.3f}s（500 笔记参考基线）"
