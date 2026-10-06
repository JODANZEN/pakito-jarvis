"""Lock-in mode: during a lock-in focus block, apps on the block list get closed when opened.

Each blocked attempt is logged as a distraction (so it counts in the trainer) and you get a
toast with how long until the break. On breaks and outside lock-in blocks, nothing is blocked.
Block list: config["focus"]["block_apps"] (process names, e.g. "Solitaire.exe").
"""
from __future__ import annotations

import csv
import io
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


class Blocker:
    def __init__(self, focus, apps: list[str], notify: Callable[[str, str], None],
                 lister=list_processes, killer=kill_process, interval: float = 3.0,
                 mute: list[str] | None = None, relaunch: Callable[[], None] | None = None):
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

    def check_once(self) -> list[str]:
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
            while not self._stop.wait(self.interval):
                try:
                    self.check_once()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
