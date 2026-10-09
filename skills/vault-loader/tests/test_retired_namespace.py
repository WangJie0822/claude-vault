"""退役命名空间（2.0.0 移除 Codex 支持）：插件不再读写 `metrics/codex/`，但用户显式
`--purge` / `--reset-counts`（`--runtime all`，或 canonical 下的 `auto`）时必须按路径清掉它。

作者本机 `metrics/codex/` 含明文笔记路径（near_miss_counts.json 的键、admitted /
near_miss 字段），而 `analyze_metrics.py` 的注释明令禁止「只清一半却报告已清空」。
走真实 CLI 子进程：命名空间是在 `main()` 里选定的。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_metrics.py"


def _run(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONUTF8": "1"})
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *args],
                          capture_output=True, text=True, env=env, encoding="utf-8",
                          cwd=str(SCRIPT.parents[1]), timeout=60)


def _seed_retired(home: Path) -> Path:
    root = home / ".context-vault" / "metrics" / "codex"
    (root / "2026-10").mkdir(parents=True)
    (root / "2026-10" / "s1.jsonl").write_text(json.dumps({"_schema": 2}) + "\n", encoding="utf-8")
    (root / "near_miss_counts.json").write_text(json.dumps({"项目笔记/a.md": 3}), encoding="utf-8")
    (root / "nudge_ts.json").write_text("{}", encoding="utf-8")
    (root / "annotations.jsonl").write_text(json.dumps({"path": "a.md"}) + "\n", encoding="utf-8")
    (home / ".context-vault" / "config.json").write_text("{}", encoding="utf-8")
    return root


def test_purge_all_removes_retired_codex_namespace(tmp_home):
    root = _seed_retired(tmp_home)
    r = _run(tmp_home, "--runtime", "all", "--purge")
    assert r.returncode == 0, r.stderr
    assert not root.exists(), f"--purge 未清掉退役的 codex 命名空间：{r.stdout!r}"
    assert str(root) in r.stdout, "删除的目录必须出现在输出里（不可逆删除要让用户看见删了哪里）"
    assert "1 条人工标注" in r.stdout


def test_default_runtime_purge_on_canonical_removes_retired(tmp_home):
    """full-review P3A-M1：用户最常敲的是不带 `--runtime` 的 `--purge`；canonical 下
    `auto` 等价于 `all`，退役目录同样要清掉。"""
    root = _seed_retired(tmp_home)
    r = _run(tmp_home, "--purge")
    assert r.returncode == 0, r.stderr
    assert not root.exists(), f"默认 --runtime 下 --purge 未清掉退役目录：{r.stdout!r}"


def test_default_runtime_reset_counts_on_canonical_clears_retired(tmp_home):
    root = _seed_retired(tmp_home)
    r = _run(tmp_home, "--reset-counts")
    assert r.returncode == 0, r.stderr
    assert not (root / "near_miss_counts.json").exists()
    assert not (root / "nudge_ts.json").exists()
    assert (root / "annotations.jsonl").exists()


def test_purge_single_runtime_leaves_retired_namespace(tmp_home):
    """显式 `--runtime claude` 只处理 claude：退役目录只在 `all` 语义下清理。"""
    root = _seed_retired(tmp_home)
    r = _run(tmp_home, "--runtime", "claude", "--purge")
    assert r.returncode == 0, r.stderr
    assert root.exists()


def test_reset_counts_all_clears_retired_derived_files(tmp_home):
    root = _seed_retired(tmp_home)
    r = _run(tmp_home, "--runtime", "all", "--reset-counts")
    assert r.returncode == 0, r.stderr
    assert not (root / "near_miss_counts.json").exists()
    assert not (root / "nudge_ts.json").exists()
    assert (root / "annotations.jsonl").exists(), "reset-counts 只清派生数据，不碰人工标注"


def test_purge_does_not_follow_linked_retired_dir(tmp_home, tmp_path):
    """Review Focus 3：退役目录若是链接，不得跟随它删除别处的数据。"""
    target = tmp_path / "elsewhere"
    (target / "2026-10").mkdir(parents=True)
    keep = target / "2026-10" / "keep.jsonl"
    keep.write_text("{}\n", encoding="utf-8")
    link = tmp_home / ".context-vault" / "metrics" / "codex"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无权创建目录符号链接")
    (tmp_home / ".context-vault" / "config.json").write_text("{}", encoding="utf-8")
    r = _run(tmp_home, "--runtime", "all", "--purge")
    assert r.returncode == 0, r.stderr
    assert keep.exists(), "链接指向的数据被删除了"
    assert os.path.lexists(link), "链接本身也不应被删除"


@pytest.mark.skipif(os.name != "nt", reason="junction 只在 Windows 上存在")
def test_purge_does_not_follow_junction(tmp_home, tmp_path):
    """full-review P3A-M4：一般 Windows 机器上没有建符号链接的权限，上一条会 SKIP；
    不需要特权、真会出现的是 junction，由 `_is_link_like` 的 isjunction 分支负责。"""
    import _winapi
    target = tmp_path / "elsewhere"
    (target / "2026-10").mkdir(parents=True)
    keep = target / "2026-10" / "keep.jsonl"
    keep.write_text("{}\n", encoding="utf-8")
    link = tmp_home / ".context-vault" / "metrics" / "codex"
    link.parent.mkdir(parents=True)
    _winapi.CreateJunction(str(target), str(link))
    (tmp_home / ".context-vault" / "config.json").write_text("{}", encoding="utf-8")
    r = _run(tmp_home, "--runtime", "all", "--purge")
    assert r.returncode == 0, r.stderr
    assert keep.exists(), "junction 指向的数据被删除了"
    assert os.path.lexists(link), "junction 本身也不应被删除"


def test_runtime_codex_choice_is_removed(tmp_home):
    r = _run(tmp_home, "--runtime", "codex", "--report")
    assert r.returncode == 2
