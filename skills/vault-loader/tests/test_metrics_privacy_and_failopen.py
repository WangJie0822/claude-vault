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
# 验收 A2 —— inj_chars 的端到端契约（M6）
# ---------------------------------------------------------------------------

def _read_records(tmp_home: Path) -> list[dict]:
    md = tmp_home / ".claude" / "vault-loader-metrics"
    out = []
    for f in sorted(md.rglob("*.jsonl")):
        if f.parent.name == md.name:          # 顶层 annotations.jsonl 不是事件记录
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_inj_chars_equals_real_injected_length(tmp_home: Path, tmp_vault: Path,
                                               write_frontmatter_cache):
    """落盘的 inj_chars 必须等于**真正进了模型上下文**的正文长度。

    此前 inj_chars 只有单元级覆盖（8 处全是直接调 `_metrics.annotate()`），
    没有任何用例把它与 `additionalContext` 的真实长度对起来 —— 也就是说
    `_finish_with_metrics` 里那行接线写错了对象（比如误传 system_message、
    或在 sanitize 之前取长度）都不会被发现。
    """
    cwd = tmp_home.parent / "proj-inj"
    cwd.mkdir()
    _write_note_and_cfg(tmp_home, tmp_vault, write_frontmatter_cache, {})
    r = _run(cwd, "please explain the SessionStart hook implementation")
    assert r.returncode == 0
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    recs = [x for x in _read_records(tmp_home) if x.get("gate") == ""]
    assert recs, "对照失败：没有产生任何走到打分的记录，本用例证明不了任何事"
    assert recs[-1]["inj_chars"] == len(ctx), (
        f"落盘 {recs[-1].get('inj_chars')} != 实际注入 {len(ctx)} 字符")


def test_gate_record_keeps_exactly_five_keys(tmp_home: Path, tmp_vault: Path,
                                             write_frontmatter_cache):
    """闸门早退的极简记录只含五个键 —— 用**集合相等**钉，不用 `not in`。

    `_stage_gate_record` 的 docstring 承诺「只记五个键、零隐私增量」，而它原本的
    守卫是黑名单式的、拦不住新键漂入。具体失效场景：日后有人把某个早退出口的
    `additional_context=None` 改成 `""`（一个看起来无害的重构），gate 记录会静默
    多出 `inj_chars: 0`，25% 的轮次以 0 计进注入量均值 —— 而全套用例一条都不会红。

    集合相等是唯一能拦住「多出一个键」的写法：`"inj_chars" not in rec` 只挡得住
    这一个名字，挡不住下一个。
    """
    cwd = tmp_home.parent / "proj-gate"
    cwd.mkdir()
    _write_note_and_cfg(tmp_home, tmp_vault, write_frontmatter_cache, {})
    r = _run(cwd, "a")           # 关键词不足 ⇒ too_few_keywords 早退
    assert r.returncode == 0
    gates = [x for x in _read_records(tmp_home) if x.get("gate")]
    assert gates, "对照失败：没有产生闸门早退记录"
    assert set(gates[-1]) == {"_schema", "ts", "session", "prompt_id", "gate"}, (
        f"极简 gate 记录漂入了新键：{sorted(set(gates[-1]) - {'_schema', 'ts', 'session', 'prompt_id', 'gate'})}")


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
        # annotate 与上面几个同权：它在 emit 之后、flush 之前被调用，抛异常同样
        # 不得让本轮已完成的注入受影响（新接口不进这张矩阵就等于没有 fail-open 覆盖）
        ("annotate/RuntimeError", (_metrics, "annotate"), RuntimeError),
    ]:
        unique_cwd = tmp_home.parent / f"normal-project-{name.replace('/', '-')}"
        rc, out = _run_inprocess(target, exc, unique_cwd)
        assert rc == 0, f"{name} 导致退出码非 0"
        parsed = json.loads(out)  # 拦不住则本身就是 fail：stdout 非法 JSON
        ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext")
        assert ctx, f"{name}: additionalContext 为空/缺失，注入静默丢失：{out!r}"
