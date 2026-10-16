"""Tiny local status server (127.0.0.1 only) so the browser extension knows when you're locked in.

GET  /state    -> {"locked": bool, "remaining_min": n, "label": str, "sites": [...]}
POST /blocked  {"site": "youtube.com"} -> logged as a distraction (same site within 20 s counts once)

Hub API: lets other brains (Claude Code via the MCP relay, later Hermes) use Pakito's tools
on the ONE running Pakito, so there's a single focus timer, task list and log.
GET  /tools    -> [{name, description, parameters}]
POST /call     {"tool": "add_task", "args": {...}, "source": "mcp"} -> the tool's result
Both need the header X-Pakito-Token (from assistant/.pakito_token) and refuse browser requests:
a web page can't send that header without a CORS preflight, which this server never approves.
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
TOKEN_FILE = Path(__file__).with_name(".pakito_token")     # gitignored, never leaves the PC
# Tools other brains don't get: no agent spawning agents (a build job starting more builds).
AGENT_HIDDEN = {"ask_claude", "delegate_build", "continue_job", "open_claude_code", "free_mode",
                # and no agent approves, discards or pushes its own work: those are Jose's calls
                "approve_build", "discard_build", "push_build",
                "look_at_screen", "type_text"}  # and no peeking at his screen or typing into his apps from outside


def get_token(path: Path = TOKEN_FILE) -> str:
    path = Path(path)
    if path.exists():
        tok = path.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    path.write_text(tok, encoding="utf-8")
    return tok


def make_handler(hub, clock=time.time, token: str | None = None):
    last_hit: dict[str, float] = {}
    token = token or get_token()

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

        def _authorized(self) -> bool:
            if self.headers.get("Origin"):             # a web page, not a local program
                return False
            return hmac.compare_digest(self.headers.get("X-Pakito-Token", ""), token)

        def _api(self, obj: dict, code: int = 200) -> None:     # like _send, but no CORS header
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/pet"):          # what the pet window draws (has transcripts: token only)
                if not self._authorized():
                    return self._api({"error": "forbidden"}, 403)
                return self._api(hub.pet_snapshot())
            if self.path.startswith("/tools"):
                if not self._authorized():
                    return self._api({"error": "forbidden"}, 403)
                return self._api({"tools": [{"name": t.name, "description": t.description,
                                             "parameters": t.parameters} for t in hub.tools.values()
                                            if t.name not in AGENT_HIDDEN]})
            if self.path.startswith("/state"):
                try:
                    hub.blocker.ext_seen = clock()           # the browser extension is alive
                except AttributeError:
                    pass
            self._send(state() if self.path.startswith("/state") else {"error": "not found"},
                       200 if self.path.startswith("/state") else 404)

        def do_POST(self):
            if self.path.startswith("/call"):
                if not self._authorized():
                    return self._api({"error": "forbidden"}, 403)
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    req = json.loads(self.rfile.read(n) or b"{}")
                    name, args = str(req["tool"]), dict(req.get("args") or {})
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    return self._api({"error": "send {\"tool\": name, \"args\": {...}}"}, 400)
                source = str(req.get("source") or "mcp")[:20]
                if name in AGENT_HIDDEN:
                    return self._api({"result": {"error": f"{name} is voice-only."}})
                return self._api({"result": hub.call(name, args, source=source)})
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


def start(hub, port: int = PORT, token: str | None = None) -> ThreadingHTTPServer | None:
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(hub, token=token))
    except OSError:
        print(f"(lock-in site blocking off: port {port} is busy — is another Pakito running?)")
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
