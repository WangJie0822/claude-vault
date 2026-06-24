"""工作日志路径 helper：按 YYYY年/MM月/YYYY-MM-DD.md 三级结构生成路径。"""
from __future__ import annotations

import re
from pathlib import Path

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def worklog_path(log_dir: Path | str, date_str: str) -> Path:
    """date_str 形如 '2026-05-19' → log_dir/2026年/05月/2026-05-19.md。

    Raises:
        ValueError: date_str 不是严格 YYYY-MM-DD（含两位月/日补零）格式时
    """
    m = _ISO_DATE_RE.match(date_str)
    if not m:
        raise ValueError(f"date_str 必须是 YYYY-MM-DD 格式: {date_str!r}")
    year, month, _ = m.groups()
    return Path(log_dir) / f"{year}年" / f"{month}月" / f"{date_str}.md"
