"""Pakito sees your tabs: a simulated extension (no browser) carries out what Pakito asks."""
import copy
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from assistant.config import DEFAULTS
from assistant.hub import Hub
from assistant.server import AGENT_HIDDEN, make_handler

TABS = [
    {"id": 11, "windowId": 1, "title": "Dashboard | Canvas", "url": "https://canvas.asu.edu/", "active": False},
    {"id": 12, "windowId": 1, "title": "Lofi Girl - YouTube", "url": "https://www.youtube.com/watch?v=1", "active": False},
    {"id": 13, "windowId": 1, "title": "Taylor Swift - YouTube", "url": "https://www.youtube.com/watch?v=2", "active": True},
    {"id": 14, "windowId": 2, "title": "Chase Online Banking", "url": "https://secure.chase.com/", "active": True},
    {"id": 15, "windowId": 1, "title": "Altermagnetism review - arXiv", "url": "https://arxiv.org/abs/2401", "active": False},
]
PAGES = {15: "Altermagnets are a third class of magnets with zero net magnetization but spin-split bands. " * 3}


class FakeExtension:
    """Does what the real background.js does, against an in-memory browser."""

    def __init__(self, hub):
        self.hub, self.tabs = hub, [dict(t) for t in TABS]
        self.focused_window, self.log, self.stop = 1, [], threading.Event()

    def snapshot(self):
        return [{**t, "focused": t["windowId"] == self.focused_window} for t in self.tabs]

    def run(self, c):
        self.log.append(c["op"])
        if c["op"] == "activate":
            for t in self.tabs:
                if t["windowId"] == next(x["windowId"] for x in self.tabs if x["id"] == c["tab"]):
                    t["active"] = t["id"] == c["tab"]
            self.focused_window = next(x["windowId"] for x in self.tabs if x["id"] == c["tab"])
            return {"ok": True}
        if c["op"] == "close":
            self.tabs = [t for t in self.tabs if t["id"] not in c["tabs"]]
            return {"ok": True}
        if c["op"] == "read":
            return {"text": PAGES.get(c["tab"], "")}
        return {"ok": True}

    def loop(self):
        results = []
        while not self.stop.is_set():
            cmds = self.hub.browser.sync(self.snapshot(), results, wait=0.2)
            results = [{"id": c["id"], **self.run(c)} for c in cmds]

    def __enter__(self):
        threading.Thread(target=self.loop, daemon=True).start()
        time.sleep(0.1)
        return self

    def __exit__(self, *a):
        self.stop.set()


@pytest.fixture
def hub(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    return Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)


def test_not_connected_says_how_to_fix(hub):
    r = hub.call("browser_tabs", {}, "voice")
    assert "isn't connected" in r["error"] and "Load unpacked" in r["error"]


def test_list_switch_close_read(hub):
    with FakeExtension(hub) as ext:
        tabs = hub.call("browser_tabs", {}, "voice")["tabs"]
        assert [t["site"] for t in tabs][:2] == ["canvas.asu.edu", "youtube.com"]
        assert [t["n"] for t in tabs if t.get("in_front")] == [3]                  # the YouTube tab he's looking at
        assert hub.call("switch_tab", {"tab": "la de Canvas"}, "voice")["switched_to"] == "Dashboard | Canvas"
        time.sleep(0.3)
        assert hub.browser.find("esta")["id"] == 11                                 # Canvas is in front now
        r = hub.call("close_tab", {"tab": "youtube", "all_matching": True}, "voice")
        assert len(r["closed"]) == 2 and all("YouTube" in x for x in r["closed"])
        time.sleep(0.3)
        assert not any("youtube" in t["url"] for t in ext.tabs)
        page = hub.call("read_tab", {"tab": "arxiv"}, "voice")
        assert page["text"].startswith("Altermagnets") and page["title"].startswith("Altermagnetism")


def test_never_reads_banking_and_logs_no_page_text(hub):
    with FakeExtension(hub) as ext:
        r = hub.call("read_tab", {"tab": "chase"}, "voice")
        assert "doesn't read" in r["error"] and "read" not in ext.log
        hub.call("read_tab", {"tab": "arxiv"}, "voice")
    logged = json.dumps(hub.events.read())
    assert "spin-split" not in logged and "Lofi Girl" not in logged          # page text and tab titles stay out


def test_agents_cant_see_his_browser():
    assert {"browser_tabs", "read_tab", "switch_tab", "close_tab"} <= AGENT_HIDDEN


def test_find_by_number_words_and_front(hub):
    hub.browser.sync([{**t, "focused": t["windowId"] == 1} for t in TABS], [], wait=0)
    assert hub.browser.find("2")["id"] == 12 and hub.browser.find(None)["id"] == 13
    assert hub.browser.find("taylor swift")["id"] == 13 and hub.browser.find("netflix") is None
    assert hub.browser.find("9") is None


def test_only_the_extension_can_sync(hub):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(hub, token="t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/ext/sync"
    body = json.dumps({"tabs": TABS, "results": [], "wait": 0}).encode()

    def post(headers):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    try:
        assert post({})[0] == 403                                                     # a local program, no origin
        assert post({"Origin": "https://evil.example", "X-Pakito-Ext": "1"})[0] == 403  # a web page
        assert post({"Origin": "chrome-extension://abc"})[0] == 403                   # missing the header
        code, out = post({"Origin": "chrome-extension://abc", "X-Pakito-Ext": "1"})
        assert code == 200 and out == {"cmds": []} and hub.browser.connected()
    finally:
        srv.shutdown()
