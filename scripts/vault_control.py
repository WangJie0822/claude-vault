"""Codex 精确线程的文本控制入口。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from context_vault import control
from context_vault.session_source import resolve_current_session
from context_vault.structured_read import read_note
from context_vault.panel_launcher import prepare_panel


def launch_panel(ref, home):
    attachment = control.attach(ref, home=home)
    capability = attachment["capability"]
    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {"start_new_session": True}
    proc, opened = None, False
    try:
        proc = subprocess.Popen([sys.executable, "-m", "context_vault.panel_server"], cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **flags)
        proc.stdin.write(json.dumps({"capability": capability, "thread_id": ref.thread_id, "home": str(home)}).encode("utf-8"))
        proc.stdin.close()
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(proc.stdout.readline(4096)), daemon=True).start()
        info = json.loads(ready.get(timeout=15))
        url = info["url"]
        if not isinstance(url, str) or not url.startswith("http://127.0.0.1:"):
            raise ValueError("invalid panel address")
        opened = webbrowser.open(url + "#" + capability)
        return {"status": "opened" if opened else "browser_unavailable", "url": url,
                "thread_id": ref.thread_id, "pid": proc.pid}
    except Exception:
        raise ValueError("panel startup failed")
    finally:
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()
        if not opened:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            control.detach(capability, home=home)


def _ids(state, paths):
    result = []
    for path in paths:
        identity = next((key for key, item in (state.get("items") or {}).items() if item.get("path") == path), None)
        if identity is None:
            raise ValueError("note identity expired; refresh a real turn")
        result.append(identity)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Codex 精确线程的召回状态、面板与控制")
    parser.add_argument("--host", choices=("codex",), default="codex")
    parser.add_argument("--thread", default=os.environ.get("CODEX_THREAD_ID"))
    parser.add_argument("--remove", action="store_true", help="从排除/手动钉住集合撤销条目")
    parser.add_argument("--verdict", choices=("relevant", "irrelevant", "unsure"))
    parser.add_argument("action", choices=("status", "open", "prepare", "exclude", "pin", "unpin", "annotate", "set", "persist", "read-note"))
    parser.add_argument("items", nargs="*")
    args = parser.parse_args(argv)
    try:
        current = os.environ.get("CODEX_THREAD_ID")
        if not args.thread or current and current != args.thread:
            raise ValueError("current thread identity mismatch")
        home = Path.home()
        ref = resolve_current_session(host=args.host, context={"thread_id": args.thread,
            "codex_home": os.environ.get("CODEX_HOME", str(home / ".codex"))}, home=home)
        if ref.actor_kind != "main":
            raise ValueError("main thread identity required")
        state = control.local_state(ref, home=home)
        if args.action == "status":
            result = state
        elif args.action == "prepare" or (args.action == "open" and ref.originator == "Codex Desktop"):
            result = prepare_panel(ref, home)
        elif args.action == "open":
            result = launch_panel(ref, home)
        elif args.action == "read-note":
            if len(args.items) != 1:
                raise ValueError("read-note requires one note identity")
            result = read_note(ref, args.items[0], home=home)
        else:
            ops = {"epoch": state["epoch"], "operation_id": uuid.uuid4().hex}
            intent = state.get("intent") or {}
            if args.action in ("exclude", "pin", "unpin"):
                key = "unpin_llm" if args.action == "unpin" else args.action
                if args.remove:
                    removed = {state.get("items", {}).get(item, {}).get("path") for item in args.items}
                    ops[key] = _ids(state, [p for p in intent.get(key, []) if p not in removed])
                else:
                    ops[key] = args.items
            elif args.action == "annotate":
                if len(args.items) != 1 or args.verdict is None:
                    raise ValueError("annotate requires one identity and --verdict")
                ops["annotations"] = [{"item": args.items[0], "verdict": args.verdict,
                    "annotation_id": state.get("items", {}).get(args.items[0], {}).get("annotation_id")}]
            elif args.action == "set":
                if len(args.items) != 2:
                    raise ValueError("set requires key and value")
                key, raw = args.items
                value = raw if key == "fine_model" else json.loads(raw)
                ops["settings"] = {**intent.get("settings", {}), key: value}
            else:
                values = {**((state.get("snapshot") or {}).get("core", {}).get("settings") or {}).get("effective", {}),
                          **intent.get("settings", {})}
                if not args.items or any(key not in values for key in args.items):
                    raise ValueError("persist requires existing settings")
                # settings 是完整替换协议，persist 只限制落盘键，不能删掉其它会话覆盖。
                settings = dict(intent.get("settings", {}))
                settings.update({key: values[key] for key in args.items})
                ops.update(settings=settings, persist=args.items)
            result = control.submit_local_ops(ref, ops, home=home)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print("[context-vault] " + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    from context_vault.exe_search import harden_exe_search
    harden_exe_search()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(main())
