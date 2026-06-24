"""自动总结运行器（跨平台 Python 化，替代 run_auto_summary.sh）。

凌晨定时任务入口（schtasks / systemd 等调用）：
  python run_auto_summary.py [--queue <path>] [--model <model>]

主入口 run_once(queue_path, model) -> int:
  - 0: 成功 / 队列空 / claude CLI 不在 PATH（fail-open，不崩溃）
  - 无 bash/GNU 依赖，纯 Python stdlib。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量与默认值
# --------------------------------------------------------------------------- #
DEFAULT_TIMEOUT_SEC = 480   # 单次 claude 调用超时（秒）
LOG_NAME = "run_auto_summary"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
_log = logging.getLogger(LOG_NAME)


# --------------------------------------------------------------------------- #
# 内联出队逻辑（复用 _auto_queue.py 的 dequeue_by_session）
# --------------------------------------------------------------------------- #

def _load_auto_queue_module(queue_path: Path):
    """动态导入与 queue_path 同目录的 _auto_queue（或 skills 内的副本）。"""
    # 优先从技能目录加载（skills/summarize-session/scripts/_auto_queue.py）
    candidates = [
        # 如果从插件根调用：scripts/ 旁边没有 _auto_queue，向上找 skills 目录
        Path(__file__).resolve().parent.parent
        / "skills/summarize-session/scripts/_auto_queue.py",
        # 与 queue_path 同目录（单元测试场景）
        queue_path.parent / "_auto_queue.py",
    ]
    for p in candidates:
        if p.exists():
            import importlib.util
            spec = importlib.util.spec_from_file_location("_auto_queue", p)
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    return None


def _read_queue(queue_path: Path) -> list[dict]:
    """读取 jsonl 队列，返回条目列表（空/缺失文件 → []）。"""
    if not queue_path.exists():
        return []
    text = queue_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    entries = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _dequeue(queue_path: Path, session_id: str) -> None:
    """从队列移除指定 session_id。优先用 _auto_queue 模块；降级为内联实现。"""
    mod = _load_auto_queue_module(queue_path)
    if mod is not None:
        try:
            mod.dequeue_by_session(queue_path, [session_id])
            return
        except Exception as exc:
            _log.warning("dequeue_by_session failed (%s), falling back to inline", exc)

    # 内联降级：过滤后原子重写
    import os
    import tempfile

    entries = _read_queue(queue_path)
    remaining = [e for e in entries if e.get("session_id") != session_id]
    fd, tmp = tempfile.mkstemp(
        prefix=queue_path.name + ".", suffix=".tmp", dir=queue_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for e in remaining:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, queue_path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _increment_failure(queue_path: Path, session_id: str) -> int:
    """对指定会话 failure_count +1，返回新值；失败返回 -1。"""
    mod = _load_auto_queue_module(queue_path)
    if mod is not None:
        try:
            return mod.increment_failure(queue_path, session_id)
        except Exception as exc:
            _log.warning("increment_failure failed (%s)", exc)
            return -1
    return -1


# --------------------------------------------------------------------------- #
# 日志写入
# --------------------------------------------------------------------------- #

def _write_run_log(
    log_dir: Path,
    sid: str,
    status: str,
    rc: int,
    detail: str = "",
    *,
    _now: datetime | None = None,
) -> None:
    """追加一条运行记录到 auto-runs/run-{YYYY-MM-DD}-{sid}.log。

    文件名格式 run-{date}-{sid}.log 与 _count_drafts_and_failures.py 的
    glob run-{yesterday}-*.log 及 _rotate_logs 的 run-*.log 对齐（C2/H1 修复）。
    _now 参数供测试注入（避免跨日期边界问题）。
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    now = _now or datetime.now().astimezone()
    date_str = now.strftime("%Y-%m-%d")
    ts = now.isoformat(timespec="seconds")
    line = f"{ts} session={sid} STATUS={status} rc={rc}"
    if detail:
        line += f" detail={detail}"
    run_log = log_dir / f"run-{date_str}-{sid}.log"
    try:
        with run_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        _log.warning("cannot write run log: %s", exc)


# --------------------------------------------------------------------------- #
# 核心：run_once
# --------------------------------------------------------------------------- #

