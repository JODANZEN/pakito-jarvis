"""Daily debrief: what you did today, from the logs Pakito already keeps.

`summary()` is read-only ("how did I do today?"). `end_day()` also stops any running block,
pauses music, and writes <vault>/Assistant/Days/<date>.md.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

EXAMEN = [  # Ignatian Daily Examen, spoken. Edit in config["examen"] if you want different wording.
    "¿Por qué das gracias hoy?",
    "Tómate un momento y pide luz para ver tu día con claridad. Dime cuando estés listo.",
    "Repasa el día: ¿dónde sentiste paz o cercanía con Dios, y dónde te sentiste lejos o inquieto?",
    "¿Hay algo de hoy por lo que quieras pedir perdón o que quieras soltar?",
    "¿Qué le pides a Dios para mañana? ¿Una cosa concreta para mañana?",
]


def _record_path(hub, day: str):
    return hub.dir / "state" / "days" / f"{day}.json"


def load_record(hub, day: str) -> dict:
    p = _record_path(hub, day)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_record(hub, day: str, rec: dict) -> None:
    p = _record_path(hub, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def _same_day(ts: float, day) -> bool:
    return datetime.fromtimestamp(ts).date() == day


def summary(hub, day=None) -> dict:
    day = day or datetime.fromtimestamp(hub.clock()).date()
    blocks = [b for b in hub.focus.log.read() if _same_day(b.get("start_ts", 0), day)]
    events = [e for e in hub.events.read() if _same_day(e.get("ts", 0), day) and e.get("ok")]
    done = [b for b in blocks if b["completed"]]
    minutes = round(sum(b["actual_min"] for b in blocks))
    distractions = sum(b["distractions"] for b in blocks)
    blocked = sum(1 for b in blocks for d in b.get("distraction_notes", []) if "(blocked)" in (d.get("note") or ""))
    labels = Counter((b.get("label") or "no label") for b in blocks)
    routines = [e["args"].get("name") for e in events if e["tool"] == "run_routine"]
    questions = [e["args"].get("question") for e in events if e["tool"] == "ask_claude"]
    level_start = blocks[0]["level_before"] if blocks else hub.focus.level
    out = {
        "date": day.isoformat(),
        "blocks": len(blocks),
        "completed": len(done),
        "focus_minutes": minutes,
        "distractions": distractions,
        "blocked_attempts": blocked,
        "by_label": dict(labels),
        "level_start": level_start,
        "level_now": hub.focus.level,
        "routines": routines,
        "claude_questions": questions,
    }
    out["spoken"] = _spoken(out)
    return out


def _spoken(s: dict) -> str:
    if not s["blocks"]:
        parts = ["No focus blocks today."]
    else:
        h, m = divmod(s["focus_minutes"], 60)
        time_txt = f"{h} h {m} min" if h else f"{m} min"
        n = s["blocks"]
        parts = [f"{n} block{'s' if n != 1 else ''}, {time_txt} focused, {s['completed']} finished."]
        if s["distractions"]:
            extra = f", {s['blocked_attempts']} of them blocked sites or apps" if s["blocked_attempts"] else ""
            parts.append(f"{s['distractions']} distraction{'s' if s['distractions'] != 1 else ''}{extra}.")
        else:
            parts.append("Zero distractions.")
        if s["level_now"] != s["level_start"]:
            parts.append(f"Level went from {s['level_start']} to {s['level_now']} minutes.")
        top = max(s["by_label"].items(), key=lambda kv: kv[1])[0] if s["by_label"] else None
        if top and top != "no label" and len(s["by_label"]) > 1:
            parts.append(f"Most time on {top}.")
    if s["claude_questions"]:
        parts.append(f"You asked Claude {len(s['claude_questions'])} thing{'s' if len(s['claude_questions']) > 1 else ''}.")
    return " ".join(parts)


def note_markdown(s: dict) -> str:
    lines = [f"# {s['date']}", "", s["spoken"], "", "## Focus",
             f"- Blocks: {s['blocks']} ({s['completed']} finished)",
             f"- Focused: {s['focus_minutes']} min",
             f"- Distractions: {s['distractions']} ({s['blocked_attempts']} blocked)",
             f"- Level: {s['level_start']} → {s['level_now']} min"]
    if s["by_label"]:
        lines.append("- By subject: " + ", ".join(f"{k} ×{v}" for k, v in s["by_label"].items()))
    if s["routines"]:
        lines += ["", "## Routines", *[f"- {r}" for r in s["routines"]]]
    if s["claude_questions"]:
        lines += ["", "## Asked Claude", *[f"- {q}" for q in s["claude_questions"]]]
    rec = s.get("record", {})
    done, still = rec.get("done") or s.get("tasks_done") or [], rec.get("still_open") or []
    if done or still:
        lines += ["", "## Done today", *([f"- [x] {d}" for d in done] or ["- (nothing logged)"])]
        if still:
            lines += ["", "## Still open → tomorrow", *[f"- [ ] {t}" for t in still]]
    if rec.get("projects"):
        lines += ["", "## Proyectos", rec["projects"]]
    if rec.get("note"):
        lines += ["", "## Notes", rec["note"]]
    ex = rec.get("examen")
    if ex:
        lines += ["", "## Examen"]
        for q, a in ex:
            lines += [f"**{q}**", a or "—", ""]
    return "\n".join(lines).rstrip() + "\n"


def write_note(hub, s: dict) -> str:
    days = hub.dir / "Days"
    days.mkdir(parents=True, exist_ok=True)
    s["record"] = load_record(hub, s["date"])
    (days / f"{s['date']}.md").write_text(note_markdown(s), encoding="utf-8")
    return f"Days/{s['date']}.md"


def log_day(hub, done: list[str] | None = None, still_open: list[str] | None = None, note: str | None = None,
            projects: str | None = None) -> dict:
    """What he says he finished / still has open. Updates the task list and the day's note."""
    s = summary(hub)
    day = s["date"]
    rec = load_record(hub, day)
    results = []
    for d in done or []:
        results.append(hub.tasks.complete(d, day))
    for t in still_open or []:
        hub.tasks.add(t)
    rec["done"] = list(dict.fromkeys((rec.get("done") or []) + [r["completed"] for r in results]))
    rec["still_open"] = list(dict.fromkeys(still_open or rec.get("still_open") or []))
    if note:
        rec["note"] = ((rec.get("note") or "") + "\n" + note).strip()
    if projects:
        rec["projects"] = ((rec.get("projects") or "") + "\n" + projects).strip()
    save_record(hub, day, rec)
    path = write_note(hub, s)
    return {"logged_done": rec["done"], "still_open": rec["still_open"], "open_tasks_now": hub.tasks.open(),
            "note": path}


