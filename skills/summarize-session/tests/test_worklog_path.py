"""worklog_path helper 单测：路径生成与输入校验。"""
from pathlib import Path

import pytest

from scripts._worklog_path import worklog_path


def test_worklog_path_basic():
    assert worklog_path(Path("/x"), "2026-05-19") == Path("/x/2026年/05月/2026-05-19.md")


def test_worklog_path_zero_padded_month():
    assert worklog_path(Path("/x"), "2026-01-05") == Path("/x/2026年/01月/2026-01-05.md")


def test_worklog_path_rejects_non_iso_slash():
    with pytest.raises(ValueError):
        worklog_path(Path("/x"), "2026/05/19")


def test_worklog_path_rejects_short_form():
    with pytest.raises(ValueError):
        worklog_path(Path("/x"), "26-5-19")


def test_worklog_path_rejects_empty():
    with pytest.raises(ValueError):
        worklog_path(Path("/x"), "")


def test_worklog_path_accepts_string_log_dir():
    """log_dir 传 str 也能工作（兼容性）。"""
    assert worklog_path(Path("/x"), "2026-12-31") == Path("/x/2026年/12月/2026-12-31.md")
