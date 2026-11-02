"""按需启动的回环面板；所有控制均复用 control 协议。"""
from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit

from . import control

ASSETS = Path(__file__).resolve().parent / "panel"
MAX_BODY = 100000
IDLE_SECONDS = 300
LAUNCH_TICKET_TTL = 300


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # URL、凭证及笔记内容不进访问日志。

    def _reply(self, status, data, mime="application/json; charset=utf-8", *, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if isinstance(data, dict) else data
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, *, authenticated):
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        if self.headers.get("Host") != expected:
            self._reply(403, {"error": "invalid_host"}); return False
        origin = self.headers.get("Origin")
        if (origin is not None and origin != "http://" + expected) or (self.command == "POST" and origin is None):
            self._reply(403, {"error": "invalid_origin"}); return False
        if authenticated:
            supplied = self.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied.encode("utf-8"), ("Bearer " + self.server.capability).encode("utf-8")):
                self._reply(401, {"error": "unauthorized"}); return False
            if self.headers.get("X-Context-Vault-Thread") != self.server.thread_id:
                self._reply(403, {"error": "thread_mismatch"}); return False
        return True

    def do_GET(self):
        url = urlsplit(self.path)
        if url.query:
            self._reply(400, {"error": "query_not_supported"}); return
        api = url.path.startswith("/api/")
        if not self._guard(authenticated=api):
            return
        if url.path.startswith("/open/"):
            supplied = url.path[len("/open/"):]
            with self.server.ticket_lock:
                valid = (self.server.ticket_digest is not None and not self.server.ticket_used
                    and time.monotonic() < self.server.ticket_deadline
                    and secrets.compare_digest(control._digest(supplied), self.server.ticket_digest))
                if not valid:
                    self._reply(410, {"error": "launch_ticket_unavailable"})
                    return
                # 先检查授权，再消费票据，不能兑换已失效的 capability。
                try:
                    control.get_state(self.server.capability, home=self.server.home)
                except (ValueError, OSError):
                    self._reply(410, {"error": "launch_ticket_unavailable"})
                    return
                self.server.ticket_used = True
            self.server.last_activity = time.monotonic()
            self._reply(303, b"", headers={"Location": "/#" + self.server.capability})
            return
        if url.path == "/api/state":
            try:
                data = control.get_state(self.server.capability, home=self.server.home)
                if data["thread_id"] != self.server.thread_id:
                    raise ValueError("thread mismatch")
                self.server.last_activity = time.monotonic()
                self._reply(200, data)
            except (ValueError, OSError):
                self._reply(401, {"error": "expired_or_unavailable"})
            return
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/panel.js": ("panel.js", "text/javascript; charset=utf-8"),
                  "/panel.css": ("panel.css", "text/css; charset=utf-8")}
        if url.path not in assets:
            self._reply(404, {"error": "not_found"}); return
        filename, mime = assets[url.path]
        data = (ASSETS / filename).read_bytes()
        if filename == "index.html":
            data = data.replace(b"__THREAD_ID__", html.escape(self.server.thread_id, quote=True).encode("utf-8"))
        self._reply(200, data, mime)

    def do_POST(self):
        if not self._guard(authenticated=True):
            return
        if self.path not in ("/api/ops", "/api/detach", "/api/close"):
            self._reply(404, {"error": "not_found"}); return
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            self._reply(411, {"error": "length_required"}); return
        self.connection.settimeout(3)
        if len(length) > 9 or int(length) > MAX_BODY:
            # 有界丢弃已发来的小量超限数据，避免 Windows 因未读数据 RST 吞掉 413 响应。
            self.connection.settimeout(.5)
            try:
                self.rfile.read(min(int(length[:9]), MAX_BODY + 1))
            except OSError:
                pass
            self._reply(413, {"error": "body_too_large"}); return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self._reply(415, {"error": "json_required"}); return
        try:
            body = json.loads(self.rfile.read(int(length)))
            if self.path != "/api/ops":
                if body != {}:
                    raise ValueError("unexpected close body")
                control.detach(self.server.capability, home=self.server.home)
                self._reply(200, {"status": "detached"})
                if self.path == "/api/close":
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            result = control.submit_ops(self.server.capability, body, home=self.server.home)
            self.server.last_activity = time.monotonic()
            self._reply(200, result)
        except (ValueError, OSError, TypeError) as exc:
            # 协议错误仅包含固定原因；不回显 body、凭证或笔记正文。
            self._reply(400, {"error": "operation_rejected", "detail": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})


def create_server(*, capability, thread_id, home, launch_ticket=None):
    state = control.get_state(capability, home=Path(home))
    if state["thread_id"] != thread_id:
        raise ValueError("thread mismatch")
    server = PanelServer(("127.0.0.1", 0), Handler)
    server.capability, server.thread_id, server.home = capability, thread_id, Path(home)
    server.last_activity = time.monotonic()
    server.ticket_digest = control._digest(launch_ticket) if launch_ticket else None
    server.ticket_deadline = time.monotonic() + LAUNCH_TICKET_TTL
    server.ticket_used = False
    server.ticket_lock = threading.Lock()
    return server


def main():
    # 凭证只经匿名 stdin 到达，不放 argv、stdout 或页面 HTML。
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        return 2
    data = json.loads(raw)
    if Path(data["home"]).resolve() != Path.home().resolve():
        return 2
    server = create_server(capability=data["capability"], thread_id=data["thread_id"], home=Path.home(),
                           launch_ticket=data.get("launch_ticket"))
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}/"}), flush=True)
    def expire():
        while time.monotonic() - server.last_activity < IDLE_SECONDS:
            time.sleep(1)
        server.shutdown()
    threading.Thread(target=expire, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        try:
            control.detach(server.capability, home=server.home)
        except (ValueError, OSError):
            pass
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError):
        raise SystemExit(2)