def run_once(
    queue_path: Path,
    model: str,
    *,
    log_dir: Path | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    max_failure: int = 3,
) -> int:
    """出队一条会话并调用 claude 总结。

    Returns:
        0  — 成功 / 队列空 / claude CLI 不在 PATH（fail-open）
        非 0 — 意外异常（已被外层 try/except 兜底）
    """
    try:
        # ① claude CLI 可用性探测
        if shutil.which("claude") is None:
            _log.warning("claude CLI not found in PATH, skip")
            return 0

        # ② 队列空/缺失
        entries = _read_queue(queue_path)
        if not entries:
            _log.info("queue empty, nothing to do")
            return 0

        # ③ 取第一条
        entry = entries[0]
        sid = entry.get("session_id", "")
        if not sid:
            _log.warning("first queue entry has no session_id, skipping")
            return 0

        _log.info("processing session=%s", sid)

        # ④ 调用 claude
        cmd = [
            "claude",
            "-p",
            f"/summarize-session --auto --session {sid}",
            "--model",
            model,
        ]
        try:
            proc = subprocess.run(
                cmd,
                timeout=timeout_sec,
                encoding="utf-8",
                capture_output=True,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            _log.warning("session=%s timed out after %ss", sid, timeout_sec)
            rc = 124  # 与旧 _timeout.py 语义一致

        # ⑤ 结果处理
        if log_dir is None:
            log_dir = queue_path.parent / "auto-runs"

        if rc == 0:
            _log.info("STATUS=success session=%s", sid)
            _write_run_log(log_dir, sid, "success", rc)
            _dequeue(queue_path, sid)
        elif rc == 124:
            _log.warning("STATUS=timeout session=%s", sid)
            _write_run_log(log_dir, sid, "timeout", rc)
            new_fc = _increment_failure(queue_path, sid)
            if new_fc >= max_failure:
                _log.warning("STATUS=permanent_skip session=%s failure_count=%d", sid, new_fc)
                _dequeue(queue_path, sid)
        else:
            _log.warning("STATUS=failed session=%s rc=%d", sid, rc)
            _write_run_log(log_dir, sid, "failed", rc)
            new_fc = _increment_failure(queue_path, sid)
            if new_fc >= max_failure:
                _log.warning("STATUS=permanent_skip session=%s failure_count=%d", sid, new_fc)
                _dequeue(queue_path, sid)

        return 0

    except Exception as exc:  # noqa: BLE001
        _log.exception("run_once unexpected error: %s", exc)
        return 0  # fail-open


# --------------------------------------------------------------------------- #
# CLI 入口
# --------------------------------------------------------------------------- #

def _load_config(skill_root: Path) -> dict:
    """加载 skill_root/config.json，缺失时返回空 dict。"""
    config_path = skill_root / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("cannot read config.json: %s", exc)
        return {}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Auto-summarize one queued session.")
    parser.add_argument("--queue", type=Path, default=None, help="Path to auto-queue.jsonl")
    parser.add_argument("--model", type=str, default=None, help="Model to use for summarization")
    parser.add_argument(
        "--skill-root",
        type=Path,
        default=None,
        help="Skill root directory (default: ~/.claude/skills/summarize-session)",
    )
    args = parser.parse_args(argv)

    # 确定技能根目录
    skill_root: Path = args.skill_root or (
        Path.home() / ".claude" / "skills" / "summarize-session"
    )

    # 读取配置
    cfg = _load_config(skill_root)
    auto_cfg: dict = cfg.get("auto", {})

    # 是否启用
    if not auto_cfg.get("enabled", False):
        _log.info("auto disabled in config, exit")
        return 0

    # 暂停检查
    if (skill_root / ".auto-paused").exists():
        _log.info("paused (.auto-paused exists), exit")
        return 0

    # 参数合并（CLI 优先 > config > 默认值）
    model: str = args.model or auto_cfg.get("model", "claude-sonnet-4-6")
    queue_path: Path = args.queue or (skill_root / "auto-queue.jsonl")
    timeout_sec: int = int(auto_cfg.get("session_timeout_sec", DEFAULT_TIMEOUT_SEC))
    max_failure: int = int(auto_cfg.get("max_failure_count", 3))
    log_dir: Path = skill_root / "auto-runs"

    # 日志轮转（保留 N 天）
    log_retention_days: int = int(auto_cfg.get("log_retention_days", 30))
    _rotate_logs(log_dir, log_retention_days)

    _log.info("=== run_auto_summary started at %s ===", datetime.now().isoformat(timespec="seconds"))

    rc = run_once(
        queue_path=queue_path,
        model=model,
        log_dir=log_dir,
        timeout_sec=timeout_sec,
        max_failure=max_failure,
    )

    _log.info("=== run_auto_summary finished rc=%d ===", rc)
    return rc


def _rotate_logs(log_dir: Path, retention_days: int) -> None:
    """删除 log_dir 内超过 retention_days 天的 run-*.log 文件。"""
    if not log_dir.exists():
        return
    import time

    cutoff = time.time() - retention_days * 86400
    for p in log_dir.glob("run-*.log"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                _log.debug("rotated old log: %s", p)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
