"""What's coming: Pakito reads your Google Calendar through its private iCal links (read-only, no login).

Your Saturday brief and sync-brain keep exams (🔴), study blocks (📚), classes and work in Google Calendar,
so this is how Pakito knows the same things: "¿qué tengo hoy?", "¿qué viene esta semana?", and a short
"coming up" line in every conversation.

Setup once:  run-pakito.bat calendar
  Google Calendar (web) → ⚙ Settings → click a calendar on the left → "Integrate calendar" →
  copy "Secret address in iCal format". Do it for your main calendar, plus classes / Work if you want them.
  For Canvas, use Canvas → Calendar → "Calendar Feed" (that's where projects show up).
The links are secrets (anyone with one can read that calendar): they live only in config.local.json.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

BIG = re.compile(r"🔴|\b(exam|midterm|final|test|project|presentation)\b", re.I)   # not quizzes/HW: he checks Canvas
DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


# ---------- parsing (small on purpose: Google/Canvas feeds only need this much) ----------
def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _when(params: str, value: str) -> tuple[datetime, bool]:
    """-> (local naive datetime, all_day)."""
    value = value.strip()
    if "VALUE=DATE" in params or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d"), True
    dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None), False
    m = re.search(r"TZID=([^;:]+)", params)
    if m:
        try:
            from zoneinfo import ZoneInfo
            return dt.replace(tzinfo=ZoneInfo(m.group(1))).astimezone().replace(tzinfo=None), False
        except Exception:
            pass                                    # no tz database: treat as local (fine for Phoenix calendars)
    return dt, False


def parse_ics(text: str, label: str = "", role: str = "main") -> list[dict]:
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"exdates": set()}
        elif line == "END:VEVENT" and cur is not None:
            if "start" in cur and cur.get("status") != "CANCELLED":
                cur.setdefault("end", cur["start"] + (timedelta(days=1) if cur["all_day"] else timedelta(hours=1)))
                cur.update(calendar=label, role=role)
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            name = name.upper()
            if name == "DTSTART":
                cur["start"], cur["all_day"] = _when(params, value)
            elif name == "DTEND":
                cur["end"], _ = _when(params, value)
            elif name == "SUMMARY":
                cur["summary"] = _unescape(value).strip()
            elif name == "DESCRIPTION":
                cur["description"] = _unescape(value).strip()[:300]
            elif name == "LOCATION":
                cur["location"] = _unescape(value).strip()
            elif name == "RRULE":
                cur["rrule"] = dict(p.split("=", 1) for p in value.split(";") if "=" in p)
            elif name == "EXDATE":
                for v in value.split(","):
                    cur["exdates"].add(_when(params, v)[0])
            elif name == "STATUS":
                cur["status"] = value.strip().upper()
            elif name == "UID":
                cur["uid"] = value.strip()
            elif name == "RECURRENCE-ID":
                cur["recurrence_id"] = _when(params, value)[0]
    return events


def expand(events: list[dict], start: datetime, end: datetime) -> list[dict]:
    """Occurrences overlapping [start, end). Expands DAILY/WEEKLY repeats (classes, shifts); moved single
    occurrences (RECURRENCE-ID) replace the original slot."""
    moved = {(e.get("uid"), e["recurrence_id"]) for e in events if e.get("recurrence_id")}
    out = []
    for e in events:
        dur = e["end"] - e["start"]
        rule = e.get("rrule")
        if not rule or e.get("recurrence_id"):
            if e["start"] < end and e["start"] + dur > start:
                out.append(e)
            continue
        freq = rule.get("FREQ", "")
        if freq not in ("DAILY", "WEEKLY"):
            if e["start"] < end and e["start"] + dur > start:
                out.append(e)
            continue
        step = int(rule.get("INTERVAL", 1))
        until = _when("", rule["UNTIL"])[0] if "UNTIL" in rule else None
        count = int(rule["COUNT"]) if "COUNT" in rule else None
        byday = [DAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in DAYS] or [e["start"].weekday()]
        n, day = 0, e["start"]
        week0 = e["start"] - timedelta(days=e["start"].weekday())
        while day < end:
            if until and day > until + timedelta(days=1):
                break
            if freq == "DAILY":
                ok = (day - e["start"]).days % step == 0
            else:
                weeks = ((day - timedelta(days=day.weekday())) - week0).days // 7
                ok = day.weekday() in byday and weeks % step == 0
            if ok and day >= e["start"]:
                n += 1
                if count and n > count:
                    break
                if until and day > until:
                    break
                if day not in e["exdates"] and (e.get("uid"), day) not in moved and day + dur > start:
                    out.append({**e, "start": day, "end": day + dur})
            day += timedelta(days=1)
    return sorted(out, key=lambda x: x["start"])


def is_big(e: dict) -> bool:
    """Exams and projects themselves, not the 📚 study blocks named after them."""
    s = e.get("summary", "")
    return bool(BIG.search(s)) and "📚" not in s and not re.search(r"\b(study|prep|review|final push)\b", s, re.I)


def describe(e: dict, now: datetime) -> str:
    d = e["start"].date()
    delta = (d - now.date()).days
    when = "today" if delta == 0 else "tomorrow" if delta == 1 else e["start"].strftime("%a %b %d").replace(" 0", " ")
    if not e["all_day"]:
        when += " " + e["start"].strftime("%I:%M%p").lstrip("0").lower()
    return f"{when}: {e.get('summary', '(no title)')}"


# ---------- the feed ----------
class Calendar:
    def __init__(self, feeds: list[dict], cache: Path, fetch: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.time, refresh_min: float = 30):
        self.feeds = [f for f in feeds or [] if f.get("url")]
        self.cache = Path(cache)
        self.fetch = fetch or self._fetch
        self.clock = clock
        self.refresh_s = refresh_min * 60
        self.events: list[dict] = []
        self.fetched_at = 0.0
        self.error = ""
        self._lock = threading.Lock()
        self._load_cache()

    @staticmethod
    def _fetch(url: str) -> str:
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Pakito"}), timeout=10) as r:
            return r.read().decode("utf-8", errors="replace")

    def _load_cache(self) -> None:
        try:
            raw = json.loads(self.cache.read_text(encoding="utf-8"))
            evs = []
            for e in raw["events"]:
                e["start"], e["end"] = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
                e["exdates"] = {datetime.fromisoformat(x) for x in e.get("exdates", [])}
                if e.get("recurrence_id"):
                    e["recurrence_id"] = datetime.fromisoformat(e["recurrence_id"])
                evs.append(e)
            self.events, self.fetched_at = evs, raw.get("fetched_at", 0)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def refresh(self) -> None:
        evs, errors = [], []
        for f in self.feeds:
            try:
                evs += parse_ics(self.fetch(f["url"]), f.get("label", ""), f.get("role", "main"))
            except Exception as e:
                errors.append(f"{f.get('label') or 'calendar'}: {type(e).__name__}")
        with self._lock:
            if evs or not errors:
                self.events, self.fetched_at = evs, self.clock()
            self.error = "; ".join(errors)
        try:
            ser = [{**e, "start": e["start"].isoformat(), "end": e["end"].isoformat(),
                    "exdates": [x.isoformat() for x in e.get("exdates", [])],
                    **({"recurrence_id": e["recurrence_id"].isoformat()} if e.get("recurrence_id") else {})}
                   for e in self.events]
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            self.cache.write_text(json.dumps({"fetched_at": self.fetched_at, "events": ser}, ensure_ascii=False),
                                  encoding="utf-8")
        except OSError:
            pass

    def maybe_refresh(self, background: bool = True) -> None:
        if not self.feeds or self.clock() - self.fetched_at < self.refresh_s:
            return
        self.fetched_at = self.clock()                     # don't start two refreshes at once
        if background:
            threading.Thread(target=self.refresh, daemon=True).start()
        else:
            self.refresh()

    def between(self, start: datetime, end: datetime, roles: tuple | None = None) -> list[dict]:
        with self._lock:
            evs = list(self.events)
        out = expand(evs, start, end)
        return [e for e in out if roles is None or e.get("role") in roles]

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock())

    def coming_up(self, days: int = 10) -> list[str]:
        """The short lines Gemini sees each conversation: what's left today + the big things soon."""
        now = self.now()
        tonight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        today = [e for e in self.between(now, tonight, ("main", "classes", "work")) if not e["all_day"] or is_big(e)]
        big = [e for e in self.between(tonight, now + timedelta(days=days)) if is_big(e)]
        lines = [describe(e, now) for e in today[:4]] + [describe(e, now) for e in big[:5]]
        seen, out = set(), []
        for l in lines:
            if l not in seen:
                seen.add(l)
                out.append(l)
        return out


