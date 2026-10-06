"""Daily debrief: what you did today, from the logs Pakito already keeps.

`summary()` is read-only ("how did I do today?"). `end_day()` also stops any running block,
pauses music, and writes <vault>/Assistant/Days/<date>.md.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime


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
        parts = [f"{s['blocks']} blocks, {time_txt} focused, {s['completed']} finished."]
        if s["distractions"]:
            extra = f", {s['blocked_attempts']} of them blocked sites or apps" if s["blocked_attempts"] else ""
            parts.append(f"{s['distractions']} distractions{extra}.")
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
    lines += ["", "## Tasks", "- (to-do list not connected yet)"]
    return "\n".join(lines) + "\n"


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
    days = hub.dir / "Days"
    days.mkdir(parents=True, exist_ok=True)
    (days / f"{s['date']}.md").write_text(note_markdown(s), encoding="utf-8")
    s["note"] = f"Days/{s['date']}.md"
    if stopped:
        s["stopped_block"] = True
    return s
