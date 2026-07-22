"""性能基线：500 笔记下 SessionStart < 500 ms、UserPromptSubmit < 300 ms。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"
FIXTURE_BUILDER = Path(__file__).resolve().parents[1] / "fixtures" / "build_large_vault.py"


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

    samples = []
    for _ in range(3):
        elapsed, _ = _run_script("session_start_load.py", Path("/tmp"))
        samples.append(elapsed)

    p95 = sorted(samples)[-1]  # 3 次取最差
    assert p95 < 0.5, f"SessionStart 性能超标: {p95:.3f}s（500 笔记 fixture）"


def test_prompt_submit_under_300ms(tmp_home: Path, large_vault: Path) -> None:
    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(large_vault)}))

    samples = []
    for _ in range(3):
        elapsed, _ = _run_script(
            "prompt_submit_load.py", Path("/tmp"),
            prompt="召回 扩展词 相关性打分 回归测试 语义检索 关键词匹配 怎么优化实现",
        )
        samples.append(elapsed)

    p95 = sorted(samples)[-1]
    # full-review 2B-M1/3A-T3 诚实标注：300ms 是 **500 篇参考基线**。真实 Vault ~979 篇
    # （~2×）端到端实测 ~217ms（宽松内存）到 ~330ms（内存压力/单次抖动），已在预算边缘。
    # 超支主导项是解释器启动(~90ms)+O(N) 基础打分正则循环(~60ms)+进程 spawn，**非本次
    # tag-IDF 改动**（性能维实测 tag-heavy 查询不比 tag-miss 慢，tag-IDF 净 +1~7ms 且因候选
    # 集收窄常抵消）。此处保持 500 篇以避免 1000 篇在内存压力下 flaky；生产规模的真实
    # scaling 天花板见 spec §9（n≈3000 需倒排索引，属下一期，与本轮四项组合正交）。
    assert p95 < 0.3, f"UserPromptSubmit 性能超标: {p95:.3f}s（500 笔记参考基线）"
