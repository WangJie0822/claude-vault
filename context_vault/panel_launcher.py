"""可信本地入口准备新面板；启动秘密仅经匿名管道传给子进程。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from . import control

ROOT = Path(__file__).resolve().parents[1]
READY_TIMEOUT = 5


def prepare_panel(ref, home):
    attachment = control.attach(ref, home=home)
    capability = attachment["capability"]
    ticket = secrets.token_urlsafe(32)
    flags = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
             if os.name == "nt" else {"start_new_session": True})
    proc, reader, prepared = None, None, False
    try:
        proc = subprocess.Popen([sys.executable, "-m", "context_vault.panel_server"], cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **flags)
        proc.stdin.write(json.dumps({"capability": capability, "launch_ticket": ticket,
            "thread_id": ref.thread_id, "home": str(home)}).encode("utf-8"))
        proc.stdin.close()
        ready = queue.Queue()
        def read_ready():
            try:
                ready.put(proc.stdout.readline(4096))
            except (OSError, ValueError):
                ready.put(b"")
        reader = threading.Thread(target=read_ready, daemon=True)
        reader.start()
        info = json.loads(ready.get(timeout=READY_TIMEOUT))
        url = info["url"]
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.netloc != "127.0.0.1:" + str(parsed.port) or parsed.path != "/"
                or parsed.query or parsed.fragment):
            raise ValueError("invalid panel address")
        control.set_panel_base_url(ref, url, home=home)
        prepared = True
        return {"status": "prepared", "thread_id": ref.thread_id,
                "url": url + "open/" + ticket, "pid": proc.pid}
    except Exception:
        # 不回显子进程输出、票据或 capability。
        raise ValueError("panel preparation failed") from None
    finally:
        if not prepared:
            try:
                control.detach(capability, home=home)
            except (ValueError, OSError):
                pass
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        print('[context-vault] panel child cleanup unavailable', file=sys.stderr)
        # BufferedReader.close 会等待仍在 readline 中的线程锁；终止失败时不得无界等待。
        if proc is not None and proc.stdout is not None and (reader is None or not reader.is_alive()):
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass
