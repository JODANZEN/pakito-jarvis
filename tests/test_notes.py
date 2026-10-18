from datetime import datetime
from pathlib import Path

from assistant.notes import parse_when, safe_title
from test_assistant import env  # noqa: F401  (shared fixture)


def test_take_note_goes_to_the_vault_inbox_in_his_words(env):
    hub, clock, *_ = env
    r = hub.call("take_note", {"text": "Idea: un canal estilo Santaolalla pero con física computacional",
                               "title": "Canal de física", "kind": "idea"})
    day = datetime.fromtimestamp(clock()).strftime("%Y-%m-%d")
    f = Path(hub.cfg["vault_path"]) / "inbox" / f"{day} Canal de física.md"
    assert r["noted"] == f.name and f.exists()
    text = f.read_text(encoding="utf-8")
    assert "un canal estilo Santaolalla pero con física computacional" in text and "💡 idea" in text
    assert "[[Home]]" in text                                       # vault rule: link up
    hub.call("take_note", {"text": "y que tenga simulaciones en vivo", "title": "Canal de física"})
    assert "simulaciones en vivo" in f.read_text(encoding="utf-8")   # same topic, same day: same file
    assert len(list(f.parent.iterdir())) == 1


def test_titles_are_safe_filenames():
    assert safe_title('PHY 310: "Lagrangian"?/notes') == "PHY 310 Lagrangian notes"
    assert safe_title("") == "nota"


def test_remember_with_a_date_fires_and_comes_up_until_done(env):
    hub, clock, _, notes = env
    now = datetime.fromtimestamp(clock())
    due = now.replace(second=0, microsecond=0)
    due = due.replace(hour=(due.hour + 2) % 24) if due.hour < 22 else due
    r = hub.call("remember", {"what": "Llamar a mamá", "due": due.strftime("%Y-%m-%dT%H:%M")})
    assert r["kind"] == "due"
    assert hub.reminders.pending() == ([], [])                      # not yet
    clock.advance(3 * 60)
    fired = hub.reminders.check()
    assert fired and notes[-1] == ("⏰ Recordatorio", "Llamar a mamá")
    assert hub.pet.snapshot()["state"] == "happy" and "Llamar a mamá" in hub.pet.snapshot()["text"]
    due_now, _ = hub.reminders.pending()
    assert [x["what"] for x in due_now] == ["Llamar a mamá"]
    from assistant.voice import VOICE_DEFAULTS, system_prompt
    assert "REMINDERS that are due" in system_prompt(hub, VOICE_DEFAULTS)
    hub.call("reminder_done", {"item": "mamá"})
    assert hub.reminders.pending() == ([], [])
    md = (hub.dir / "Reminders.md").read_text(encoding="utf-8")
    assert "- [x] ⏰" in md and "Llamar a mamá" in md


def test_remember_without_date_asks_later_and_saves_the_answer(env):
    hub, clock, *_ = env
    r = hub.call("remember", {"what": "Le presté 20 dólares a Mario"})
    assert r["kind"] == "ask"
    clock.advance(60 * 24 * 2 + 1)
    _, ask = hub.reminders.pending()
    assert [x["what"] for x in ask] == ["Le presté 20 dólares a Mario"]
    from assistant.voice import VOICE_DEFAULTS, system_prompt
    assert "Ask about ONE per conversation" in system_prompt(hub, VOICE_DEFAULTS)
    r = hub.call("reminder_done", {"item": "Mario", "answer": "ya me pagó"})
    assert r["answer"] == "ya me pagó"
    assert "→ ya me pagó" in (hub.dir / "Reminders.md").read_text(encoding="utf-8")


def test_snooze_and_bad_dates(env):
    hub, clock, *_ = env
    hub.call("remember", {"what": "Aplicar a pasantías", "ask_in_days": 0.01})
    clock.advance(60)
    assert hub.reminders.pending()[1]
    hub.call("snooze_reminder", {"item": "pasantías", "days": 1})
    assert hub.reminders.pending() == ([], [])
    assert "Couldn't read" in hub.call("remember", {"what": "x", "due": "el martes"})["error"]
    assert "already passed" in hub.call("remember", {"what": "x", "due": "2020-01-01T09:00"})["error"]
    assert parse_when("2026-10-20", datetime(2026, 10, 16)) == datetime(2026, 10, 20, 9, 0)


def test_reminders_survive_restart(env):
    hub, clock, *_ = env
    hub.call("remember", {"what": "Revisar el portafolio", "ask_in_days": 3})
    from assistant.notes import Reminders
    again = Reminders(hub.dir / "state" / "reminders.json", clock=clock)
    assert again.upcoming()[0]["what"] == "Revisar el portafolio"
