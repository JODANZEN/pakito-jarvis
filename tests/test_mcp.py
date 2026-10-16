import json
import urllib.error
import urllib.request

import pytest

from assistant import mcp_server, server
from test_assistant import env  # noqa: F401  (shared fixture)

TOKEN = "test-token-123"


@pytest.fixture
def live(env):
    hub = env[0]
    srv = server.start(hub, port=0, token=TOKEN)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield hub, base
    srv.shutdown()


def req(base, method, path, body=None, headers=None):
    r = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                               headers=headers or {})
    with urllib.request.urlopen(r, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_api_needs_token_and_refuses_browsers(live):
    hub, base = live
    for headers in ({}, {"X-Pakito-Token": "wrong"},
                    {"X-Pakito-Token": TOKEN, "Origin": "https://evil.example"}):
        with pytest.raises(urllib.error.HTTPError) as e:
            req(base, "POST", "/call", {"tool": "add_task", "args": {"task": "x"}}, headers)
        assert e.value.code == 403
    assert "Tasks" not in [p.name for p in hub.dir.iterdir()] or "x" not in (hub.dir / "Tasks.md").read_text()
    assert req(base, "GET", "/state")[0] == 200              # the extension's endpoint still works without a token


def test_api_runs_tools_on_the_one_hub(live):
    hub, base = live
    h = {"X-Pakito-Token": TOKEN}
    _, tools = req(base, "GET", "/tools", headers=h)
    names = {t["name"] for t in tools["tools"]}
    assert {"add_task", "start_focus", "focus_status"} <= names
    assert not names & server.AGENT_HIDDEN                    # no agent spawning agents
    _, out = req(base, "POST", "/call", {"tool": "add_task", "args": {"task": "Read Taylor ch 7"}}, h)
    assert "Taylor" in (hub.dir / "Tasks.md").read_text(encoding="utf-8")
    ev = hub.events.read()[-1]
    assert ev["tool"] == "add_task" and ev["source"] == "mcp"   # logged like any other brain's action
    _, out = req(base, "POST", "/call", {"tool": "ask_claude", "args": {"question": "x"}}, h)
    assert "voice-only" in out["result"]["error"]


def test_mcp_relay_protocol(live, monkeypatch):
    hub, base = live
    monkeypatch.setattr(mcp_server, "BASE", base)
    monkeypatch.setattr(mcp_server, "_token", lambda: TOKEN)
    relay = mcp_server.Relay()
    init = relay.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
    assert relay.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    listed = relay.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert any(t["name"] == "focus_status" and "inputSchema" in t for t in listed)
    out = relay.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "start_focus", "arguments": {"minutes": 25, "label": "PHY 310"}}})
    assert out["result"]["isError"] is False and hub.focus.status()["phase"] == "focus"
    bad = relay.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert bad["error"]["code"] == -32601


def test_mcp_relay_when_pakito_is_off(monkeypatch):
    monkeypatch.setattr(mcp_server, "BASE", "http://127.0.0.1:9")   # nothing listens there
    relay = mcp_server.Relay()
    tools = relay.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["pakito_status"]
    out = relay.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "add_task", "arguments": {"task": "x"}}})
    assert out["result"]["isError"] and "isn't running" in out["result"]["content"][0]["text"]


def test_mcp_stdio_loop(monkeypatch):
    import io
    monkeypatch.setattr(mcp_server, "BASE", "http://127.0.0.1:9")
    stdin = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n\nnot json\n{"jsonrpc":"2.0","method":"x"}\n')
    stdout = io.BytesIO()
    mcp_server.serve(stdin, stdout)
    lines = stdout.getvalue().decode().splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == {"jsonrpc": "2.0", "id": 1, "result": {}}
