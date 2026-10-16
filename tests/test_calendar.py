from datetime import datetime

import pytest

from assistant.calendar_feed import Calendar, is_big, parse_ics
from test_assistant import env  # noqa: F401  (shared fixture)

MAIN = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:ex1
DTSTART;VALUE=DATE:20261015
DTEND;VALUE=DATE:20261016
SUMMARY:🔴 CSE 355 Exam 2
DESCRIPTION:In person\\, in class. CFGs/CFLs\\, PDAs.
END:VEVENT
BEGIN:VEVENT
UID:sb1
DTSTART:20261013T203000Z
DTEND:20261013T233000Z
SUMMARY:📚 CSE 355 Exam 2 final push
END:VEVENT
BEGIN:VEVENT
UID:ex2
DTSTART;VALUE=DATE:20261019
DTEND;VALUE=DATE:20261020
SUMMARY:🔴 PHY 310 Exam 2 (Ch 4–5) — moved\\, confirm date
END:VEVENT
BEGIN:VEVENT
UID:gone
DTSTART:20261014T170000Z
SUMMARY:cancelled thing
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""

CLASSES = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:phy302
DTSTART;TZID=America/Phoenix:20260825T133000
DTEND;TZID=America/Phoenix:20260825T153500
RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261204T235959Z
EXDATE;TZID=America/Phoenix:20261013T133000
SUMMARY:PHY 302 Math Methods
LOCATION:PSH-356
END:VEVENT
BEGIN:VEVENT
UID:phy302
RECURRENCE-ID;TZID=America/Phoenix:20261015T133000
DTSTART;TZID=America/Phoenix:20261015T150000
DTEND;TZID=America/Phoenix:20261015T170000
SUMMARY:PHY 302 Math Methods (moved)
END:VEVENT
END:VCALENDAR
"""

CANVAS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:c1
DTSTART;VALUE=DATE:20261016
SUMMARY:Curvilinear Coordinates Project [PHY 302]
END:VEVENT
BEGIN:VEVENT
UID:c2
DTSTART;VALUE=DATE:20261014
SUMMARY:Agenda #15 HW [PHY 302]
END:VEVENT
BEGIN:VEVENT
UID:c3
DTSTART;VALUE=DATE:20261014
SUMMARY:Quiz 5 Ch 5 [PHY 310]
END:VEVENT
END:VCALENDAR
"""


def ts(s):
    return datetime.fromisoformat(s).timestamp()


@pytest.fixture
def cal(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/Phoenix")
    import time
    time.tzset()
    feeds = [{"label": "main", "role": "main", "url": "u1"}, {"label": "classes", "role": "classes", "url": "u2"},
             {"label": "Canvas", "role": "canvas", "url": "u3"}]
    data = {"u1": MAIN, "u2": CLASSES, "u3": CANVAS}
    now = {"t": ts("2026-10-13T10:51:00")}
    c = Calendar(feeds, tmp_path / "cache.json", fetch=data.__getitem__, clock=lambda: now["t"])
    c.refresh()
    c.now_box = now
    return c


def test_parse_handles_dates_utc_tz_escapes_and_cancelled(cal):
    evs = parse_ics(MAIN)
    assert [e["summary"] for e in evs] == ["🔴 CSE 355 Exam 2", "📚 CSE 355 Exam 2 final push",
                                          "🔴 PHY 310 Exam 2 (Ch 4–5) — moved, confirm date"]
    assert evs[0]["all_day"] and evs[0]["description"].startswith("In person, in class")
    assert evs[1]["start"] == datetime(2026, 10, 13, 13, 30)          # 20:30Z = 1:30 pm Phoenix


def test_weekly_classes_expand_with_skips_and_moves(cal):
    week = cal.between(datetime(2026, 10, 12), datetime(2026, 10, 19), ("classes",))
    assert [(e["start"], e["summary"]) for e in week] == [
        (datetime(2026, 10, 15, 15, 0), "PHY 302 Math Methods (moved)")]   # Tue skipped (fall break), Thu moved
    later = cal.between(datetime(2026, 10, 19), datetime(2026, 10, 26), ("classes",))
    assert [e["start"].day for e in later] == [20, 22]


def test_coming_up_is_short_and_skips_homework(cal):
    lines = cal.coming_up(10)
    assert lines[0] == "today 1:30pm: 📚 CSE 355 Exam 2 final push"
    assert any("Thu Oct 15: 🔴 CSE 355 Exam 2" in l for l in lines)
    assert any("Curvilinear Coordinates Project" in l for l in lines)      # projects come from Canvas
    assert not any("HW" in l or "Quiz" in l for l in lines)               # he checks those on Canvas himself
    assert len(lines) <= 9


def test_big_detection():
    assert is_big({"summary": "MAT 343 Test 2"}) and is_big({"summary": "🔴 anything"})
    assert not is_big({"summary": "Quiz 5 Ch 5"}) and not is_big({"summary": "Finalize slides"})
    assert not is_big({"summary": "📚 CSE 355 Exam 2 final push"})


def test_cache_survives_offline(cal, tmp_path):
    def down(url):
        raise OSError("offline")
    c2 = Calendar(cal.feeds, tmp_path / "cache.json", fetch=down, clock=cal.clock)
    assert any("CSE 355 Exam 2" in l for l in c2.coming_up(10))          # from the cache
    c2.refresh()
    assert c2.error and any("CSE 355 Exam 2" in l for l in c2.coming_up(10))   # a failed refresh keeps old data


def test_hub_tool_and_prompt(env, cal):
    from assistant.voice import VOICE_DEFAULTS, system_prompt
    hub = env[0]
    hub.calendar = cal
    r = hub.call("whats_coming", {"when": "today"})
    assert r["events"] == ["today 1:30pm: 📚 CSE 355 Exam 2 final push"]
    r = hub.call("whats_coming", {"days": 7, "big": True})
    assert [e.split(": ", 1)[1] for e in r["events"]] == [
        "🔴 CSE 355 Exam 2", "Curvilinear Coordinates Project [PHY 302]", "🔴 PHY 310 Exam 2 (Ch 4–5) — moved, confirm date"]
    r = hub.call("whats_coming", {"days": 2, "assignments": True})
    assert any("Agenda #15 HW" in e for e in r["events"])                  # only when he asks
    p = system_prompt(hub, VOICE_DEFAULTS)
    assert "Coming up (his calendar):" in p and "🔴 CSE 355 Exam 2" in p and "HW" not in p.split("Coming up")[1]


def test_not_connected(env):
    hub = env[0]
    assert "run-pakito.bat calendar" in hub.call("whats_coming", {})["error"]