def save_examen(hub, answers: list[str]) -> dict:
    s = summary(hub)
    rec = load_record(hub, s["date"])
    qs = hub.cfg.get("examen") or EXAMEN
    rec["examen"] = [[q, (answers[i] if i < len(answers) else "")] for i, q in enumerate(qs)]
    save_record(hub, s["date"], rec)
    return {"saved": True, "note": write_note(hub, s)}


def end_day(hub) -> dict:
    stopped = None
    if hub.focus.phase != "idle":
        stopped = hub.focus.stop()
    if hub.spotify:
        try:
            hub.spotify.control("pause")
        except Exception:
            pass
    s = summary(hub)
    s["note"] = write_note(hub, s)
    if stopped:
        s["stopped_block"] = True
    s["open_tasks"] = hub.tasks.open()
    s["next"] = ("Read 'spoken'. Then ask what he got done today and what's still open (mention open_tasks "
                 "briefly if any), then ask how his projects went today (what moved, what got stuck), and call log_day "
                 "with his answers (projects in his words). Then ask if he wants to do the examen.")
    qs = take_dream_questions(hub)
    if qs:
        s["dream_questions"] = qs
        s["next"] = s["next"].replace("Then ask if he wants to do the examen.",
            "Then ask him the dream_questions, one at a time (they come from his Second Brain's daily dream; just "
            "listen, never answer for him), saving each answer with dream_answer(id, answer). Then ask if he "
            "wants to do the examen.")
    return s


