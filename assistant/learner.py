"""Learns routines from what you actually do.

An *episode* = a request you made ("cse 355 homework") + the actions that followed
within a few minutes. When the same set of actions follows similar requests
`repeat_threshold` times, it suggests saving them as a routine.

Episodes are stored in <vault>/Assistant/logs/episodes.jsonl.
"""
from __future__ import annotations

import time
import uuid
from collections import Counter
from typing import Callable

from .routines import action_signature, normalize
from .util import JsonlLog, now_iso

# tool name -> (routine action type, which arg holds the value)
RECORDABLE = {
    "open_url": ("open", "url"),
    "open_app": ("app", "name"),
    "play_music": ("music", "query"),
    "web_search": ("search", "query"),
    "start_focus": ("focus", "label"),
    "open_book": ("book", "book"),
}


class Learner:
    def __init__(self, cfg: dict, log: JsonlLog, dismissed_log: JsonlLog | None = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.log = log
        self.dismissed_log = dismissed_log
        self.clock = clock
        self.current: dict | None = None
        self.dismissed: set[tuple] = set()
        if dismissed_log:
            for d in dismissed_log.read():
                self.dismissed.add(tuple(tuple(x) for x in d["signature"]))

    def begin(self, utterance: str, source: str = "typed") -> str:
        self.close()
        self.current = {
            "id": uuid.uuid4().hex[:8],
            "utterance": utterance.strip(),
            "source": source,
            "ts": self.clock(),
            "actions": [],
        }
        return self.current["id"]

    def observe(self, tool: str, args: dict) -> None:
        if self.current is None or tool not in RECORDABLE:
            return
        if self.clock() - self.current["ts"] > self.cfg["episode_window_sec"]:
            self.close()
            return
        typ, key = RECORDABLE[tool]
        val = str(args.get(key) or "").strip()
        if typ == "focus":
            mins = args.get("minutes")
            val = f"{mins:g} {val}".strip() if isinstance(mins, (int, float)) else val
        action = {"type": typ, "value": val}
        if action not in self.current["actions"]:
            self.current["actions"].append(action)

    def close(self) -> dict | None:
        """Save the current episode. Returns a new suggestion if this one tipped a pattern over."""
        ep, self.current = self.current, None
        if not ep or not ep["actions"]:
            return None
        ep["time"] = now_iso(ep["ts"])
        self.log.append(ep)
        sig = action_signature(ep["actions"])
        for s in self.suggestions():
            if s["signature"] == sig and s["count"] == self.cfg["repeat_threshold"]:
                return s
        return None

    def suggestions(self, existing_routines: list[dict] | None = None) -> list[dict]:
        groups: dict[tuple, list[dict]] = {}
        for ep in self.log.read():
            if ep.get("actions"):
                groups.setdefault(action_signature(ep["actions"]), []).append(ep)
        taken = {action_signature(r["actions"]) for r in (existing_routines or [])}
        out = []
        for sig, eps in groups.items():
            if len(eps) < self.cfg["repeat_threshold"] or sig in taken or sig in self.dismissed:
                continue
            names = Counter(normalize(e["utterance"]) for e in eps)
            name, _ = names.most_common(1)[0]
            out.append(
                {
                    "name": name,
                    "aliases": [n for n in names if n != name][:5],
                    "actions": eps[-1]["actions"],  # most recent version of the actions
                    "count": len(eps),
                    "signature": sig,
                }
            )
        out.sort(key=lambda s: -s["count"])
        return out

    def dismiss(self, signature: tuple) -> None:
        self.dismissed.add(signature)
        if self.dismissed_log:
            self.dismissed_log.append({"signature": [list(x) for x in signature], "time": now_iso(self.clock())})
