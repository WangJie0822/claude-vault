# tests/test_fresh_install_e2e.py
import json, os, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


def _run_hook(rel, stdin_obj, home):
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT),
           "USERPROFILE": str(home), "HOME": str(home), "PYTHONUTF8": "1"}
    r = subprocess.run([sys.executable, str(ROOT / rel)],
                       input=json.dumps(stdin_obj), capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=30)
    return r


def test_session_start_fresh_user_exit0(tmp_path):
    r = _run_hook("skills/vault-loader/scripts/session_start_load.py",
                  {"cwd": str(tmp_path)}, tmp_path)
    assert r.returncode == 0  # fail-open
    # stdout 要么空，要么合法 JSON（不得是 traceback）
    if r.stdout.strip():
        json.loads(r.stdout)


def test_user_prompt_submit_fresh_user_exit0(tmp_path):
    r = _run_hook("skills/vault-loader/scripts/prompt_submit_load.py",
                  {"prompt": "hello", "cwd": str(tmp_path)}, tmp_path)
    assert r.returncode == 0


def test_session_end_no_spawn_default(tmp_path):
    r = _run_hook("hooks/session_end_enqueue.py",
                  {"session_id": "s1", "cwd": str(tmp_path)}, tmp_path)
    assert r.returncode == 0  # auto 默认关，不 spawn 也 exit 0
