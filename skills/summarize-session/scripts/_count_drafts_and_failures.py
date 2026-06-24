"""扫描草稿数量和昨夜失败数,输出供 hook 使用。

用法: python3 _count_drafts_and_failures.py
输出(JSON 单行):
  {"drafts": 3, "failures_yesterday": 2, "log_file": "auto-runs/run-2026-04-22-023000.log"}
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

SKILL_ROOT = Path(os.environ.get(
    "AUTO_SKILL_ROOT", str(Path.home() / ".claude" / "skills" / "summarize-session")
))


def count_drafts() -> int:
    drafts_root = SKILL_ROOT / "auto-drafts"
    if not drafts_root.exists():
        return 0
    return len(list(drafts_root.glob("*/*.draft.md")))


def count_yesterday_failures() -> tuple[int, str]:
    log_dir = SKILL_ROOT / "auto-runs"
    if not log_dir.exists():
        return 0, ""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%F")
    matches = sorted(log_dir.glob(f"run-{yesterday}-*.log"))
    if not matches:
        return 0, ""
    fail = 0
    for f in matches:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            fail += text.count("STATUS=failed") + text.count("STATUS=timeout")
        except OSError:
            continue
    return fail, str(matches[-1])


if __name__ == "__main__":
    n_drafts = count_drafts()
    n_fail, log_file = count_yesterday_failures()
    print(json.dumps({
        "drafts": n_drafts,
        "failures_yesterday": n_fail,
        "log_file": log_file,
    }, ensure_ascii=False))
