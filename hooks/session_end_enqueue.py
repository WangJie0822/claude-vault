#!/usr/bin/env python3
"""SessionEnd hook：把当前会话异步入队待自动总结。

行为等价复刻旧 session-end-enqueue.sh（spec §5 M4：detached 异步、绝不阻塞退出）：
- 读 stdin JSON 取 session_id / cwd
- session_id 缺失 → exit 0（不 spawn）
- cwd 缺失 → 用 os.getcwd()（复刻旧 `CWD="$PWD"`）
- 用 subprocess.Popen detached 起入队脚本，stdout/stderr 重定向到 enqueue.log（append），
  main 立即返回不 wait（复刻旧 `nohup ... &`）
"""
import os
import subprocess
import sys
from pathlib import Path

# H6：Windows python 默认编码可能非 utf-8，先 reconfigure（与其它 hook 一致）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# H7：兜住 helper import 期故障，避免拖垮会话
try:
    from _hook_common import read_stdin_json, fail_open
except Exception:
    sys.exit(0)

# 入队脚本路径：优先 CLAUDE_PLUGIN_ROOT 环境变量，回退到相对 __file__ 定位
_PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parent.parent)
_ENQUEUE_SCRIPT = os.path.join(
    _PLUGIN_ROOT, "skills", "summarize-session", "scripts", "enqueue_auto_summary.py"
)
# 默认与旧 .sh 等价；测试可经 env 覆盖（端到端跨进程，monkeypatch 无法穿透 subprocess）
_HOME = os.path.expanduser("~")
_LOG_DIR = os.environ.get(
    "CLAUDE_AUTO_RUNS_DIR",
    os.path.join(_HOME, ".claude", "skills", "summarize-session", "auto-runs"),
)

# M4：Windows detached 创建标志，子进程脱离父会话、不被 hook 退出连带回收
_DETACHED_FLAGS = (
    getattr(subprocess, "DETACHED_PROCESS", 0)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
)


def spawn_enqueue(session_id, cwd):
    """detached 异步起入队脚本，stdout/stderr append 到 enqueue.log；立即返回不 wait（M4）。

    返回 Popen（测试可断言），失败抛异常由 fail_open 吞掉。
    """
    os.makedirs(_LOG_DIR, exist_ok=True)
    log_path = os.path.join(_LOG_DIR, "enqueue.log")
    args = [
        sys.executable,
        _ENQUEUE_SCRIPT,
        "--session", session_id,
        "--cwd", cwd,
    ]
    log = open(log_path, "ab")  # noqa: SIM115 — detached 子进程持有句柄，不能 with 即关
    popen_kwargs = dict(stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if os.name == "nt":
        popen_kwargs["creationflags"] = _DETACHED_FLAGS
    else:
        popen_kwargs["start_new_session"] = True
    # M4：直接用 subprocess.Popen（非 run_subprocess——那会 wait+timeout），
    # 起完立即返回，绝不 wait()，保证不阻塞会话退出。
    proc = subprocess.Popen(args, shell=False, **popen_kwargs)
    return proc


def _auto_enabled():
    """读 summarize-session config.json 的 auto.enabled 字段；缺失/异常一律返回 False（opt-in）。

    - 模块（_auto_config）从 CLAUDE_PLUGIN_ROOT（插件代码目录）加载
    - config.json 从用户态路径加载（AUTO_SKILL_ROOT env 或 ~/.claude/skills/summarize-session），
      与 enqueue_auto_summary._skill_root() 和 run_auto_summary.main() 的默认值保持一致。
    """
    try:
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parent.parent)
        sys.path.insert(0, os.path.join(plugin_root, "skills", "summarize-session", "scripts"))
        from _auto_config import load_auto_config
        # C1 fix: config.json 读用户态路径，而非插件缓存目录
        user_skill_root = os.environ.get(
            "AUTO_SKILL_ROOT",
            os.path.join(os.path.expanduser("~"), ".claude", "skills", "summarize-session"),
        )
        cfg_path = Path(user_skill_root) / "config.json"
        return bool(load_auto_config(cfg_path).enabled)
    except Exception:
        return False  # 缺失/异常一律视为关闭（auto-mode 默认 opt-in）


def main():
    data = read_stdin_json()
    session_id = data.get("session_id", "") if isinstance(data, dict) else ""
    cwd = data.get("cwd", "") if isinstance(data, dict) else ""
    main_with(session_id if isinstance(session_id, str) else "",
              cwd if isinstance(cwd, str) else "")


def main_with(session_id, cwd):
    # 字段缺失就跳过（复刻旧 `[ -z "$SESSION_ID" ] && exit 0`）
    if not session_id:
        return
    if not _auto_enabled():
        return  # auto-mode 未 opt-in，不 spawn
    if not cwd:
        cwd = os.getcwd()  # 复刻旧 `CWD="$PWD"`
    spawn_enqueue(session_id, cwd)


if __name__ == "__main__":
    fail_open(main)
