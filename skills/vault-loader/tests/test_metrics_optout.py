# -*- coding: utf-8 -*-
"""停用闸门优先级守卫：任一停用途径生效 ⇒ metrics 零落盘；非停用路径 ⇒ metrics 确实落盘（对照组）。

fixture 沿用 `test_metrics_privacy_and_failopen.py::_write_note_and_cfg` 的真实 vault 模式
（frontmatter cache + 实际笔记文件 + 同一条已验证有效的 prompt/tag 组合）——**不要退回
"vault_path 指向不存在目录"的写法**：那样全部场景都会在「vault 不可达」早退（`prompt_submit_load.py`
约 501 行），该早退点在 5 道闸门之后、`_metrics.stage()`（约 571 行）之前，导致断言对闸门
的正确性完全不敏感（5 道闸门实测逐一中和 + 全部同时中和，6 场景全部保持绿，见
task-10-report.md「续」两节）。本文件因此必须搭配 `test_non_disabled_control_writes_metrics`
这个对照组——没有它，任何「零落盘」断言都无法区分「闸门拦住了」与「流水线压根没到 flush」。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "prompt_submit_load.py"

# 与 test_metrics_privacy_and_failopen.py 用同一条已验证能命中笔记、走到 decide_injection
# 的 prompt/tag 组合——该文件的对照组已实证这套组合会产出 .jsonl，此处直接复用，不必重新摸索
# 中文分词能否命中的问题。
PROMPT = "please explain the SessionStart hook implementation"


def _setup_vault_and_cfg(tmp_vault: Path, write_frontmatter_cache, extra_cfg: dict) -> dict:
    """建真实可用的 vault（frontmatter cache + 实际笔记文件），返回可直接落盘的 config dict。"""
    write_frontmatter_cache({
        "技术笔记/hook.md": {
            "tags": ["hook", "skill"],
            "category": "技术笔记",
            "summary": "SessionStart hook 实现",
            "mtime": 1900000000,
        }
    })
    (tmp_vault / "技术笔记").mkdir(exist_ok=True)
    (tmp_vault / "技术笔记" / "hook.md").write_text("# hook", encoding="utf-8")

    return {
        "dry_run": False,
        "vault_path": str(tmp_vault),
        "metrics": {"enabled": True, "near_miss_k": 10},
        **extra_cfg,
    }


def _run(tmp_home: Path, cwd: Path, cfg: dict, env_extra=None):
    cfgdir = tmp_home / ".claude" / "skills" / "vault-loader"
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                        encoding="utf-8")
    import os
    env = dict(os.environ)
    env.update({"HOME": str(tmp_home), "USERPROFILE": str(tmp_home)})
    env.update(env_extra or {})
    payload = json.dumps({"cwd": str(cwd), "prompt": PROMPT,
                          "session_id": "sess-A", "prompt_id": "pid-1"})
    return subprocess.run([sys.executable, str(HOOK)], input=payload, text=True,
                          capture_output=True, env=env, encoding="utf-8",
                          cwd=str(HOOK.parents[1]),
                          # hook 一旦引入意外的阻塞读或死锁，无 timeout 会让整个
                          # 测试跑静默挂死到 CI 超时——无堆栈、无输出、看不出卡在哪。
                          # 宁可红在这里。
                          timeout=60)


# 六个场景 = 五道停用闸门的全部子路径。**不要删减。**
# 变异实测证明：只保留前四个时，中和 `.vault-loader-disabled` 文件分支
# 或项目 CLAUDE.md disable 分支，四个场景**全部保持绿**——守卫对这两道闸门
# 完全测不出被破坏。这两道之所以最初漏掉，是因为它们要靠「创建文件」触发，
# 用 cfg_extra/env_extra 这两个参数结构表达不了，故增加第三个参数 setup。
@pytest.mark.parametrize("cfg_extra,env_extra,setup", [
    ({"opt_out_paths": ["{CWD}"]}, None, None),
    ({"enabled": False}, None, None),
    ({"user_prompt_submit": {"enabled": False}}, None, None),
    ({}, {"VAULT_LOADER_DISABLE": "1"}, None),
    ({}, None, "disable_file"),   # ~/.claude/.vault-loader-disabled
    ({}, None, "claude_md"),      # 项目 CLAUDE.md 的 disable 注释
])
def test_disabled_paths_write_zero_metrics(tmp_home, tmp_vault, write_frontmatter_cache,
                                            cfg_extra, env_extra, setup):
    work = tmp_home.parent / "work"
    work.mkdir(parents=True, exist_ok=True)
    cfg = _setup_vault_and_cfg(tmp_vault, write_frontmatter_cache, {})
    for k, v in cfg_extra.items():
        cfg[k] = [str(work)] if v == ["{CWD}"] else v
    if setup == "disable_file":
        (tmp_home / ".claude" / ".vault-loader-disabled").write_text("", encoding="utf-8")
    elif setup == "claude_md":
        # 必须写**真**标记：项目 CLAUDE.md 文档里提到它时会在 disable 中插一个
        # 零宽空格 U+200B 防自我命中，但这里是被测输入，插了就匹配不上、
        # 用例会假绿（闸门没触发却因别的原因没落盘）。
        (work / "CLAUDE.md").write_text("<!-- vault-loader: disable -->\n",
                                        encoding="utf-8")
    r = _run(tmp_home, work, cfg, env_extra)
    assert r.returncode == 0
    metrics = tmp_home / ".claude" / "vault-loader-metrics"
    files = list(metrics.rglob("*.jsonl")) if metrics.exists() else []
    assert files == [], f"停用状态下仍落盘: {files}"


def test_non_disabled_control_writes_metrics(tmp_home, tmp_vault, write_frontmatter_cache):
    """对照组（不可省略）：同一套真实 vault + 同一条 prompt、不触发任何停用条件 ⇒ metrics 必须
    确实落盘（≥1 个 .jsonl）。没有这条，`test_disabled_paths_write_zero_metrics` 的「零落盘」
    断言在 5 道闸门全部失效时也会保持绿——因为它测不出「零落盘」到底是闸门拦住的，还是
    流水线压根没跑到 flush（后者恰是本文件此前版本的真实状态，实测见 task-10-report.md）。
    """
    work = tmp_home.parent / "work-control"
    work.mkdir(parents=True, exist_ok=True)
    cfg = _setup_vault_and_cfg(tmp_vault, write_frontmatter_cache, {})
    r = _run(tmp_home, work, cfg)
    assert r.returncode == 0
    metrics = tmp_home / ".claude" / "vault-loader-metrics"
    files = list(metrics.rglob("*.jsonl")) if metrics.exists() else []
    assert files, "对照组（非停用）未产生 metrics 文件：测试管线没跑到 flush，5 道闸门测试全部失去意义"
