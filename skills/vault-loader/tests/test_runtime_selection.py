"""`analyze_metrics.py` 的命名空间选择：同一个目录只能被处理一次。

canonical `config.json` 存在而迁移未提交（没有 `migration.json`、有 legacy 数据）时，
`--runtime auto` 取 `all` 展开成 legacy 与 claude 两项，而 `_metrics.configure_context`
在这种机器上把 claude 映射回 legacy——两项指向同一个目录。full-review P2B-E1 实测
`--report` 因此把事件数报成两倍，`--purge` 的「扫描目录」把同一路径打印两遍。

走真实 CLI 子进程：命名空间是在 `main()` 里选定的。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_metrics.py"


def _run(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONUTF8": "1"})
    # 显式给空 stdin：继承 pytest 的 stdin 时，是否被判成 TTY 取决于启动方式
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *args], input="",
                          capture_output=True, text=True, env=env, encoding="utf-8",
                          cwd=str(SCRIPT.parents[1]), timeout=60)


def _write_events(month_dir: Path, schema: int, n: int) -> None:
    month_dir.mkdir(parents=True)
    rec = {"_schema": schema, "gate": "ok", "admitted": [], "near_miss": []}
    (month_dir / "s1.jsonl").write_text("".join(json.dumps(rec) + "\n" for _ in range(n)),
                                        encoding="utf-8")


def _seed_legacy(home: Path, n: int = 3) -> Path:
    root = home / ".claude" / "vault-loader-metrics"
    _write_events(root / "2026-10", 1, n)
    return root


def _write_canonical_config(home: Path) -> None:
    (home / ".context-vault").mkdir(parents=True, exist_ok=True)
    (home / ".context-vault" / "config.json").write_text("{}", encoding="utf-8")


def _n_events(stdout: str) -> int:
    m = re.search(r"事件数 (\d+)", stdout)
    assert m, f"报表里没有「事件数」：{stdout!r}"
    return int(m.group(1))


@pytest.mark.parametrize("runtime", [None, "all"])
def test_report_counts_unmigrated_legacy_once(tmp_home, runtime):
    _seed_legacy(tmp_home, 3)
    _write_canonical_config(tmp_home)
    args = ["--report"] + (["--runtime", runtime] if runtime else [])
    r = _run(tmp_home, *args)
    assert r.returncode == 0, r.stderr
    assert _n_events(r.stdout) == 3


def test_purge_lists_unmigrated_legacy_dir_once(tmp_home):
    root = _seed_legacy(tmp_home, 3)
    _write_canonical_config(tmp_home)
    r = _run(tmp_home, "--purge")
    assert r.returncode == 0, r.stderr
    assert r.stdout.count(str(root)) == 1, r.stdout


def test_review_needs_no_explicit_runtime_when_only_one_dir(tmp_home):
    """去重后只剩一个实际命名空间，`--review` 不再报「必须显式指定 --runtime」。
    stdin 不是 TTY，所以它随后在交互检查处中止——这里只看它越过了命名空间那一关。"""
    _seed_legacy(tmp_home, 1)
    _write_canonical_config(tmp_home)
    r = _run(tmp_home, "--review")
    assert "必须显式指定" not in r.stderr, r.stderr
    assert "TTY" in r.stderr, r.stderr


def test_report_after_migration_still_reads_both_namespaces(tmp_home):
    """迁移提交后 legacy 与 claude 是两个目录，两边的事件都要统计。"""
    _seed_legacy(tmp_home, 3)
    _write_canonical_config(tmp_home)
    (tmp_home / ".context-vault" / "migration.json").write_text(
        json.dumps({"status": "committed"}), encoding="utf-8")
    _write_events(tmp_home / ".context-vault" / "metrics" / "claude" / "2026-10", 2, 2)
    r = _run(tmp_home, "--report")
    assert r.returncode == 0, r.stderr
    assert _n_events(r.stdout) == 5


@pytest.mark.skipif(os.name != "nt", reason="junction 只在 Windows 上存在")
def test_report_counts_junctioned_namespace_once(tmp_home):
    """legacy 目录是指向 claude 目录的 junction 时，两项是同一份数据，只统计一次。"""
    import _winapi
    _write_canonical_config(tmp_home)
    (tmp_home / ".context-vault" / "migration.json").write_text(
        json.dumps({"status": "committed"}), encoding="utf-8")
    claude = tmp_home / ".context-vault" / "metrics" / "claude"
    _write_events(claude / "2026-10", 2, 2)
    legacy = tmp_home / ".claude" / "vault-loader-metrics"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(claude), str(legacy))
    r = _run(tmp_home, "--report")
    assert r.returncode == 0, r.stderr
    assert _n_events(r.stdout) == 2
