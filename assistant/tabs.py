"""Pakito sees your browser tabs (through the Pakito browser extension, the same one lock-in uses).

The extension calls POST /ext/sync on Pakito's local server about once a second: it sends the open tabs (title,
address, which one is in front) and the results of what it was asked to do. The server answers with what to do
next: switch to a tab, close it, open a link, or read a page's text. The call is held open for a few seconds
until there's something to do, so an order arrives at once.

Only the extension can talk to this part (its own origin + a header a web page can't send). Pages that look like
banking or password managers are never read.
"""
from __future__ import annotations

import itertools
import re
import threading
import time
from typing import Callable
from urllib.parse import urlparse

READ_LIMIT = 9000              # characters of page text handed to Gemini
FRESH_SEC = 8                  # the extension checked in this recently = connected

SENSITIVE_SITES = ("bank", "banco", "chase.com", "wellsfargo", "bankofamerica", "paypal", "venmo", "1password",
                   "bitwarden", "lastpass", "dashlane", "accounts.google.com", "login.microsoftonline",
                   "weblogin.asu.edu", "duosecurity", "myaccount.google")


def site_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        host = ""
    return host.removeprefix("www.")


def is_sensitive(tab: dict) -> bool:
    from .screen import is_sensitive as sensitive_title
    t = f"{tab.get('url', '')} {tab.get('title', '')}".lower()
    return any(s in t for s in SENSITIVE_SITES) or sensitive_title(tab.get("title", ""), "")


class BrowserTabs:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.tabs: list[dict] = []
        self.seen = 0.0
        self._cond = threading.Condition()
        self._queue: list[dict] = []
        self._results: dict[int, dict] = {}
        self._ids = itertools.count(1)

    # ---------- the extension's side ----------
    def sync(self, tabs: list[dict], results: list[dict], wait: float = 8.0) -> list[dict]:
        """Called by the server for each POST /ext/sync. Returns the commands to run (waits up to `wait` s)."""
        clean = []
        for t in tabs[:200]:
            if isinstance(t, dict) and isinstance(t.get("id"), int):
                clean.append({"id": t["id"], "window": t.get("windowId"), "title": str(t.get("title", ""))[:200],
                              "url": str(t.get("url", ""))[:500], "active": bool(t.get("active")),
                              "focused": bool(t.get("focused"))})
        with self._cond:
            self.tabs, self.seen = clean, self.clock()
            for r in results[:50]:
                if isinstance(r, dict) and isinstance(r.get("id"), int):
                    self._results[r["id"]] = r
            self._cond.notify_all()
            end = time.monotonic() + wait
            while not self._queue and time.monotonic() < end:
                self._cond.wait(timeout=max(0.0, end - time.monotonic()))
            cmds, self._queue = self._queue, []
        return cmds

    # ---------- Pakito's side ----------
    def connected(self) -> bool:
        return self.clock() - self.seen < FRESH_SEC

    def ask(self, op: str, tab: int | None = None, timeout: float = 6.0, **extra) -> dict:
        """Send one order to the extension and wait for its answer."""
        if not self.connected():
            return {"error": "The browser extension isn't connected (load browser-extension in your browser's "
                             "extensions page, Developer mode, Load unpacked; or reload it after an update)."}
        cid = next(self._ids)
        with self._cond:
            self._queue.append({"id": cid, "op": op, "tab": tab, **extra})
            self._cond.notify_all()
            end = time.monotonic() + timeout
            while cid not in self._results and time.monotonic() < end:
                self._cond.wait(timeout=max(0.0, end - time.monotonic()))
            res = self._results.pop(cid, None)
        if res is None:
            return {"error": "The browser didn't answer in time."}
        return res

    def listing(self) -> list[dict]:
        """Numbered like he'd say them: 1, 2, 3… in window/tab order; the tab in front is marked."""
        return [{"n": i + 1, "title": t["title"][:90], "site": site_of(t["url"]),
                 **({"in_front": True} if t["active"] and t["focused"] else {})}
                for i, t in enumerate(self.tabs)]

    def find_all(self, ref: str) -> list[dict]:
        """Every tab matching some words ('cierra todas las de YouTube')."""
        words = [w for w in re.findall(r"\w+", str(ref).lower()) if len(w) > 2 and w not in ("tab", "tabs", "pestaña",
                                                                                         "pestañas", "las", "los",
                                                                                         "todas", "all", "del")]
        if not words:
            return []
        return [t for t in self.tabs
                if sum(w in f"{t['title']} {site_of(t['url'])}".lower() for w in words) / len(words) >= 0.5]

    def find(self, ref: str | float | None) -> dict | None:
        """'3' / 'esta' (the one in front) / words from the title or site ('la de Canvas', 'youtube')."""
        if not self.tabs:
            return None
        front = next((t for t in self.tabs if t["active"] and t["focused"]), None) or \
            next((t for t in self.tabs if t["active"]), None)
        if ref is None or str(ref).strip().lower() in ("", "esta", "this", "this one", "la actual", "current",
                                                      "esta pestaña", "la que tengo abierta", "front"):
            return front
        s = str(ref).strip().lower()
        if re.fullmatch(r"\d+(\.0)?", s):
            i = int(float(s)) - 1
            return self.tabs[i] if 0 <= i < len(self.tabs) else None
        words = [w for w in re.findall(r"\w+", s) if len(w) > 2 and w not in ("tab", "pestaña", "the", "las", "los",
                                                                               "del", "que", "una", "one")]
        if not words:
            return None
        best, score = None, 0.0
        for t in self.tabs:
            hay = f"{t['title']} {site_of(t['url'])}".lower()
            sc = sum(w in hay for w in words) / len(words)
            if sc > score or (sc == score and best is not None and t["active"] and not best["active"]):
                best, score = t, sc
        return best if score >= 0.5 else None
