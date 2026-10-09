"""CLI model backend used by optional, user-triggered enrichment tasks."""
from __future__ import annotations

import os
import shutil
import subprocess


def call_claude(prompt: str, *, timeout: int = 60,
                model: str = "haiku") -> str | None:
    executable = shutil.which("claude")
    if executable is None:
        return None
    env = dict(os.environ)
    env["VAULT_LOADER_DISABLE"] = "1"
    try:
        result = subprocess.run(
            [executable, "-p", "--model", model, "--tools", "",
             "--no-session-persistence"], input=prompt,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, shell=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return result.stdout if result.returncode == 0 else None


def choose_backend(requested: str = "auto") -> str:
    """解析 enrich 的模型后端。2.0.0 起只有 claude。

    `auto` 只在有 `CLAUDE_PLUGIN_ROOT` 的进程里判为 claude——Claude Code 给 hook 子进程导出
    该变量；skill 让模型在 Bash 工具里手动运行时没有它（2026-10-08 实测），普通终端同理，
    这两种情况都拒绝。`enrich_keywords.py` 的 `--limit` 默认不限量，把 auto 直接等同
    claude 会让一次误跑把整个 Vault 里缺 keywords 的笔记逐篇交给付费模型。
    """
    if requested == "claude":
        return "claude"
    if requested != "auto":
        raise ValueError(f"unsupported backend: {requested}")
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "claude"
    raise ValueError("cannot infer model backend outside a hook; pass --backend claude")
