# -*- coding: utf-8 -*-
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts import _metrics

VL_ROOT = Path(__file__).resolve().parent.parent


def test_stage_is_pure_and_flush_writes(tmp_path):
    _metrics.reset()
    _metrics.stage({"_schema": _metrics.SCHEMA, "session": "sess-A", "x": 1})
    assert not _metrics.metrics_dir(tmp_path).exists()   # stage 零 IO
    _metrics.flush(tmp_path)
    files = list(_metrics.metrics_dir(tmp_path).rglob("*.jsonl"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8").strip())["x"] == 1


def test_flush_without_stage_is_noop(tmp_path):
    _metrics.reset()
    _metrics.flush(tmp_path)
    assert not list(_metrics.metrics_dir(tmp_path).rglob("*.jsonl"))


def test_writer_failure_never_breaks_injection(tmp_path, monkeypatch, capsys):
    """写盘必抛时，stdout 仍须是含注入的合法 JSON —— 这是 fail-open 的实质判据。
    现有 test_fail_open 只覆盖导入期失败、且其断言之一就是 stdout 为空，
    结构上测不出这类运行期损失。"""
    from scripts import prompt_submit_load as m

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(_metrics, "flush", boom)
    _metrics.reset()
    rc = m._finish_with_metrics({"display": {"user_visible": True}}, tmp_path,
                                additional_context="正文", system_message="摘要")
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["additionalContext"] == "正文"


def _make_pkg_with_broken_metrics(tmp_path: Path) -> Path:
    """复制真实 scripts/ 包，仅把 `_metrics.py` 换成导入期必炸的坏版本。

    与 `test_fail_open.py::_make_broken_pkg`（整包只留被测 hook 本体、制造
    `_config_loader` 等核心模块 ImportError）不同：这里其余 15 个模块原样复制、
    保证核心召回链路可用，**只**让 `_metrics` 这一个模块导入期抛异常——用来钉住
    「metrics 导入失败必须走独立降级、不能连累召回」这条隔离契约，而不是钉住
    "任意模块缺失都 exit 0" 这条已被 test_fail_open.py 覆盖过的粗粒度契约。
    """
    pkg = tmp_path / "scripts"
    shutil.copytree(VL_ROOT / "scripts", pkg, ignore=shutil.ignore_patterns("__pycache__"))
    (pkg / "_metrics.py").write_text(
        "raise ImportError('simulated metrics import failure for test guard')\n",
        encoding="utf-8",
    )
    return pkg / "prompt_submit_load.py"


def test_metrics_import_failure_does_not_break_recall(
        tmp_path, tmp_home, tmp_vault, write_frontmatter_cache):
    """形态级守卫（Critical 修复的回归钉子）：`_metrics` 导入失败不得连累召回。

    背景：`from scripts import _metrics` 曾被放进核心 import try-block（与
    `_config_loader`/`_output` 等 8 个召回必需模块混在一起）——那样 metrics 任何
    导入期问题（语法错误、分发缺文件、依赖缺失）都会让整轮召回静默失效
    （exit 0、stdout 空、零告警，与 test_fail_open.py 钉死的"核心模块缺失即静默
    exit 0"是同一种失败模式，但 metrics 不该占用那道防线）。正确做法是仿
    `_diagnostics` 走独立 try/except + 零功能替身（stage/flush/build_record/
    get_salt 全部覆盖），召回不受影响。

    变异验证记录见 task-7-report.md：把 import 挪回核心 try-block 后本用例实测
    变红；挪回独立 try-block 后实测恢复绿——证明本守卫测的是真实路径。
    """
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
    cfg.write_text(json.dumps({"dry_run": False, "vault_path": str(tmp_vault)}),
                   encoding="utf-8")

    script = _make_pkg_with_broken_metrics(tmp_path)

    env = os.environ.copy()  # tmp_home fixture 已用 monkeypatch.setenv 写好 HOME/USERPROFILE
    env.setdefault("PYTHONUTF8", "1")
    payload = {"cwd": str(tmp_home),
               "prompt": "please explain the SessionStart hook implementation"}
    r = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", env=env, timeout=15,
    )

    assert r.returncode == 0, f"metrics 导入失败不应改变退出码，stderr={r.stderr!r}"
    assert "metrics 模块不可用" in r.stderr, f"应留可诊断痕迹，实际 stderr={r.stderr!r}"
    parsed = json.loads(r.stdout)  # 解析失败本身就是本用例要防的那种回归
    ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext")
    assert ctx, f"metrics 导入失败连累了召回，additionalContext 为空：stdout={r.stdout!r}"


def test_metrics_stub_covers_all_called_interfaces():
    """`_MetricsStub` 必须覆盖 `_metrics` 上**被实际调用到**的每个接口。

    stub 在 metrics 模块导入失败时顶上。漏补一个新接口不会破 fail-open
    （emit 已在其前完成），但会让每轮多打一行 `metrics 写入失败` —— 把「静默降级」
    变成持续噪声，而这条路径平时不跑、没人会注意到。

    **判据靠扫源码而不是硬编码清单**：写死清单等于要求人同时改两处，
    而漏改的那次恰恰就是会出问题的那次。
    """
    import re

    src_path = Path(__file__).resolve().parents[1] / "scripts" / "prompt_submit_load.py"
    src = src_path.read_text(encoding="utf-8")
    # 去掉 _MetricsStub 类体本身，避免把 stub 的定义当成调用点
    stub_start = src.index("class _MetricsStub")
    stub_end = src.index("_metrics = _MetricsStub()")
    body = src[:stub_start] + src[stub_end:]

    # 先剥 docstring 与行注释，再扫调用点。两道都必需，理由各不相同：
    #   - 只认「后跟左括号」的形态，挡的是注释里的 `_metrics.py` 被当成接口名 `py`；
    #   - 剥 docstring/注释，挡的是注释里**写全了调用形态**的引用（如解释某个键为什么
    #     不落盘时提到 `_metrics.scorelow_entries()`）——它长得与真实调用一模一样，
    #     会让本用例报「stub 缺少接口」，而那个接口根本不在 hook 路径上。
    #     实际踩过：给 `_stage_gate_record` 的 docstring 补一句机制说明就把本用例弄红了。
    code = re.sub(r'"""(?:.|\n)*?"""', "", body)      # docstring（本文件无单引号三引号）
    code = re.sub(r"#.*", "", code)                    # 行注释
    called = set(re.findall(r"\b_metrics\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", code))
    assert called, "解析失败：没扫到任何 _metrics.<name> 调用点（判据本身失效了）"
    # 阳性对照：`stage` 是恒存在的真实调用点，扫不到它说明剥注释剥过头了
    assert "stage" in called, f"解析失效：真实调用点 stage 被误剥，扫到的是 {sorted(called)}"

    # stub 定义在 `except ImportError` 分支内，正常导入路径下它根本不存在
    # （`from ... import _MetricsStub` 会 ImportError）⇒ 只能同样按源码解析。
    stub_src = src[stub_start:stub_end]
    provided = set(re.findall(r"^\s+def\s+([A-Za-z_][A-Za-z0-9_]*)", stub_src, re.M))
    assert provided, "解析失败：没扫到 stub 的任何方法定义"

    missing = sorted(called - provided)
    assert not missing, (
        f"_MetricsStub 缺少被调用的接口 {missing}；"
        f"调用点={sorted(called)}，stub 提供={sorted(provided)}")
