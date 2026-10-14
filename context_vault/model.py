"""CLI model backend used by optional, user-triggered enrichment tasks."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile

from context_vault.exe_search import which_no_cwd

# 不加载用户 / 项目 / 本地的 settings、CLAUDE.md 与 hooks，不加载 MCP 与 skill：
# 实测单次输入 117,438 → 30,866 token（2026-10-13，单样本，claude 2.1.296）。
_ISOLATION = ["--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands"]
# 不得以 `-` 开头（否则会被解析成额外的命令行选项），且不含 cmd 元字符（npm 的 .CMD shim 经 cmd.exe）
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]*")


def _kill_tree(pid: int) -> None:
    """杀整棵进程树：经 npm 的 .CMD 垫片时真正干活的 node 是孙进程，只杀直接子进程它照样跑完、照样计费。"""
    if os.name == "nt":
        exe = which_no_cwd("taskkill")
        if exe:
            try:
                subprocess.run([exe, "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30)
            except (subprocess.TimeoutExpired, OSError):
                pass
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


def _run_capped(argv, *, input, timeout, env, cwd):
    """subprocess.run 的 timeout 只杀直接子进程，之后还要等孙进程关掉管道（实测 timeout=2 等了 8 s 以上）。
    超时或被中断时杀整棵进程树，让 timeout 成为硬上限；正常结束时与 subprocess.run 等价。"""
    kw = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", env=env, cwd=cwd, shell=False, **kw)
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except BaseException:
        _kill_tree(proc.pid)
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def call_claude(prompt: str, *, timeout: int = 60,
                model: str = "haiku") -> str | None:
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
        return None
    executable = which_no_cwd("claude")
    if executable is None:
        return None
    env = dict(os.environ)
    env["VAULT_LOADER_DISABLE"] = "1"
    try:
        cwd = tempfile.mkdtemp(prefix="cv-claude-")
    except OSError:
        return None
    try:
        result = _run_capped(
            [executable, "-p", "--model", model, "--tools", "",
             "--no-session-persistence", *_ISOLATION], input=prompt,
            timeout=timeout, env=env, cwd=cwd,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    finally:
        # 清理失败（Windows 上后代进程仍占用 cwd）不得吞掉已付费的结果；
        # TemporaryDirectory 的清理异常会落进上面的 except OSError（3.10 才有 ignore_cleanup_errors）
        shutil.rmtree(cwd, ignore_errors=True)
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
