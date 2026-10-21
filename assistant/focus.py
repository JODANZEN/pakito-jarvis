"""Adaptive focus trainer — progressive overload for attention.

Rules (all tunable in config["focus"]):
  * A block's length is your current *level* (starts at 20 min).
  * Finish the block with <= 2 logged distractions  -> level goes up 2 min.
  * Bail before half the block                     -> level goes down 2 min.
  * Anything in between                            -> level stays.
  * Blocks where you pick the minutes yourself are logged but don't move the level,
    so the level only tracks what the trainer prescribed.
  * Break = 1/5 of the block (min 5); every 4th completed block gets a 15-min break.

Every finished block is one line in <vault>/Assistant/logs/focus.jsonl.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from .util import JsonlLog, now_iso


class FocusTrainer:
    def __init__(
        self,
        cfg: dict,
        state_path: Path,
        log: JsonlLog,
        notify: Callable[[str, str], None],
        clock: Callable[[], float] = time.time,
        timer_factory=threading.Timer,
    ):
        self.cfg = cfg
        self.state_path = Path(state_path)
        self.log = log
        self.notify = notify
        self.clock = clock
        self.timer_factory = timer_factory
        self._lock = threading.RLock()
        self._timer = None
        self.phase = "idle"           # idle | focus | break
        self.listeners: list = []     # called with the new phase ("focus" | "break" | "idle")
        self.block: dict | None = None
        self.break_ends: float | None = None
        self.state = self._load_state()

    # ---------- persistence ----------
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"level_min": self.cfg["start_level_min"], "completed_since_long_break": 0}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def _emit(self, phase: str) -> None:
        for cb in list(self.listeners):
            try:
                threading.Thread(target=cb, args=(phase,), daemon=True).start() if self.async_listeners else cb(phase)
            except Exception:
                pass

    async_listeners = True            # tests flip this to run listeners inline

    # ---------- timers ----------
    def _schedule(self, seconds: float, fn) -> None:
        self._cancel()
        t = self.timer_factory(seconds, fn)
        t.daemon = True
        t.start()
        self._timer = t

    def _cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    # ---------- public API (these become voice tools) ----------
    @property
    def level(self) -> int:
        return int(self.state["level_min"])

    @property
    def locked(self) -> bool:
        self.catch_up()                       # the blocker polls this: a block the PC slept through unlocks
        return self.phase == "focus" and bool(self.block and self.block.get("lockin"))

    def catch_up(self) -> None:
        """Finish a block/break whose time already passed. Timers stall while the PC sleeps (a 58-min block from
        9 pm ended at 7 am), so every status check also checks the wall clock."""
        with self._lock:
            now = self.clock()
            if self.phase == "focus" and self.block and now >= self.block["start"] + self.block["planned_min"] * 60:
                self._on_focus_done(at=self.block["start"] + self.block["planned_min"] * 60)
            if self.phase == "break" and self.break_ends is not None and now >= self.break_ends:
                self._on_break_done(quiet=now - self.break_ends > 600)

    def start(self, minutes: float | None = None, label: str | None = None, lockin: bool = False) -> dict:
        self.catch_up()
        with self._lock:
            if self.phase == "focus":
                if lockin and not self.block.get("lockin"):        # "lock in" mid-block: upgrade, don't restart
                    self.block["lockin"] = True
                    if label and not self.block.get("label"):
                        self.block["label"] = label.strip()
                    self.notify("Focus", "Locked in 🔒 for the rest of this block.")
                    self._emit("focus")
                    return {"upgraded": True, "lockin": True, **self.status()}
                return {"error": "A focus block is already running.", **self.status()}
            self._cancel()
            custom = minutes is not None
            planned = float(minutes) if custom else float(self.level)
            self.block = {
                "start": self.clock(),
                "planned_min": planned,
                "label": (label or "").strip() or None,
                "custom": custom,
                "distractions": [],
                "level_before": self.level,
                "lockin": bool(lockin),
            }
            self.phase = "focus"
            self.break_ends = None
            self._schedule(planned * 60, self._on_focus_done)
            what = f" — {self.block['label']}" if self.block["label"] else ""
            lock = " Locked in 🔒" if lockin else ""
            self.notify("Focus", f"{planned:g} min{what}. Go.{lock}")
            self._emit("focus")
            return {"started": True, "minutes": planned, "label": self.block["label"], "level": self.level,
                    "lockin": bool(lockin)}

    def distracted(self, note: str | None = None) -> dict:
        self.catch_up()
        with self._lock:
            if self.phase != "focus":
                return {"error": "No focus block running."}
            at = (self.clock() - self.block["start"]) / 60
            self.block["distractions"].append({"at_min": round(at, 1), "note": note})
            return {"distractions": len(self.block["distractions"]), "at_min": round(at, 1)}

    def stop(self) -> dict:
        self.catch_up()
        with self._lock:
            if self.phase == "focus":
                rec = self._finish(completed=False)
                self.phase = "idle"
                self._cancel()
                return {"stopped": True, "block": rec, "next_level": self.level}
            if self.phase == "break":
                self._cancel()
                self.phase = "idle"
                self.break_ends = None
                return {"stopped": True, "skipped_break": True, "next_level": self.level}
            return {"error": "Nothing running."}

    def status(self) -> dict:
        self.catch_up()
        with self._lock:
            out = {"phase": self.phase, "level": self.level}
            if self.phase == "focus":
                out["lockin"] = bool(self.block.get("lockin"))
                elapsed = (self.clock() - self.block["start"]) / 60
                out.update(
                    label=self.block["label"],
                    planned_min=self.block["planned_min"],
                    remaining_min=round(max(0.0, self.block["planned_min"] - elapsed), 1),
                    remaining_sec=int(max(0.0, self.block["planned_min"] * 60 - (self.clock() - self.block["start"]))),
                    distractions=len(self.block["distractions"]),
                )
            elif self.phase == "break":
                out["break_remaining_min"] = round(max(0.0, (self.break_ends - self.clock()) / 60), 1)
                out["break_remaining_sec"] = int(max(0.0, self.break_ends - self.clock()))
            return out

    def stats(self, days: int = 7) -> dict:
        cutoff = self.clock() - days * 86400
        blocks = [b for b in self.log.read() if b.get("start_ts", 0) >= cutoff]
        if not blocks:
            return {"days": days, "blocks": 0, "level": self.level}
        done = [b for b in blocks if b["completed"]]
        return {
            "days": days,
            "blocks": len(blocks),
            "completed": len(done),
            "completion_rate": round(len(done) / len(blocks), 2),
            "focus_minutes": round(sum(b["actual_min"] for b in blocks), 1),
            "avg_block_min": round(sum(b["actual_min"] for b in blocks) / len(blocks), 1),
            "distractions_per_block": round(sum(b["distractions"] for b in blocks) / len(blocks), 2),
            "level_start": blocks[0]["level_before"],
            "level_now": self.level,
        }

    # ---------- internals ----------
    def _on_focus_done(self, at: float | None = None) -> None:
        with self._lock:
            if self.phase != "focus":
                return
            rec = self._finish(completed=True, at=at)
            n = self.state["completed_since_long_break"]
            if n >= self.cfg["long_break_every"]:
                brk = float(self.cfg["long_break_min"])
                self.state["completed_since_long_break"] = 0
                self._save_state()
            else:
                brk = float(max(self.cfg["min_break_min"], round(rec["planned_min"] * self.cfg["break_ratio"])))
            self.phase = "break"
            self.break_ends = (at or self.clock()) + brk * 60      # a late catch-up keeps the real schedule
            self._schedule(max(0.0, self.break_ends - self.clock()), self._on_break_done)
            up = " Level up." if rec["level_after"] > rec["level_before"] else ""
            self.notify("Block done ✓", f"{brk:g} min break. Next block: {self.level} min.{up}")
            self._emit("break")

    def _on_break_done(self, quiet: bool = False) -> None:
        with self._lock:
            if self.phase != "break":
                return
            self.phase = "idle"
            self.break_ends = None
            self._cancel()
            if not quiet:                            # long past (the PC slept through it): no stale toast
                self.notify("Break over", f"Next block is {self.level} min. Say 'focus' when you're ready.")
            self._emit("idle")

    def _finish(self, completed: bool, at: float | None = None) -> dict:
        b = self.block
        end = at if at is not None else self.clock()
        actual = min((end - b["start"]) / 60, b["planned_min"]) if completed else (end - b["start"]) / 60
        c = self.cfg
        before = self.level
        after = before
        if not b["custom"]:
            if completed and len(b["distractions"]) <= c["max_distractions_for_levelup"]:
                after = before + c["step_up_min"]
            elif not completed and actual < b["planned_min"] * c["early_stop_ratio"]:
                after = before - c["step_down_min"]
            after = max(c["min_level_min"], min(c["max_level_min"], after))
        if completed:
            self.state["completed_since_long_break"] += 1
        self.state["level_min"] = after
        self._save_state()
        rec = {
            "start": now_iso(b["start"]),
            "start_ts": b["start"],
            "end": now_iso(end),
            "label": b["label"],
            "planned_min": b["planned_min"],
            "actual_min": round(actual, 2),
            "completed": completed,
            "custom_length": b["custom"],
            "distractions": len(b["distractions"]),
            "distraction_notes": b["distractions"],
            "lockin": b.get("lockin", False),
            "level_before": before,
            "level_after": after,
        }
        self.log.append(rec)
        self.block = None
        return rec
