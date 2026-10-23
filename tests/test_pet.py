import json
import time
import urllib.error
import urllib.request

import pytest

from assistant import pet, server
from assistant.pet_state import PetState
from test_assistant import env  # noqa: F401  (shared fixture)


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def ps(tmp_path):
    c = Clock()
    return PetState(tmp_path / "pet.json", c, {"linger_sec": 10}), c


def test_popin_shows_while_talking_then_fades(ps):
    p, c = ps
    assert p.snapshot()["show"] is False                         # pop-in: hidden until you talk
    p.conversation(True)
    p.set("listen", level=0.4)
    s = p.snapshot()
    assert s["show"] and s["state"] == "listen" and s["level"] == 0.4
    p.set("think")
    assert p.snapshot()["state"] == "think"
    p.conversation(False)
    c.t += 5
    assert p.snapshot()["show"]                                  # lingers a bit
    c.t += 6
    assert not p.snapshot()["show"]                              # then gone


def test_stay_and_off_and_calls(ps, tmp_path):
    p, c = ps
    assert p.set_mode("quédate") == "stay"
    c.t += 999
    assert p.snapshot()["show"]                                  # always there
    p.on_call = True
    assert not p.snapshot()["show"]                              # never in a screen share
    p.on_call = False
    assert p.set_mode("desaparece") == "off"
    p.conversation(True)
    assert not p.snapshot()["show"]                              # off means off
    assert p.set_mode("banana") is None
    assert PetState(tmp_path / "pet.json", c).mode == "off"      # remembered across restarts


def test_popin_comes_for_news_and_lockin(ps):
    p, c = ps
    c.t += 100
    p.flash("happy", 8)
    s = p.snapshot()
    assert s["show"] and s["state"] == "happy"
    c.t += 30
    assert not p.snapshot()["show"]
    s = p.snapshot({"phase": "focus", "lockin": True, "remaining_sec": 600, "planned_min": 25, "label": "PHY 310"})
    assert s["show"] and s["state"] == "focus" and s["timer"] == {"left": 600, "total": 1500, "label": "PHY 310"}


def test_transcript_bubble(ps):
    p, _ = ps
    p.conversation(True)
    p.set("listen")
    p.say("you", "pon música ")
    p.say("you", "de estudio")
    assert p.snapshot()["text"] == "pon música de estudio" and p.snapshot()["who"] == "you"
    p.say("pakito", "Listo.")
    assert p.snapshot()["text"] == "Listo." and p.snapshot()["who"] == "pakito"
    p.set("listen")                                              # next turn starts clean
    p.set("think")
    p.set("listen")
    assert p.snapshot()["text"] == ""


def test_hub_feeds_pet_timer_fuel_news_and_mode_tool(env):
    hub, clock, *_ = env
    hub.call("start_focus", {"minutes": 25, "label": "PHY 310"})
    s = hub.pet_snapshot()
    assert s["state"] == "focus" and s["timer"]["left"] == 1500
    clock.advance(10)
    assert hub.pet_snapshot()["timer"]["left"] == 900
    assert hub.call("pet_mode", {"mode": "stay"}) == {"pet": "stay"}
    hub.pet._fuel = (0, 0)
    hub.focus.stop()
    hub._fuel = (0, 0)
    assert hub.pet_snapshot()["fuel_min"] == 10
    hub._pet_news({"status": "done", "kind": "build", "spoken": "Settings page is ready."})
    s = hub.pet_snapshot()
    assert s["state"] == "happy" and "Settings page" in s["text"]


def test_pet_endpoint_needs_token(env):
    hub = env[0]
    srv = server.start(hub, port=0, token="tok")
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/pet", timeout=3)            # transcripts aren't public
        r = urllib.request.urlopen(urllib.request.Request(base + "/pet", headers={"X-Pakito-Token": "tok"}), timeout=3)
        assert json.loads(r.read())["mode"] in ("popin", "stay", "off")
    finally:
        srv.shutdown()


def lit(g, color):
    return {(x, y) for y, row in enumerate(g.px) for x, c in enumerate(row) if c == color}


def test_face_shows_the_real_timer():
    snap = {"state": "focus", "timer": {"left": 18 * 60 + 42, "total": 1800}, "fuel": 0.5}
    g = pet.draw(snap, 0.0)
    ref = pet.Grid()
    ref.text("18:42", 24 - 9, 16, pet.K["gl"])                  # width 17 -> starts at x=15
    assert lit(ref, pet.K["gl"]) <= lit(g, pet.K["gl"])
    g2 = pet.draw(snap, 0.0, timer_left=59)
    ref2 = pet.Grid()
    ref2.text("00:59", 15, 16, pet.K["gl"])
    assert lit(ref2, pet.K["gl"]) <= lit(g2, pet.K["gl"])


def test_every_state_draws_and_listen_follows_the_mic():
    for st in ("idle", "listen", "think", "talk", "focus", "nap", "happy"):
        g = pet.draw({"state": st, "mode": "stay", "time": "11:42", "timer": {"left": 90, "total": 300}}, 1.234)
        assert len(g.px) == pet.H and all(len(r) == pet.W for r in g.px)
    quiet = pet.draw({"state": "listen", "level": 0.0}, 0.0)
    loud = pet.draw({"state": "listen", "level": 0.9}, 0.0)
    mouth = lambda g: sum(1 for y in range(17, 21) for x in range(20, 28) if g.px[y][x] in (pet.K["gl"], pet.K["gls"]))
    assert mouth(loud) > mouth(quiet)


def test_fuel_bar_fills():
    empty = pet.draw({"state": "idle", "fuel": 0}, 0.5)
    full = pet.draw({"state": "idle", "fuel": 1}, 0.5)
    assert len(lit(full, pet.K["gr"])) == 24 and not lit(empty, pet.K["gr"])


def test_frame_is_cheap():
    snap = {"state": "talk", "level": 0.5, "fuel": 0.6, "timer": {"left": 100, "total": 1500}}
    t0 = time.perf_counter()
    for i in range(50):
        pet.to_photo_rows(pet.draw(snap, i * 0.08), 3, "#ff00fe")
    per_frame = (time.perf_counter() - t0) / 50
    assert per_frame < 0.02, per_frame                          # 12 fps needs < 83 ms; aim far below


def test_bubble_text_never_leaves_the_frame():
    """Oct 23: long replies spilled out of the speech bubble (fixed 74 px box, up to 150 chars)."""
    from assistant.pet import fit_lines
    measure = lambda s: 7 * len(s)                             # a 7-px monospace font
    width, lines = 196, 3                                      # 28 characters per line, 3 lines
    long = ("Claro que sí, aquí tienes la hora exacta en Tempe: son las nueve y treinta y cuatro de la mañana "
            "del viernes, y tu siguiente bloque de estudio empieza a las diez")
    out = fit_lines(long, width, lines, measure)
    rows = out.split("\n")
    assert len(rows) == lines and all(measure(r) <= width for r in rows)
    assert rows[0].startswith("…") and out.endswith("a las diez")          # newest words always visible
    assert fit_lines("Listo.", width, lines, measure) == "Listo."
    assert fit_lines("hola\n⚙ play music", width, lines, measure) == "hola\n⚙ play music"
    url = "https://www.youtube.com/results?search_query=" + "x" * 80   # one huge word gets split, not overflowed
    assert all(measure(r) <= width for r in fit_lines(url, width, lines, measure).split("\n"))
