"""`_metrics` 的命名空间白名单（2.0.0 收为 {"claude"}）：codex 不得再造出 metrics 目录。

`_metrics._RUNTIME` 与 `_state._RUNTIME` 是两个独立的进程级全局，白名单各写一份；
`_state` 那两处有 `test_topic_namespace.py` 的 hostile 参数化守着，本文件守这一处。
"""
from __future__ import annotations

from pathlib import Path

from scripts import _metrics


def test_codex_is_not_a_metrics_namespace(tmp_home: Path) -> None:
    try:
        _metrics.configure_context("codex")
        assert _metrics.metrics_dir(tmp_home) == tmp_home / ".claude" / "vault-loader-metrics"
        _metrics.configure_context("claude")
        assert _metrics.metrics_dir(tmp_home) == \
            tmp_home / ".context-vault" / "metrics" / "claude", "阳性对照：claude 应走 canonical"
    finally:
        _metrics.configure_context("legacy")
