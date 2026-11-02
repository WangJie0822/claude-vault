"""旧 Codex 数据保留；真实 CLI 只对在用代际做清理，不通过退役清理删掉 v2。"""
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


def test_purge_all_preserves_old_codex_and_purges_v2(tmp_home):
    root = _seed_retired(tmp_home)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    fresh = root / "v2/2026-10/new.jsonl"
    fresh.parent.mkdir(parents=True)
    fresh.write_text("{}\n", encoding="utf-8")
    r = _run(tmp_home, "--runtime", "all", "--purge")
    assert r.returncode == 0, r.stderr
    assert all(p.is_file() for p in before), "旧 Codex 文件被清理"
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not fresh.exists()
    assert str(root / "v2") in r.stdout


def test_default_runtime_purge_on_canonical_preserves_old_codex(tmp_home):
    """full-review P3A-M1：用户最常敲的是不带 `--runtime` 的 `--purge`；canonical 下
    `auto` 等价于 `all`，旧代际仍须保留。"""
    root = _seed_retired(tmp_home)
    r = _run(tmp_home, "--purge")
    assert r.returncode == 0, r.stderr
    assert (root / "2026-10/s1.jsonl").exists()
    assert (root / "annotations.jsonl").exists()


def test_default_runtime_reset_counts_preserves_old_codex(tmp_home):
    root = _seed_retired(tmp_home)
    r = _run(tmp_home, "--reset-counts")
    assert r.returncode == 0, r.stderr
    assert (root / "near_miss_counts.json").exists()
    assert (root / "nudge_ts.json").exists()
    assert (root / "annotations.jsonl").exists()


def test_purge_single_runtime_leaves_retired_namespace(tmp_home):
    """显式 `--runtime claude` 只处理 claude：退役目录只在 `all` 语义下清理。"""
    root = _seed_retired(tmp_home)
    r = _run(tmp_home, "--runtime", "claude", "--purge")
    assert r.returncode == 0, r.stderr
    assert root.exists()


def test_reset_counts_all_preserves_old_codex_derived_files(tmp_home):
    root = _seed_retired(tmp_home)
    r = _run(tmp_home, "--runtime", "all", "--reset-counts")
    assert r.returncode == 0, r.stderr
    assert (root / "near_miss_counts.json").exists()
    assert (root / "nudge_ts.json").exists()
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


def test_runtime_codex_report_is_available(tmp_home):
    r = _run(tmp_home, "--runtime", "codex", "--report")
    assert r.returncode == 0, r.stderr