def setup() -> None:
    """run-pakito.bat calendar"""
    local = Path(__file__).with_name("config.local.json")
    cfg = json.loads(local.read_text(encoding="utf-8")) if local.exists() else {}
    print(__doc__)
    feeds = (cfg.get("calendar") or {}).get("feeds", [])
    print(f"You have {len(feeds)} calendar(s) set up: {[f.get('label') for f in feeds]}\n")
    roles = {"1": "main", "2": "classes", "3": "work", "4": "canvas"}
    while True:
        url = input("Paste a secret iCal link (or Enter to finish): ").strip()
        if not url:
            break
        r = input("What is it? 1 = main (exams, study blocks)  2 = classes  3 = work  4 = Canvas/assignments: ").strip()
        label = input("Short name for it (e.g. main, classes, Canvas): ").strip() or roles.get(r, "main")
        try:
            n = len(parse_ics(Calendar._fetch(url)))
            print(f"  ✅ read {n} events")
        except Exception as e:
            print(f"  ⚠ couldn't read it right now ({type(e).__name__}); saving it anyway")
        feeds = [f for f in feeds if f.get("url") != url] + [{"label": label, "role": roles.get(r, "main"), "url": url}]
    cfg.setdefault("calendar", {})["feeds"] = feeds
    local.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    cal = Calendar(feeds, Path(__file__).with_name(".calendar_test.json"))
    cal.refresh()
    print("\nComing up:\n  " + "\n  ".join(cal.coming_up() or ["(nothing in the next 10 days)"]))
    try:
        Path(__file__).with_name(".calendar_test.json").unlink()
    except OSError:
        pass


if __name__ == "__main__":
    setup()
