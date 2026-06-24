"""run_auto_summary.py 单元测试（TDD 驱动，纯 Python stdlib）。"""
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import run_auto_summary as r

# 计数器脚本路径（用于 glob 契约测试）
_COUNTER_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "skills/summarize-session/scripts/_count_drafts_and_failures.py"
)


def test_missing_claude_cli_graceful(monkeypatch, tmp_path):
    monkeypatch.setattr(r.shutil, "which", lambda name: None)  # claude 不在 PATH
    rc = r.run_once(queue_path=tmp_path / "auto-queue.jsonl", model="x")
    assert rc == 0  # 不崩溃


def test_empty_queue_noop(tmp_path):
    rc = r.run_once(queue_path=tmp_path / "auto-queue.jsonl", model="x")
    assert rc == 0


# ---------------------------------------------------------------------------
# C2/H1 fix: log filename is run-{date}-{sid}.log, counter can find it
# ---------------------------------------------------------------------------

def test_write_run_log_filename_format(tmp_path):
    """_write_run_log 写出 run-{YYYY-MM-DD}-{sid}.log 格式文件（C2 实证）。"""
    now = datetime(2026, 6, 23, 3, 0, 0, tzinfo=timezone.utc)
    r._write_run_log(tmp_path, "abc123", "failed", 1, _now=now)
    expected = tmp_path / "run-2026-06-23-abc123.log"
    assert expected.exists(), f"Expected {expected} to exist"
    content = expected.read_text(encoding="utf-8")
    assert "STATUS=failed" in content


def test_write_run_log_timeout_token(tmp_path):
    """timeout 状态写出 STATUS=timeout，与计数器 grep 契约一致。"""
    now = datetime(2026, 6, 23, 3, 0, 0, tzinfo=timezone.utc)
    r._write_run_log(tmp_path, "sid999", "timeout", 124, _now=now)
    log = tmp_path / "run-2026-06-23-sid999.log"
    content = log.read_text(encoding="utf-8")
    assert "STATUS=timeout" in content


def test_counter_glob_matches_run_log(tmp_path, monkeypatch):
    """_count_drafts_and_failures 的 glob 能命中 run-{yesterday}-*.log 格式（H1 实证）。"""
    import importlib.util, os

    # 导入计数器模块（从 scripts 目录动态加载）
    spec = importlib.util.spec_from_file_location("_count_drafts_and_failures", _COUNTER_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 将 SKILL_ROOT 指向 tmp_path（含 auto-runs 子目录）
    monkeypatch.setenv("AUTO_SKILL_ROOT", str(tmp_path))
    # 重载让模块感知新 env（SKILL_ROOT 在模块级求值）
    spec.loader.exec_module(mod)

    auto_runs = tmp_path / "auto-runs"
    auto_runs.mkdir()

    # 写昨天日期的日志文件
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    log_file = auto_runs / f"run-{yesterday}-testsid.log"
    log_file.write_text("2026-06-22T03:00:00 session=testsid STATUS=failed rc=1\n", encoding="utf-8")

    # 直接调用计数函数（绕过模块级 SKILL_ROOT，patch 内部变量）
    import importlib
    import sys
    # 重新 exec 让 SKILL_ROOT 读到新 env
    spec2 = importlib.util.spec_from_file_location("_cdf2", _COUNTER_SCRIPT)
    mod2 = importlib.util.module_from_spec(spec2)
    old_env = os.environ.get("AUTO_SKILL_ROOT")
    os.environ["AUTO_SKILL_ROOT"] = str(tmp_path)
    spec2.loader.exec_module(mod2)
    fail_count, log_path = mod2.count_yesterday_failures()
    if old_env is None:
        os.environ.pop("AUTO_SKILL_ROOT", None)
    else:
        os.environ["AUTO_SKILL_ROOT"] = old_env

    assert fail_count >= 1, f"Counter should detect STATUS=failed in log, got {fail_count}"


def test_rotate_logs_removes_old_run_logs(tmp_path):
    """_rotate_logs 删除超期的 run-*.log 文件（H1 rotation 实证）。"""
    log_dir = tmp_path / "auto-runs"
    log_dir.mkdir()

    old_log = log_dir / "run-2020-01-01-oldsid.log"
    old_log.write_text("old\n", encoding="utf-8")
    # 把 mtime 设到很久以前（确保超过 30 天 retention）
    old_time = time.time() - 35 * 86400
    import os
    os.utime(old_log, (old_time, old_time))

    new_log = log_dir / "run-2026-06-23-newsid.log"
    new_log.write_text("new\n", encoding="utf-8")

    r._rotate_logs(log_dir, retention_days=30)

    assert not old_log.exists(), "Old log should be rotated"
    assert new_log.exists(), "New log should be kept"
