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
            prompt="please explain the hook spec implementation",
        )
        samples.append(elapsed)

    p95 = sorted(samples)[-1]
    assert p95 < 0.3, f"UserPromptSubmit 性能超标: {p95:.3f}s（500 笔记 fixture）"
