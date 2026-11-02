"""Codex 模型后端；只依赖标准库，亦可按绝对文件路径加载。"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

LAST_FAIL = None
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# Task1 在两份 Windows 引擎上与 profile/provider/认证路由组合验证。
_DISABLED = ("hooks", "shell_tool", "unified_exec", "apps", "browser_use", "computer_use",
    "image_generation", "view_image", "in_app_browser", "multi_agent", "multi_agent_v2", "code_mode",
    "code_mode_host", "plugins", "remote_plugin", "skill_search", "tool_suggest", "sleep_tool", "goals",
    "workspace_dependencies", "memories")
_SETTINGS = ("project_doc_max_bytes=0", "skills.include_instructions=false", "skills.bundled.enabled=false",
    'web_search="disabled"', "tools.update_plan.enabled=false", "tools.experimental_request_user_input.enabled=false",
    "plugins._default.enabled=false")


def find_codex():
    # 手工枚举绝对 PATH，避免 Python 3.9 的 Windows which 优先查找 cwd。
    suffixes = (".exe", ".cmd", ".bat") if os.name == "nt" else ("",)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or not Path(directory).is_absolute():
            continue
        for suffix in suffixes:
            path = Path(directory) / ("codex" + suffix)
            if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
                return str(path)
    return None


def cleanup(directory):
    shutil.rmtree(directory, ignore_errors=True)


def _no_window_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# 进程树策略与 _topic#_run_tree_capped 一致；调用方可注入该 runner。
# Job Object 建不成时的兜底：按绝对路径调用系统 taskkill，不按 PATH 查找
_TASKKILL = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "taskkill.exe")


def _win_kernel32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.TerminateJobObject.restype = wintypes.BOOL
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return ctypes, wintypes, k32


def _win_new_job():
    """建一个带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 的 Job Object，返回句柄；任何失败返回 None。"""
    try:
        ctypes, wintypes, k32 = _win_kernel32()

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _Ext(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _Ext()
        info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        # 9 = JobObjectExtendedLimitInformation
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return None
        return job
    except Exception:  # noqa: BLE001 — 建不成就退回 taskkill
        return None


def _win_assign(job, pid: int) -> bool:
    try:
        _ct, _wt, k32 = _win_kernel32()
        h = k32.OpenProcess(0x0001 | 0x0100, False, pid)       # PROCESS_TERMINATE | PROCESS_SET_QUOTA
        if not h:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(job, h))
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return False


def _win_close_job(job, *, kill: bool) -> None:
    try:
        _ct, _wt, k32 = _win_kernel32()
        if kill:
            k32.TerminateJobObject(job, 1)
        k32.CloseHandle(job)
    except Exception:  # noqa: BLE001
        pass


def _kill_tree(proc, job) -> None:
    if os.name == "nt":
        if job:
            _win_close_job(job, kill=True)
        else:
            try:
                subprocess.run([_TASKKILL, "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                               timeout=30, creationflags=_no_window_flags())
            except (subprocess.TimeoutExpired, OSError):
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run_tree_capped(argv, input_text: str, timeout: float, env: dict, cwd: str):
    """起 claude，并把 timeout 做成**整棵进程树**的硬上限（spec §6.2 / F03 / F69）。返回 `(returncode, stdout)`。

    超时或被中断时先杀树再原样抛出（`subprocess.TimeoutExpired` 等）。只杀直接子进程不够：npm 的
    `claude.CMD` 垫片下真正干活的是 node 孙进程，`subprocess.run(timeout=1)` 实测 6.1 s 才返回（spec §2）。
    - Windows：Job Object 在 Popen 之前建好、子进程创建后立即加入；Job 内进程派生的子孙自动入 Job，
      cmd 先退出、node 成孤儿时也能一并杀掉（`taskkill /T` 按父 pid 遍历不到它）。加入前那几毫秒里若已
      派生孙进程，它不在 Job 内——已知残余；Job 建不成时退回 `taskkill /T`。正常结束也关闭 Job，
      KILL_ON_JOB_CLOSE 顺带清掉残留子孙。
    - POSIX：新会话 + `os.killpg`，只在超时 / 中断时杀；正常结束不清进程组（`-p` 正常退出时没有残留）。
    """
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = _no_window_flags()
    else:
        kw["start_new_session"] = True
    job = _win_new_job() if os.name == "nt" else None
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", env=env, cwd=cwd,
                                shell=False, **kw)
    except BaseException:
        if job:
            _win_close_job(job, kill=False)
        raise
    if job and not _win_assign(job, proc.pid):
        _win_close_job(job, kill=False)
        job = None
    try:
        out, _err = proc.communicate(input_text, timeout=timeout)
    except BaseException:
        _kill_tree(proc, job)
        job = None
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise
    if job:
        _win_close_job(job, kill=False)
    return proc.returncode, out


def call_codex(prompt, *, selection, timeout=120, schema=None, runner=None):
    """保留同一 CODEX_HOME/profile/provider；隔离发现失败时不发起模型调用。"""
    global LAST_FAIL
    LAST_FAIL = None
    directory = None
    try:
        model, profile, provider = (selection.get(k) for k in ("model", "profile", "provider"))
        for value, pattern in ((model, _MODEL), (profile, _NAME), (provider, _NAME)):
            if value is not None and (not isinstance(value, str) or not pattern.fullmatch(value)):
                raise ValueError("invalid selection")
        if (not isinstance(prompt, str) or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("invalid call")
        executable = find_codex()
        if executable is None:
            LAST_FAIL = "no_codex"
            return None
        directory = tempfile.mkdtemp(prefix="cv-codex-")
        env = dict(os.environ, VAULT_LOADER_DISABLE="1", NoDefaultCurrentDirectoryInExePath="1")
        options = []
        if profile:
            options += ["--profile", profile]
        if provider:
            options += ["-c", "model_provider=" + json.dumps(provider)]
        for feature in _DISABLED:
            options += ["--disable", feature]
        options += ["--enable", "skip_host_skill_discovery"]
        for setting in _SETTINGS:
            options += ["-c", setting]
        run = runner or (lambda argv, *, input, timeout, env, cwd: _run_tree_capped(argv, input, timeout, env, cwd))
        deadline = time.monotonic() + timeout
        rc, discovered = run([executable, *options, "-C", directory, "mcp", "list", "--json"],
            input="", timeout=min(15, timeout), env=env, cwd=directory)
        try:
            servers = json.loads(discovered) if rc == 0 else None
            if (not isinstance(servers, list) or len(servers) > 1024
                    or any(not isinstance(s, dict) or not isinstance(s.get("name"), str)
                           or not 0 < len(s["name"]) <= 512 for s in servers)):
                raise ValueError("invalid MCP discovery")
        except (ValueError, TypeError):
            LAST_FAIL = "isolation_failed"
            return None
        disabled = "mcp_servers={" + ",".join(json.dumps(s["name"]) + "={enabled=false}" for s in servers) + "}"
        output = Path(directory) / "result.txt"
        argv = [executable, "exec", "-", *options, "-c", disabled, "--skip-git-repo-check", "--ephemeral",
            "--color", "never", "--json", "-C", directory, "--sandbox", "read-only", "--ignore-rules",
            "-c", 'approval_policy="never"', "--output-last-message", str(output)]
        if model is not None:
            argv += ["--model", model]
        if schema is not None:
            schema_path = Path(directory) / "schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            argv += ["--output-schema", str(schema_path)]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        rc, _ = run(argv, input=prompt, timeout=remaining, env=env, cwd=directory)
        if rc != 0:
            LAST_FAIL = "exit_nonzero"
            return None
        if not output.is_file() or output.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("invalid output")
        return output.read_text(encoding="utf-8")
    except subprocess.TimeoutExpired:
        LAST_FAIL = "timeout"
        return None
    except Exception:
        LAST_FAIL = "error"
        return None
    finally:
        if directory is not None:
            try:
                cleanup(directory)
            except OSError:
                pass
