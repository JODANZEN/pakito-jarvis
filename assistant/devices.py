"""Several computers, one Pakito memory (e.g. the PC and a Mac sharing the Second Brain over OneDrive).

Each running Pakito writes a small heartbeat to state/devices/<name>.json every 30 s: when it's alive and when
you last used it. The one you used most recently is "in charge": it fires reminders. The other keeps running
quietly; talk to it and it takes over. A machine that sleeps or closes stops beating, and after STALE_SEC
the other one takes over on its own. One computer alone is always in charge.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

STALE_SEC = 150          # no heartbeat for this long = that computer is asleep/off (OneDrive needs a moment too)
BEAT_SEC = 30


class Presence:
    def __init__(self, folder: Path, name: str, clock: Callable[[], float] = time.time):
        self.dir, self.name, self.clock = Path(folder), name, clock
        self.used = 0.0
        self._last_write = 0.0
        self._lock = threading.Lock()
        try:                                     # a restart doesn't forget when you last used this computer
            self.used = float(json.loads(self.path.read_text(encoding="utf-8")).get("used", 0))
        except (OSError, ValueError, AttributeError):
            pass

    @property
    def path(self) -> Path:
        return self.dir / f"{self.name}.json"

    def beat(self, used: bool = False) -> None:
        now = self.clock()
        with self._lock:
            if used:
                self.used = now
            elif now - self._last_write < 5:
                return
            self._last_write = now
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"name": self.name, "alive": now, "used": self.used}), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass

    def others(self) -> list[dict]:
        out = []
        for f in self.dir.glob("*.json") if self.dir.exists() else []:
            if f.stem == self.name:
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and self.clock() - float(d.get("alive", 0)) < STALE_SEC:
                out.append(d)
        return out

    def in_charge(self) -> bool:
        """True unless another computer is awake and was used more recently than this one."""
        return not any(float(d.get("used", 0)) > self.used for d in self.others())

    def who(self) -> str:
        if self.in_charge():
            return self.name
        return max(self.others(), key=lambda d: float(d.get("used", 0))).get("name", "?")

    def start(self) -> None:
        def loop():
            while True:
                self.beat()
                time.sleep(BEAT_SEC)
        threading.Thread(target=loop, daemon=True, name="pakito-presence").start()
