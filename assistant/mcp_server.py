"""Pakito as an MCP server: lets Claude Code / the Claude app (and later Hermes) use Pakito's tools.

It does NOT start a second Pakito. It's a thin relay to the Pakito that's already running
(run-pakito.bat), through the token-protected API on 127.0.0.1:8765. So there's still one focus
timer, one task list, one log, whichever brain is asking.

Register once (any terminal):
    run-pakito.bat mcp          -> adds it to Claude Code for all your projects
Then in Claude Code: "use pakito to add a task…", "what's my focus status?" (tools show up as mcp__pakito__*).

Standalone on purpose (stdlib only, run by file path), so any MCP client can launch it from anywhere.
Speaks MCP over stdio: newline-delimited JSON-RPC 2.0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".pakito_token"
BASE = os.environ.get("PAKITO_URL", "http://127.0.0.1:8765")
NOT_RUNNING = ("Pakito isn't running, so its tools aren't available. Start it with run-pakito.bat "
               "(or the Pakito shortcut), then try again.")


def _token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def api(method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Pakito-Token": _token(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


class Relay:
    """The MCP logic, separate from stdio so it's testable. api(method, path, body) -> dict."""

    def __init__(self, api_fn=api):
        self.api = api_fn

    def tools(self) -> list[dict]:
        try:
            listed = self.api("GET", "/tools").get("tools", [])
        except (urllib.error.URLError, OSError, ValueError):
            return [{"name": "pakito_status", "description": "Check whether Pakito is running.",
                     "inputSchema": {"type": "object", "properties": {}}}]
        return [{"name": t["name"], "description": t["description"],
                 "inputSchema": t.get("parameters") or {"type": "object", "properties": {}}} for t in listed]

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """-> (text, is_error)"""
        try:
            if name == "pakito_status":
                self.api("GET", "/tools")
                return "Pakito is running.", False
            res = self.api("POST", "/call", {"tool": name, "args": args or {}, "source": "mcp"}).get("result")
        except urllib.error.HTTPError as e:
            return (f"Pakito refused ({e.code}). The token in assistant/.pakito_token may not match the "
                    "running Pakito; restart Pakito." if e.code == 403 else f"Pakito error {e.code}"), True
        except (urllib.error.URLError, OSError, ValueError):
            return NOT_RUNNING, True
        err = isinstance(res, dict) and "error" in res
        return json.dumps(res, ensure_ascii=False, default=str), err

    def handle(self, msg: dict) -> dict | None:
        """One JSON-RPC message in -> response (or None for notifications)."""
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:                                        # notification (e.g. notifications/initialized)
            return None
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "pakito", "version": "0.1"},
                      "instructions": "Pakito is the user's PC assistant. These tools act on their real computer and "
                                      "their real focus timer / task list, shared with their voice assistant."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.tools()}
        elif method == "tools/call":
            text, is_err = self.call(params.get("name", ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": is_err}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    relay = Relay()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        out = relay.handle(msg)
        if out is not None:
            stdout.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()


def register() -> None:
    """run-pakito.bat mcp: add this server to Claude Code (user scope = every project)."""
    sys.path.insert(0, str(HERE.parent))
    from assistant.claude_bridge import find_claude
    exe = find_claude()
    if not exe:
        print("Claude Code not found. Install it first (see README → Ask Claude).")
        return
    cmd = [exe, "mcp", "add", "--transport", "stdio", "--scope", "user", "pakito", "--",
           sys.executable, str(Path(__file__).resolve())]
    print("Running:", " ".join(cmd))
    subprocess.run([exe, "mcp", "remove", "--scope", "user", "pakito"], capture_output=True)
    r = subprocess.run(cmd)
    if r.returncode == 0:
        print("\n✅ Added. In any Claude Code session, Pakito's tools show up as mcp__pakito__*.\n"
              "   Pakito has to be running for them to work.\n\n"
              "For the Claude desktop app, add this under \"mcpServers\" in its config "
              "(Settings → Developer → Edit config):\n" +
              json.dumps({"pakito": {"command": sys.executable, "args": [str(Path(__file__).resolve())]}}, indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "register":
        register()
    else:
        serve()
