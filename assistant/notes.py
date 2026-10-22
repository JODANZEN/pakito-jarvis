"""Two ways to hand Pakito something to keep.

"Toma nota: …"  -> Inbox: a raw capture in your vault's inbox/ as `YYYY-MM-DD <topic>.md`, in your own words
                   (the vault rules: inbox = unprocessed captures, filed in the weekly sweep; never edit his phrasing).
                   Several notes on the same topic the same day go into the same file.

"Recuérdame …"  -> Reminders (Assistant/state/reminders.json, readable copy in Assistant/Reminders.md):
   * with a date/time ("el martes a las 9", "en 3 días"): a toast + Pakibot pops in when it's due, and
     Pakito brings it up in your next conversation until you've heard it.
   * without one ("recuerda que le presté 20 a Mario"): Pakito asks you about it later (2 days by default),
     and saves what you answer.
Reminders only fire while Pakito is running; anything missed comes up the next time you talk to it.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

BAD = re.compile(r'[\\/:*?"<>|#^\[\]]')


def safe_title(title: str, fallback: str = "nota") -> str:
    t = BAD.sub(" ", (title or "").strip())
    t = re.sub(r"\s+", " ", t).strip(" .")
    return (t[:60].rstrip() or fallback)


class Inbox:
    def __init__(self, vault: Path, clock: Callable[[], float] = time.time):
        self.dir = Path(vault) / "inbox"
        self.clock = clock

    def add(self, text: str, title: str | None = None, kind: str = "note") -> dict:
        text = (text or "").strip()
        if not text:
            return {"error": "Nothing to note."}
        now = datetime.fromtimestamp(self.clock())
        topic = safe_title(title or " ".join(text.split()[:6]))
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{now:%Y-%m-%d} {topic}.md"
        stamp = f"{now:%H:%M}"
        label = {"idea": "💡 idea", "note": "note", "thought": "thought", "todo": "to-do"}.get(kind, kind)
        if path.exists():
            with path.open("a", encoding="utf-8") as f:
                f.write(f"\n**{stamp}** ({label})\n{text}\n")
            return {"noted": str(path.name), "appended": True}
        path.write_text(
            f"# {topic}\n\n*Captured by voice with Pakito, {now:%Y-%m-%d} {stamp} ({label}). Raw: file it in the "
            f"weekly sweep.*\n\n{text}\n\n*Connected to: [[Home]]*\n", encoding="utf-8")
        return {"noted": str(path.name)}


def parse_when(value: str | None, now: datetime) -> datetime | None:
    """ISO date/datetime from Gemini ('2026-10-20T09:00', '2026-10-20'); a bare date means 9:00 am."""
    if not value:
        return None
    v = value.strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(v[:19] if "T" in v or " " in v else v[:10], fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=9)
            return dt
        except ValueError:
            continue
    return None


class Reminders:
    def __init__(self, path: Path, md_path: Path | None = None, clock: Callable[[], float] = time.time,
                 notify: Callable[[str, str], None] = lambda t, m: None, on_fire: Callable[[dict], None] | None = None,
                 ask_after_days: float = 2, in_charge: Callable[[], bool] = lambda: True):
        self.path, self.md_path = Path(path), Path(md_path) if md_path else None
        self.clock, self.notify, self.on_fire = clock, notify, on_fire
        self.ask_after_days = ask_after_days
        self.in_charge = in_charge
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self._sync()

    def _disk(self) -> list[dict]:
        try:
            items = json.loads(self.path.read_text(encoding="utf-8"))
            return [i for i in items if isinstance(i, dict) and i.get("id")] if isinstance(items, list) else []
        except (OSError, ValueError):
            return []

    def _sync(self) -> None:
        """Merge what another computer saved (shared folder) with ours: per reminder, the newer change wins."""
        disk = {i["id"]: i for i in self._disk()}
        with self._lock:
            mine = {i["id"]: i for i in self.items}
            for rid, d in disk.items():
                m = mine.get(rid)
                if m is None or d.get("updated", d.get("created", 0)) > m.get("updated", m.get("created", 0)):
                    mine[rid] = d
            self.items = sorted(mine.values(), key=lambda r: r.get("created", 0))

    def _touch(self, r: dict) -> None:
        r["updated"] = max(self.clock(), r.get("updated", 0) + 1e-6)

    def _save(self) -> None:
        self._sync()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
            if self.md_path:
                self.md_path.write_text(self._markdown(), encoding="utf-8")
        except OSError:
            pass

    def _markdown(self) -> str:
        lines = ["# Reminders", "", "Pakito keeps this list (say \"recuérdame…\"). Checked = done.", ""]
        for r in sorted(self.items, key=lambda r: r["at"]):
            when = datetime.fromtimestamp(r["at"]).strftime("%a %b %d %H:%M")
            kind = "⏰" if r["kind"] == "due" else "❓ ask"
            line = f"- [{'x' if r.get('done') else ' '}] {kind} {when} — {r['what']}"
            if r.get("answer"):
                line += f" → {r['answer']}"
            lines.append(line)
        return "\n".join(lines) + "\n"

    def add(self, what: str, due: str | None = None, ask_in_days: float | None = None) -> dict:
        what = (what or "").strip()
        if not what:
            return {"error": "Remember what?"}
        now = datetime.fromtimestamp(self.clock())
        when = parse_when(due, now)
        if due and not when:
            return {"error": f"Couldn't read the date '{due}'. Pass it as 2026-10-20T09:00."}
        if when and when <= now:
            return {"error": "That time already passed. Pick a future time."}
        kind = "due" if when else "ask"
        if not when:
            when = now + timedelta(days=ask_in_days if ask_in_days is not None else self.ask_after_days)
        item = {"id": uuid.uuid4().hex[:5], "what": what, "kind": kind, "at": when.timestamp(),
                "created": self.clock(), "updated": self.clock(), "fired": False, "told": False, "done": False}
        with self._lock:
            self.items.append(item)
        self._save()
        return {"remembered": what, "kind": kind, "when": when.strftime("%a %b %d %I:%M %p").replace(" 0", " "),
                "id": item["id"]}

    def check(self) -> list[dict]:
        """Fire what's due: toast + pet. Called every 30 s while Pakito runs."""
        self._sync()
        if not self.in_charge():              # another computer you're using now fires them
            return []
        now, fired = self.clock(), []
        with self._lock:
            for r in self.items:
                if not r["done"] and not r["fired"] and r["at"] <= now:
                    r["fired"] = True
                    self._touch(r)
                    fired.append(r)
        for r in fired:
            title = "⏰ Recordatorio" if r["kind"] == "due" else "❓ Pakito quiere saber"
            self.notify(title, r["what"][:200])
            if self.on_fire:
                try:
                    self.on_fire(r)
                except Exception:
                    pass
        if fired:
            self._save()
        return fired

    def pending(self) -> tuple[list[dict], list[dict]]:
        """(due reminders to tell him, things to ask him about) — what the next conversation should bring up."""
        self.check()
        now = self.clock()
        live = [r for r in self.items if not r["done"] and r["at"] <= now]
        return [r for r in live if r["kind"] == "due"], [r for r in live if r["kind"] == "ask"]

    def find(self, ref: str | None) -> dict | None:
        self._sync()
        open_ = [r for r in self.items if not r["done"]]
        if not ref:
            due = [r for r in open_ if r["at"] <= self.clock()]
            return (due or open_ or [None])[0]
        ref = ref.lower().strip()
        for r in open_:
            if r["id"] == ref or ref in r["what"].lower():
                return r
        words = [w for w in re.findall(r"\w+", ref) if len(w) > 3]
        best = max(open_, key=lambda r: sum(w in r["what"].lower() for w in words), default=None)
        return best if best and any(w in best["what"].lower() for w in words) else None

    def done(self, ref: str | None = None, answer: str | None = None) -> dict:
        r = self.find(ref)
        if not r:
            return {"error": "No reminder like that."}
        with self._lock:
            r.update(done=True, told=True, done_at=self.clock())
            if answer:
                r["answer"] = answer.strip()
            self._touch(r)
        self._save()
        return {"done": r["what"], **({"answer": r["answer"]} if answer else {})}

    def snooze(self, ref: str | None, until: str | None = None, days: float | None = None) -> dict:
        r = self.find(ref)
        if not r:
            return {"error": "No reminder like that."}
        now = datetime.fromtimestamp(self.clock())
        when = parse_when(until, now) or now + timedelta(days=days if days is not None else 1)
        with self._lock:
            r.update(at=when.timestamp(), fired=False, told=False)
            self._touch(r)
        self._save()
        return {"snoozed": r["what"], "until": when.strftime("%a %b %d %I:%M %p").replace(" 0", " ")}

    def upcoming(self) -> list[dict]:
        self._sync()
        out = []
        for r in sorted((r for r in self.items if not r["done"]), key=lambda r: r["at"]):
            out.append({"id": r["id"], "what": r["what"], "kind": r["kind"],
                        "when": datetime.fromtimestamp(r["at"]).strftime("%a %b %d %I:%M %p").replace(" 0", " ")})
        return out

    def start(self, interval: float = 30) -> None:
        def loop():
            while True:
                time.sleep(interval)
                try:
                    self.check()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()
