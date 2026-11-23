"""Follow-ups: things he said he'd get to, so they don't vanish when he keeps working.

Two kinds of things land here (state/followups.json, shared by every computer and the nightly dream):
  * Pakito's own: a Claude question that got blocked or failed, or "luego le digo a Claude que…" (followups add).
  * The dream's: once a day Claude replays his conversations, finds what he said he wanted done but never got
    done, and writes it here with a short plan (which steps Claude can do itself, which need him).

Pakito brings one up at most every few hours ("dijiste que querías pasar los syllabus al calendario; ¿se lo paso a
Claude?") and can hand it to Claude with the plan. An item sent to Claude closes itself when Claude finishes it,
and reopens if Claude got blocked.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

MAX_OPEN = 8


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"\w+", s.lower()) if len(w) > 3}


def similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6


class FollowUps:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()

    # ---------- the file (the dream writes it too: read fresh every time) ----------
    def load(self) -> list[dict]:
        try:
            items = json.loads(self.path.read_text(encoding="utf-8"))
            return [i for i in items if isinstance(i, dict) and i.get("what")] if isinstance(items, list) else []
        except (OSError, ValueError):
            return []

    def save(self, items: list[dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _stamp(self) -> str:
        return datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds")

    # ---------- keeping it current with Claude's jobs ----------
    def sync(self, jobs: dict[str, dict]) -> list[dict]:
        """Items sent to Claude: done when Claude finished cleanly, open again if it got blocked or failed."""
        items = self.load()
        changed = False
        for i in items:
            j = jobs.get(i.get("job") or "")
            if i.get("status") != "sent" or not j or j.get("status") == "running":
                continue
            if j.get("status") == "done" and not j.get("blocked_tools"):
                i.update(status="done", closed_why=f"Claude did it (job {j['id']})", updated=self._stamp())
            else:
                i.update(status="open", updated=self._stamp(),
                         note=("Claude got blocked: " + ", ".join(j.get("blocked_tools") or [])) if j.get(
                             "blocked_tools") else f"Claude failed: {str(j.get('answer', ''))[:120]}")
            changed = True
        if changed:
            self.save(items)
        return items

    def open(self, jobs: dict[str, dict] | None = None) -> list[dict]:
        items = self.sync(jobs or {})
        return [i for i in items if i.get("status", "open") == "open"]

    # ---------- what Pakito does with them ----------
    def add(self, what: str, said: str | None = None, plan: list[str] | str | None = None, source: str = "pakito",
            job: str | None = None, who: str | None = None) -> dict:
        what = (what or "").strip()
        if not what:
            return {"error": "What should I come back to?"}
        if isinstance(plan, str):
            plan = [p.strip(" -•") for p in re.split(r"\n|;", plan) if p.strip(" -•")]
        with self._lock:
            items = self.load()
            same = next((i for i in items if i.get("status", "open") in ("open", "sent")
                         and ((job and i.get("job") == job) or similar(i["what"], what))), None)
            if same:                                       # the same thing again: refresh it, don't duplicate
                same.update({k: v for k, v in (("plan", plan), ("said", said), ("job", job), ("who", who)) if v},
                            status="open", updated=self._stamp())
                self.save(items)
                return {"updated": same["id"], "what": same["what"]}
            day = datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")
            n = 1 + sum(1 for i in items if str(i.get("id", "")).startswith(f"f-{day}-"))
            item = {"id": f"f-{day}-{n}", "what": what[:300], "status": "open", "source": source,
                    "said_at": self._stamp(), "updated": self._stamp()}
            item.update({k: v for k, v in (("said", said), ("plan", plan), ("job", job), ("who", who)) if v})
            items.append(item)
            live = [i for i in items if i.get("status", "open") == "open"]
            for old in sorted(live, key=lambda i: i.get("said_at", ""))[:max(0, len(live) - MAX_OPEN)]:
                old.update(status="dropped", closed_why="too many open; oldest dropped", updated=self._stamp())
            self.save(items)
        return {"added": item["id"], "what": item["what"]}

    def find(self, ref: str | None, items: list[dict] | None = None) -> dict | None:
        items = items if items is not None else self.load()
        live = [i for i in items if i.get("status", "open") in ("open", "sent")]
        if not ref:
            return live[-1] if live else None
        r = str(ref).lower().strip()
        return next((i for i in live if i.get("id") == r), None) or \
            next((i for i in live if r in i["what"].lower() or similar(i["what"], r)), None)

    def close(self, ref: str | None, status: str = "done", why: str | None = None) -> dict:
        with self._lock:
            items = self.load()
            i = self.find(ref, items)
            if not i:
                return {"error": "No follow-up like that.", "open": [x["what"] for x in items
                                                                    if x.get("status", "open") == "open"][:5]}
            i.update(status=status, updated=self._stamp(), **({"closed_why": why} if why else {}))
            self.save(items)
        return {status: i["what"]}

    def mark_sent(self, ref: str, job: str) -> None:
        with self._lock:
            items = self.load()
            i = self.find(ref, items)
            if i:
                i.update(status="sent", job=job, updated=self._stamp())
                self.save(items)

    def offer(self, jobs: dict[str, dict] | None = None, cooldown_h: float = 4) -> dict | None:
        """The one follow-up this conversation may bring up: least recently offered, at most every cooldown_h."""
        live = self.open(jobs)
        if not live:
            return None
        now = self.clock()
        if now - max((i.get("offered_ts", 0) for i in live), default=0) < cooldown_h * 3600:
            return None
        pick = min(live, key=lambda i: i.get("offered_ts", 0))
        with self._lock:
            items = self.load()
            for i in items:
                if i.get("id") == pick["id"]:
                    i["offered_ts"] = now
            self.save(items)
        return pick

    @staticmethod
    def brief(i: dict) -> dict:
        out = {"id": i["id"], "what": i["what"]}
        for k in ("plan", "said", "note", "who"):
            if i.get(k):
                out[k] = i[k]
        if i.get("said_at"):
            out["when"] = i["said_at"][:10]
        return out

    @staticmethod
    def question_for_claude(i: dict) -> str:
        """What Claude gets when he says 'hazlo' / 'pásaselo a Claude'."""
        q = f"Follow-up he asked for earlier: {i['what']}"
        if i.get("said"):
            q += f'\nHis words: "{i["said"]}"'
        if i.get("plan"):
            q += "\nPlan so far:\n" + "\n".join(f"- {p}" for p in (i["plan"] if isinstance(i["plan"], list)
                                                                    else [i["plan"]]))
        return q + ("\nDo the parts you can do yourself (his notes are yours to read and write), then say in the spoken "
                    "answer what's done and what he still has to do.")
