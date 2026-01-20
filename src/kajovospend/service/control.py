from __future__ import annotations

import json
import socketserver
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Any


@dataclass
class ControlContext:
    get_status: Callable[[], Dict[str, Any]]
    request_stop: Callable[[], None]


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        data = b""
        while True:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        line = data.split(b"\n", 1)[0].decode("utf-8", errors="ignore").strip()
        try:
            req = json.loads(line) if line else {}
        except Exception:
            req = {}
        cmd = str(req.get("cmd") or "status")
        ctx: ControlContext = self.server.ctx  # type: ignore[attr-defined]
        if cmd == "stop":
            ctx.request_stop()
            resp = {"ok": True}
        elif cmd == "ping":
            resp = {"ok": True, "pong": True}
        else:
            resp = ctx.get_status()
        self.request.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))


class ControlServer:
    def __init__(self, host: str, port: int, ctx: ControlContext):
        self._server = socketserver.ThreadingTCPServer((host, port), _Handler)
        self._server.daemon_threads = True
        self._server.ctx = ctx  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self):
        self._server.shutdown()
        self._server.server_close()
