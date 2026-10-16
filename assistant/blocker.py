"""Lock-in mode: during a lock-in focus block, apps on the block list get closed when opened.

Each blocked attempt is logged as a distraction (so it counts in the trainer) and you get a
toast with how long until the break. On breaks and outside lock-in blocks, nothing is blocked.
Block list: config["focus"]["block_apps"] (process names, e.g. "Solitaire.exe").

Websites: the Opera GX extension (browser-extension/) is the main blocker. As a backup that works without it,
during lock-in Pakito also looks at the window in front: if it's a browser showing a blocked site (by its title,
e.g. "Instagram", "r/... - Reddit", "Home / X") it closes that tab (Ctrl+W). Only exact site-name segments
of the title count, so a Canvas page that merely mentions Instagram isn't touched.
"""
from __future__ import annotations

import csv
import io
import re
import subprocess
import sys
import threading
import time
from typing import Callable


def list_processes() -> set[str]:
    if sys.platform != "win32":
        return set()
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         creationflags=0x08000000).stdout
    return {row[0].lower() for row in csv.reader(io.StringIO(out)) if row}


def kill_process(name: str) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True, creationflags=0x08000000)


BROWSERS = {"opera.exe", "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "vivaldi.exe", "arc.exe"}
SUFFIX = re.compile(r"\s+[-–—]\s+(opera( gx)?|google chrome|microsoft\u200b? edge|mozilla firefox|brave|vivaldi)$", re.I)
SPLIT = re.compile(r"\s+(?:[-–—|•/:]|on)\s+")


def site_brand(domain: str) -> str:
    return domain.lower().removeprefix("www.").split(".")[0]


def title_site(title: str, sites: list[str]) -> str | None:
    """Which blocked site a browser window title belongs to, if any (exact name segments only)."""
    t = SUFFIX.sub("", title.strip())
    t = re.sub(r"^\(\d+\+?\)\s*", "", t)                       # "(3) Facebook" -> "Facebook"
    parts = [p.strip().lower() for p in SPLIT.split(t) if p.strip()]
    if not parts:
        return None
    for site in sites:
        b = site_brand(site)
        names = {b, "twitter"} if b in ("x", "twitter") else {b}
        if parts[0] in names or parts[-1] in names:
            return site
        if b == "instagram" and parts[-1].endswith("instagram photos and videos"):
            return site
    return None


def foreground_window() -> tuple[str, str, int]:
    """-> (title, process name, hwnd) of the window in front."""
    if sys.platform != "win32":
        return "", "", 0
    from .screen import foreground
    import ctypes
    title, proc, _ = foreground()
    return title, proc, ctypes.windll.user32.GetForegroundWindow()


def close_tab(hwnd: int) -> None:
    """Ctrl+W to the browser in front, only if it's still the same window."""
    if sys.platform != "win32":
        return
    import ctypes
    u = ctypes.windll.user32
    if u.GetForegroundWindow() != hwnd:
        return
    VK_CONTROL, VK_W, UP = 0x11, 0x57, 0x0002
    u.keybd_event(VK_CONTROL, 0, 0, 0)
    u.keybd_event(VK_W, 0, 0, 0)
    u.keybd_event(VK_W, 0, UP, 0)
    u.keybd_event(VK_CONTROL, 0, UP, 0)


class Blocker:
    def __init__(self, focus, apps: list[str], notify: Callable[[str, str], None],
                 lister=list_processes, killer=kill_process, interval: float = 3.0,
                 mute: list[str] | None = None, relaunch: Callable[[], None] | None = None,
                 sites: list[str] | None = None, fg=foreground_window, closer=close_tab, clock=time.time):
        self.focus = focus
        self.apps = [a.lower() for a in apps]
        self.pretty = {a.lower(): a.removesuffix(".exe").removesuffix(".EXE") for a in apps}
        self.notify = notify
        self.lister, self.killer, self.interval = lister, killer, interval
        self._stop = threading.Event()
        self.blocked_count = 0
        # mute = quietly closed during lock-in (e.g. Phone Link, so phone notifications stop), NOT a distraction
        self.mute = [a.lower() for a in (mute or [])]
        self.relaunch = relaunch
        self.muted_now = False
        self.sites = list(sites or [])
        self.fg, self.closer, self.clock = fg, closer, clock
        self.ext_seen = 0.0                      # last time the browser extension asked /state
        self._last_site_hit: dict[str, float] = {}
        focus.listeners.append(self.on_phase)

    def on_phase(self, phase: str) -> None:
        if phase == "focus" and self.focus.locked:
            self.check_once()
        elif phase != "focus" and self.muted_now:          # break or done: bring notifications back
            self.muted_now = False
            if self.relaunch:
                try:
                    self.relaunch()
                except Exception:
                    pass

    @property
    def extension_connected(self) -> bool:
        return self.clock() - self.ext_seen < 90       # it checks in at least every 30 s while the browser is open

    def check_sites(self) -> str | None:
        """Backup site blocker: close a blocked site's tab if it's the window in front."""
        if not self.sites or not self.focus.locked:
            return None
        try:
            title, proc, hwnd = self.fg()
        except Exception:
            return None
        if proc.lower() not in BROWSERS:
            return None
        site = title_site(title, self.sites)
        if not site:
            return None
        self.closer(hwnd)
        now = self.clock()
        if now - self._last_site_hit.get(site, 0) > 20:   # the extension may report the same visit: count once
            self._last_site_hit[site] = now
            self.blocked_count += 1
            self.focus.distracted(f"opened {site} (blocked)")
            left = self.focus.status().get("remaining_min", 0)
            self.notify("Locked in 🔒", f"{site} is blocked. Break in {max(1, round(left))} min.")
        return site

    def check_once(self) -> list[str]:
        if self.focus.locked:
            self.check_sites()
        if not (self.apps or self.mute) or not self.focus.locked:
            return []
        running = self.lister()
        for app in self.mute:
            if app in running:
                self.killer(app)
                self.muted_now = True
        hits = [a for a in self.apps if a in running]
        for app in hits:
            self.killer(app)
            self.blocked_count += 1
            self.focus.distracted(f"opened {app} (blocked)")
            left = self.focus.status().get("remaining_min", 0)
            self.notify("Locked in 🔒", f"{self.pretty[app]} is blocked. Break in {max(1, round(left))} min.")
        return hits

    def start(self) -> None:
        def loop():
            n = 0
            while not self._stop.wait(1.0):              # sites every second, apps every `interval`
                n += 1
                try:
                    if self.focus.locked:
                        self.check_sites()
                    if n % max(1, int(self.interval)) == 0:
                        self.check_once()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
