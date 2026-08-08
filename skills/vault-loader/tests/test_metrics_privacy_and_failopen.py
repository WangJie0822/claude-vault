# -*- coding: utf-8 -*-
"""Task 7 端到端硬验收：隐私开关零落盘 + metrics 全链路 fail-open。

`test_metrics_wiring.py` 已覆盖 `_finish_with_metrics` 单点的 stage/flush 契约；
本文件补两条**贯穿 main() 全流程、经真实子进程/真实 stdin 入口**的验收，
覆盖单点测试测不出的两类风险：

1. opt-out 闸门（main() 5 道停用闸门之一）是否真的在 stage 之前生效——
   单测 `_finish_with_metrics` 测不出「闸门根本没到 stage 那步」这件事。
2. metrics 四个关键函数（stage/flush/build_record/get_salt）任一失败时，
   main() 端到端仍必须产出含 additionalContext 的合法 JSON——这是本任务
   Global Constraints 明确要求的 fail-open 判据（退出码正常但 stdout 变空
   是最隐蔽的失败模式：用户完全无感，知识库注入静默消失）。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prompt_submit_load.py"


def _run(cwd: Path, prompt: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    if env_extra:
        env.update(env_extra)
    payload = {"cwd": str(cwd), "prompt": prompt, "session_id": "sess-A", "prompt_id": "pid-1"}
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", env=env, timeout=15,
    )


def _write_note_and_cfg(tmp_home: Path, tmp_vault: Path, write_frontmatter_cache,
                        extra_cfg: dict) -> None:
    write_frontmatter_cache({
        "技术笔记/hook.md": {
            "tags": ["hook", "skill"],
            "category": "技术笔记",
            "summary": "SessionStart hook 实现",
            "mtime": 1900000000,
        }
    })
    (tmp_vault / "技术笔记").mkdir()
    (tmp_vault / "技术笔记" / "hook.md").write_text("# hook", encoding="utf-8")

    cfg = tmp_home / ".claude" / "skills" / "vault-loader" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "dry_run": False,
        "vault_path": str(tmp_vault),
        "metrics": {"enabled": True, "near_miss_k": 10},
        **extra_cfg,
    }), encoding="utf-8")


# ---------------------------------------------------------------------------
# 验收 A —— opt-out 目录零落盘（用户唯一的隐私开关）
# ---------------------------------------------------------------------------

def test_opt_out_produces_zero_metrics_files(tmp_home: Path, tmp_vault: Path,
                                              write_frontmatter_cache):
    secret_cwd = tmp_home.parent / "secret-project"
    secret_cwd.mkdir()
    _write_note_and_cfg(tmp_home, tmp_vault, write_frontmatter_cache,
                        {"opt_out_paths": [str(secret_cwd)]})

    metrics_dir = tmp_home / ".claude" / "vault-loader-metrics"
    before = set(metrics_dir.rglob("*")) if metrics_dir.exists() else set()

    r = _run(secret_cwd, "please explain the SessionStart hook implementation")

    after = set(metrics_dir.rglob("*")) if metrics_dir.exists() else set()
    new_files = after - before

    assert r.returncode == 0
    # opt-out 命中 main() 的裸 `return 0` 闸门（先于 _finish/emit），stdout 恒空——
    # 这与 test_fail_open.py 里「导入失败时 stdout 必须为空」是同一契约，不是缺陷。
    assert r.stdout == ""
    assert not new_files, f"opt-out 目录仍产生了 metrics 文件：{new_files}"

    # 对照组：同配置、同 prompt，cwd 不在 opt_out 内——证明管线本身真的会落盘，
    # 排除"零新增文件"只是因为管线压根没跑到 flush 这一混淆可能。
    normal_cwd = tmp_home.parent / "normal-project"
    normal_cwd.mkdir()
    r2 = _run(normal_cwd, "please explain the SessionStart hook implementation")
    after2 = set(metrics_dir.rglob("*")) if metrics_dir.exists() else set()
    new_files2 = after2 - after
    assert r2.returncode == 0
    assert any(str(p).endswith(".jsonl") for p in new_files2), (
        "对照组（非 opt-out）也没有产生 .jsonl，说明测试管线本身没跑到 flush，"
        "无法证明零文件是 opt-out 生效导致的")


# ---------------------------------------------------------------------------
# 验收 B —— metrics 四个关键函数任一失败，main() 全流程仍 fail-open
# ---------------------------------------------------------------------------

def test_metrics_failure_never_breaks_injection_end_to_end(
        tmp_home: Path, tmp_vault: Path, write_frontmatter_cache):
    from scripts import _metrics
    from scripts._output import reset_emit_guard
    from scripts import prompt_submit_load as m

    _write_note_and_cfg(tmp_home, tmp_vault, write_frontmatter_cache, {})

    def _run_inprocess(exc_target, exc_type, unique_cwd):
        """进程内跑 main()，monkeypatch 指定函数抛异常。

        **每次调用必须用独立 cwd**：main() 成功路径会 save_injected() 落 state，
        同一 cwd 复用会让第 2 次起触发 dedup/fallback-cooldown 早退分支（无 admitted
        候选），产出的 stdout 与"flush 抛异常"这条待测路径无关——那是测试 fixture
        自身的状态污染，不是被测代码的缺陷（本会话曾踩坑，故显式记录）。
        """
        reset_emit_guard()
        _metrics.reset()
        mp = pytest.MonkeyPatch()
        try:
            def boom(*a, **k):
                raise exc_type("boom")
            mp.setattr(exc_target[0], exc_target[1], boom)
            buf = io.StringIO()
            mp.setattr(sys, "stdin", io.StringIO(json.dumps({
                "cwd": str(unique_cwd),
                "prompt": "please explain the SessionStart hook implementation",
                "session_id": "sess-B", "prompt_id": "pid-2",
            })))
            unique_cwd.mkdir(exist_ok=True)
            mp.setattr(sys, "stdout", buf)
            rc = m.main()
            return rc, buf.getvalue()
        finally:
            mp.undo()
            reset_emit_guard()

    for name, target, exc in [
        ("stage/OSError", (_metrics, "stage"), OSError),
        ("flush/RuntimeError", (_metrics, "flush"), RuntimeError),
        ("build_record/OSError", (_metrics, "build_record"), OSError),
        ("get_salt/RuntimeError", (_metrics, "get_salt"), RuntimeError),
    ]:
        unique_cwd = tmp_home.parent / f"normal-project-{name.replace('/', '-')}"
        rc, out = _run_inprocess(target, exc, unique_cwd)
        assert rc == 0, f"{name} 导致退出码非 0"
        parsed = json.loads(out)  # 拦不住则本身就是 fail：stdout 非法 JSON
        ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext")
        assert ctx, f"{name}: additionalContext 为空/缺失，注入静默丢失：{out!r}"
