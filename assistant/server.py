"""Tiny local status server (127.0.0.1 only) so the browser extension knows when you're locked in.

GET  /state    -> {"locked": bool, "remaining_min": n, "label": str, "sites": [...]}
POST /blocked  {"site": "youtube.com"} -> logged as a distraction (same site within 20 s counts once)
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765


def make_handler(hub, clock=time.time):
    last_hit: dict[str, float] = {}

    def state() -> dict:
        st = hub.focus.status()
        return {"locked": hub.focus.locked, "remaining_min": st.get("remaining_min", 0),
                "label": st.get("label"), "sites": hub.cfg["focus"].get("block_sites", [])}

    def blocked(site: str) -> dict:
        if not hub.focus.locked:
            return {"ok": False}
        now = clock()
        if now - last_hit.get(site, 0) < 20:          # the extension can report one visit twice
            return {"ok": True, "counted": False}
        last_hit[site] = now
        hub.focus.distracted(f"opened {site} (blocked)")
        left = hub.focus.status().get("remaining_min", 0)
        hub.notify("Locked in 🔒", f"{site} is blocked. Break in {max(1, round(left))} min.")
        return {"ok": True, "counted": True}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, obj: dict, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(state() if self.path.startswith("/state") else {"error": "not found"},
                       200 if self.path.startswith("/state") else 404)

        def do_POST(self):
            if not self.path.startswith("/blocked"):
                return self._send({"error": "not found"}, 404)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                site = str(json.loads(self.rfile.read(n) or b"{}").get("site", ""))[:100]
            except json.JSONDecodeError:
                site = ""
            self._send(blocked(site) if site else {"error": "no site"})

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def log_message(self, *a):
            pass                                      # keep the console clean

    return Handler


def start(hub, port: int = PORT) -> ThreadingHTTPServer | None:
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(hub))
    except OSError:
        print(f"(lock-in site blocking off: port {port} is busy — is another Pakito running?)")
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
