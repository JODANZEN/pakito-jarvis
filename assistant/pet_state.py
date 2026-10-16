"""What Pakibot (the on-screen pet) should show. Pakito updates this; the pet window reads it from /pet.

Modes ("modo pop-in" / "quédate" / "desaparece"):
  popin  — shows up while you talk to Pakito, then fades out after a few quiet seconds.
           Also pops in for news: a block finished, a Claude job done, and stays during lock-in.
  stay   — always on screen. Sleeps when idle, shows the time now and then, timer during focus.
  off    — hidden until you call it back.
Always hidden during calls, so it never shows up in a screen share.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

MODES = ("popin", "stay", "off")
ALIASES = {"pop in": "popin", "pop-in": "popin", "popin": "popin", "aparece": "popin", "aparecer": "popin",
           "stay": "stay", "quedate": "stay", "quédate": "stay", "companion": "stay", "compañero": "stay",
           "always": "stay", "off": "off", "hide": "off", "desaparece": "off", "esconder": "off",
           "escóndete": "off", "hidden": "off"}


class PetState:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time, cfg: dict | None = None):
        self.path = Path(path)
        self.clock = clock
        self.cfg = {"mode": "popin", "linger_sec": 10, "daily_goal_min": 120, **(cfg or {})}
        self._lock = threading.Lock()
        self.mode = self.cfg["mode"]
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8")).get("mode")
            if saved in MODES:
                self.mode = saved
        except (OSError, ValueError):
            pass
        self.conv = False
        self.activity = "idle"           # idle | listen | think | talk  (inside a conversation)
        self.level = 0.0                 # mic level while listening, voice level while talking (0..1)
        self.who, self.text, self.note = "", "", ""
        self.on_call = False
        self.event, self.event_until = "", 0.0     # e.g. "happy" after a finished block
        self.last_active = 0.0
        self.hidden_until = 0.0          # out of the way for a screenshot

    # ---- set by Pakito ----
    def set_mode(self, mode: str) -> str | None:
        m = ALIASES.get(mode.lower().strip(), mode.lower().strip())
        if m not in MODES:
            return None
        with self._lock:
            self.mode = m
            self.last_active = self.clock()          # so pop-in shows itself once to confirm
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"mode": m}), encoding="utf-8")
        except OSError:
            pass
        return m

    def conversation(self, open_: bool) -> None:
        with self._lock:
            self.conv = open_
            self.activity = "idle"
            self.level = 0.0
            self.last_active = self.clock()
            if open_:
                self.who = self.text = self.note = ""

    def set(self, activity: str | None = None, level: float | None = None, note: str | None = None) -> None:
        with self._lock:
            if activity:
                if activity == "listen" and self.activity != "listen":
                    self.who, self.text = "you", ""          # new turn: clear the bubble
                self.activity = activity
            if level is not None:
                self.level = max(0.0, min(1.0, level))
            if note is not None:
                self.note = note
            self.last_active = self.clock()

    def say(self, who: str, text: str) -> None:
        """Append live transcript text (Gemini sends it in pieces)."""
        with self._lock:
            if who != self.who:
                self.who, self.text = who, ""
            self.text = (self.text + text)[-400:]
            self.last_active = self.clock()

    def hide_for(self, seconds: float) -> None:
        with self._lock:
            self.hidden_until = self.clock() + seconds

    def flash(self, event: str, seconds: float = 8) -> None:
        with self._lock:
            self.event, self.event_until = event, self.clock() + seconds
            self.last_active = self.clock()

    # ---- read by the pet window ----
    def snapshot(self, focus: dict | None = None, fuel_min: float = 0, jobs: list[dict] | None = None) -> dict:
        focus = focus or {}
        now = self.clock()
        with self._lock:
            event = self.event if now < self.event_until else ""
            if event:
                state = event
            elif self.conv and self.activity != "idle":
                state = self.activity
            elif focus.get("phase") == "focus":
                state = "focus"
            elif focus.get("phase") == "break":
                state = "nap"
            else:
                state = "idle"
            lingering = now - self.last_active < self.cfg["linger_sec"]
            locked = focus.get("phase") == "focus" and focus.get("lockin")
            hide_now = now < self.hidden_until
            if self.mode == "off" or self.on_call or hide_now:
                show = False
            elif self.mode == "stay":
                show = True
            else:
                show = bool(self.conv or lingering or event or locked)
            out = {"mode": self.mode, "show": show, "state": state, "level": round(self.level, 2),
                   "who": self.who, "text": self.text, "note": self.note, "conv": self.conv,
                   "on_call": self.on_call, "hide_now": hide_now, "time": datetime.fromtimestamp(now).strftime("%H:%M"),
                   "fuel": round(min(1.0, fuel_min / max(1, self.cfg["daily_goal_min"])), 2),
                   "fuel_min": round(fuel_min), "level_min": focus.get("level")}
        if focus.get("phase") == "focus":
            out["timer"] = {"left": focus.get("remaining_sec", int(focus.get("remaining_min", 0) * 60)),
                            "total": int(focus.get("planned_min", 0) * 60), "label": focus.get("label") or ""}
        elif focus.get("phase") == "break":
            out["timer"] = {"left": focus.get("break_remaining_sec", 0), "total": 0, "label": "break"}
        running = [j for j in (jobs or []) if j.get("status") == "running"]
        if running:
            out["claude"] = f"Claude: {running[-1].get('question', '')[:40]}"
        return out