def dream_line(hub) -> str | None:
    """One line from last night's dream (state/dream.json, written by the Claude dream run).
    Returned once, then marked told so Pakito doesn't repeat it every conversation."""
    p = hub.dir / "state" / "dream.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if d.get("told") or not (d.get("line") or "").strip():
        return None
    d["told"] = True
    try:
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return d["line"].strip()


def _dq_path(hub):
    return hub.dir / "state" / "dream_questions.json"


def _dq_load(hub) -> list[dict]:
    try:
        qs = json.loads(_dq_path(hub).read_text(encoding="utf-8"))
        return [q for q in qs if isinstance(q, dict)] if isinstance(qs, list) else []
    except Exception:
        return []


def _dq_save(hub, qs: list[dict]) -> None:
    try:
        _dq_path(hub).write_text(json.dumps(qs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _dq_open(q: dict) -> bool:
    """Still waiting for his answer: the dream hasn't filed one ("answered") and Pakito hasn't heard one."""
    return bool((q.get("q") or "").strip()) and not q.get("answered") and not q.get("pakito_heard")


def open_dream_questions(hub) -> list[dict]:
    return [{"id": q.get("id"), "q": q["q"].strip(), "why": q.get("why", "")} for q in _dq_load(hub) if _dq_open(q)]


def offer_dream_question(hub, cooldown_h: float = 4) -> dict | None:
    """The one question this conversation may ask: the least recently offered open one, at most once every
    cooldown_h hours, so quick commands don't turn into an interview."""
    qs = _dq_load(hub)
    now = hub.clock()
    live = [q for q in qs if _dq_open(q)]
    if not live:
        return None
    last = max((q.get("offered_ts", 0) for q in live), default=0)
    if now - last < cooldown_h * 3600:
        return None
    q = min(live, key=lambda q: q.get("offered_ts", 0))
    q["offered_ts"] = now
    _dq_save(hub, qs)
    return {"id": q.get("id"), "q": q["q"].strip()}


def answer_dream_question(hub, qid: str | None, answer: str) -> dict:
    """Save his answer (his words) where the dream reads it; the dream files it and marks it answered."""
    qs = _dq_load(hub)
    live = [q for q in qs if _dq_open(q)]
    q = next((q for q in live if qid and str(q.get("id")) == str(qid)), None)
    if q is None and qid:
        ref = str(qid).lower()
        q = next((q for q in live if ref in q["q"].lower()), None)
    if q is None and len(live) == 1:
        q = live[0]
    if q is None:
        return {"error": "Which question? Call dream_questions for the ids.", "open": open_dream_questions(hub)}
    if not (answer or "").strip():
        return {"error": "Pass his answer, in his words."}
    stamp = datetime.fromtimestamp(hub.clock()).isoformat(timespec="seconds")
    q.setdefault("asked_at", stamp)
    q["pakito_heard"] = answer.strip()
    q["pakito_heard_at"] = stamp
    _dq_save(hub, qs)
    return {"saved": q.get("id"), "left": len(live) - 1}


def take_dream_questions(hub, n: int = 2) -> list[str]:
    """For the debrief: up to n open questions (not answered by the dream, not already answered to Pakito)."""
    qs = _dq_load(hub)
    out = []
    stamp = datetime.fromtimestamp(hub.clock()).isoformat(timespec="seconds")
    for q in qs:
        if _dq_open(q) and len(out) < n:
            q.setdefault("asked_at", stamp)
            q["offered_ts"] = hub.clock()
            out.append(f"[{q.get('id')}] {q['q'].strip()}")
    if out:
        _dq_save(hub, qs)
    return out
