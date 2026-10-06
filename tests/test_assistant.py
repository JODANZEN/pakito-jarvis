import copy

import pytest

from assistant.config import DEFAULTS
from assistant.hub import Hub
from assistant.routines import parse


class Clock:
    def __init__(self):
        self.t = 1_760_000_000.0

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * 60


class FakeTimer:
    """Timers that fire only when the test says so."""
    live = []

    def __init__(self, seconds, fn):
        self.seconds, self.fn, self.cancelled, self.daemon = seconds, fn, False, True

    def start(self):
        FakeTimer.live.append(self)

    def cancel(self):
        self.cancelled = True

    @classmethod
    def fire_last(cls):
        t = [x for x in cls.live if not x.cancelled][-1]
        t.cancelled = True
        t.fn()
        return t


@pytest.fixture
def env(tmp_path):
    FakeTimer.live = []
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    clock, launched, notes = Clock(), [], []
    hub = Hub(cfg, launcher=launched.append, notifier=lambda t, m: notes.append((t, m)),
              clock=clock, timer_factory=FakeTimer)
    return hub, clock, launched, notes


# ---------------- focus trainer ----------------

def test_completed_block_levels_up_and_schedules_break(env):
    hub, clock, _, notes = env
    assert hub.focus.level == 20
    hub.call("start_focus", {"label": "CSE 355"})
    assert FakeTimer.live[-1].seconds == 20 * 60
    clock.advance(20)
    FakeTimer.fire_last()
    assert hub.focus.level == 22
    assert hub.focus.phase == "break"
    assert FakeTimer.live[-1].seconds == 5 * 60          # 20 * 0.2 = 4 -> min 5
    rec = hub.focus.log.read()[-1]
    assert rec["completed"] and rec["actual_min"] == 20 and rec["label"] == "CSE 355"
    clock.advance(5)
    FakeTimer.fire_last()
    assert hub.focus.phase == "idle"
    assert notes[-1][0] == "Break over"


def test_too_many_distractions_blocks_levelup(env):
    hub, clock, *_ = env
    hub.call("start_focus")
    for _ in range(3):
        clock.advance(2)
        hub.call("log_distraction", {"note": "phone"})
    clock.advance(14)
    FakeTimer.fire_last()
    assert hub.focus.level == 20
    assert hub.focus.log.read()[-1]["distractions"] == 3


def test_early_bail_levels_down_but_late_bail_does_not(env):
    hub, clock, *_ = env
    hub.call("start_focus")
    clock.advance(5)                       # < half of 20
    hub.call("stop_focus")
    assert hub.focus.level == 18
    hub.call("start_focus")
    clock.advance(15)                      # > half of 18
    hub.call("stop_focus")
    assert hub.focus.level == 18
    assert [b["completed"] for b in hub.focus.log.read()] == [False, False]


def test_custom_minutes_dont_move_level(env):
    hub, clock, *_ = env
    hub.call("start_focus", {"minutes": 45, "label": "PHY 302"})
    clock.advance(45)
    FakeTimer.fire_last()
    assert hub.focus.level == 20
    assert FakeTimer.live[-1].seconds == 9 * 60          # 45 * 0.2


def test_long_break_every_fourth(env):
    hub, clock, *_ = env
    for i in range(4):
        hub.call("start_focus")
        clock.advance(hub.focus.level)
        FakeTimer.fire_last()                # focus done -> break timer
        brk = FakeTimer.live[-1].seconds
        hub.call("stop_focus")               # skip the break
    assert brk == 15 * 60
    assert hub.focus.level == 28


def test_level_persists_across_restart(env, tmp_path):
    hub, clock, *_ = env
    hub.call("start_focus")
    clock.advance(20)
    FakeTimer.fire_last()
    hub2 = Hub(hub.cfg, launcher=lambda x: None, notifier=lambda *a: None, clock=clock, timer_factory=FakeTimer)
    assert hub2.focus.level == 22


def test_cant_double_start_and_stats(env):
    hub, clock, *_ = env
    hub.call("start_focus")
    assert "error" in hub.call("start_focus")
    clock.advance(20)
    FakeTimer.fire_last()
    s = hub.call("focus_stats")
    assert s["blocks"] == 1 and s["completed"] == 1 and s["focus_minutes"] == 20


# ---------------- routines ----------------

def test_seed_routines_and_matching(env):
    hub, *_ = env
    names = [r["name"] for r in hub.routines.all()]
    assert names == ["work mode", "lock in", "homework", "phy 310", "physics reading"]
    m = hub.routines.match
    assert m("lock in")["name"] == "lock in"
    assert m("let's study")["name"] == "lock in"
    assert m("research")["name"] == "work mode"
    assert m("homework time")["name"] == "homework"
    assert m("PHY 302")["name"] == "physics reading"
    assert m("PHY 310")["name"] == "phy 310"
    assert m("classical mechanics")["name"] == "phy 310"
    assert m("let's do physics reading")["name"] == "physics reading"
    assert m("order a pizza") is None


def test_request_runs_routine(env):
    hub, clock, launched, _ = env
    out = hub.request("homework")
    assert out["routine"] == "homework"
    assert launched == ["https://canvas.asu.edu", "https://chatgpt.com", "spotify:search:chill%20study"]
    tools = [e["tool"] for e in hub.events.read()]
    assert tools[-1] == "run_routine"   # parent logs after its children
    assert {e["source"] for e in hub.events.read()[:-1]} == {"routine"}


def test_lock_in_routine_starts_locked_block(env):
    hub, clock, launched, _ = env
    hub.request("lock in")
    assert hub.focus.phase == "focus" and hub.focus.locked
    assert hub.focus.block["label"] == "study"
    clock.advance(20)
    FakeTimer.fire_last()                         # block done -> break -> not locked
    assert hub.focus.phase == "break" and not hub.focus.locked
    assert hub.focus.log.read()[-1]["lockin"] is True


def test_blocker_kills_only_while_locked(env):
    from assistant.blocker import Blocker
    hub, clock, _, notes = env
    running = {"solitaire.exe", "code.exe"}
    killed = []
    b = Blocker(hub.focus, ["Solitaire.exe"], hub.notify, lister=lambda: running, killer=killed.append)
    assert b.check_once() == []                   # idle: nothing blocked
    hub.call("start_focus", {"label": "x"})       # normal block: still nothing
    assert b.check_once() == []
    hub.call("stop_focus")
    hub.focus.async_listeners = False
    clock.advance(3)
    hub.call("start_focus", {"label": "x", "lockin": True})     # lock-in start checks right away
    assert killed == ["solitaire.exe"]
    assert notes[-1][0].startswith("Locked in") and notes[-1][1] == "Solitaire is blocked. Break in 18 min."
    assert hub.focus.status()["distractions"] == 1   # counts as a distraction


def test_parse_ignores_junk():
    md = "# x\nsome text\n## a\n- open: http://x\n- bogus: y\nnote\n## empty\n"
    r = parse(md)
    assert len(r) == 1 and r[0]["actions"] == [{"type": "open", "value": "http://x"}]


# ---------------- learner ----------------

def _do_cse_301(hub, clock, phrasing):
    hub.request(phrasing)
    hub.call("open_url", {"url": "https://canvas.asu.edu/courses/1"})
    hub.call("play_music", {"query": "jazz"})
    clock.advance(1)
    return hub.end_request()


def test_learns_routine_after_three_repeats(env):
    hub, clock, launched, notes = env
    assert _do_cse_301(hub, clock, "ethics post") is None
    assert _do_cse_301(hub, clock, "cse 301 discussion") is None
    s = _do_cse_301(hub, clock, "ethics post")
    assert s and s["name"] == "ethics post" and s["count"] == 3
    assert notes[-1][0] == "Save as routine?"
    sug = hub.call("routine_suggestions")["suggestions"]
    assert len(sug) == 1 and "cse 301 discussion" in sug[0]["aliases"]
    saved = hub.call("save_routine", {"index": 1, "name": "cse 301"})
    assert saved["saved"]["name"] == "cse 301"
    assert hub.call("routine_suggestions")["suggestions"] == []
    # and now saying it runs it (after the 30-min don't-reopen window)
    clock.advance(31)
    launched.clear()
    assert hub.request("ethics post")["routine"] == "cse 301"
    assert "spotify:search:jazz" in launched


def test_actions_outside_window_dont_count(env):
    hub, clock, *_ = env
    hub.request("something")
    clock.advance(10)                       # > 180 s window
    hub.call("open_url", {"url": "x.com"})
    assert hub.learner.current is None
    assert hub.learner.log.read() == []


def test_dismiss_persists(env):
    hub, clock, *_ = env
    for _ in range(3):
        _do_cse_301(hub, clock, "ethics post")
    hub.call("dismiss_suggestion", {"index": 1})
    hub2 = Hub(hub.cfg, launcher=lambda x: None, notifier=lambda *a: None, clock=clock, timer_factory=FakeTimer)
    assert hub2.call("routine_suggestions")["suggestions"] == []


# ---------------- hub ----------------

def test_declarations_are_gemini_shaped(env):
    hub, *_ = env
    decl = {d["name"]: d for d in hub.declarations()}
    assert {"start_focus", "open_url", "run_routine", "save_routine"} <= decl.keys()
    for d in decl.values():
        assert d["parameters"]["type"] == "object" and isinstance(d["description"], str)


def test_broken_tool_is_logged_not_raised(env):
    hub, *_ = env

    @hub.tool("boom", "explodes")
    def boom():
        raise RuntimeError("nope")

    out = hub.call("boom")
    assert "RuntimeError" in out["error"]
    assert hub.events.read()[-1]["ok"] is False


def test_console_smoke(env, capsys):
    from assistant.__main__ import handle
    hub, clock, launched, _ = env
    assert handle(hub, "focus 30 phy 310")
    assert hub.focus.block["planned_min"] == 30
    assert handle(hub, "d phone")
    assert handle(hub, "quit")               # warns because a block is running
    assert handle(hub, "stop")
    assert handle(hub, "routines")
    assert not handle(hub, "quit")
    assert "physics reading" in capsys.readouterr().out


# ---------------- short-term memory ----------------

def test_dedupe_skips_reopen_and_force_overrides(env):
    hub, clock, launched, _ = env
    hub.call("open_url", {"url": "https://canvas.asu.edu/"})
    clock.advance(5)
    r = hub.call("open_url", {"url": "canvas.asu.edu"})          # same site, different spelling
    assert r["skipped"] and launched.count("https://canvas.asu.edu/") == 1 and len(launched) == 1
    hub.call("open_url", {"url": "canvas.asu.edu", "force": True})
    assert len(launched) == 2
    clock.advance(31)
    assert "skipped" not in hub.call("open_url", {"url": "canvas.asu.edu"})


def test_routine_twice_doesnt_double_open(env):
    hub, clock, launched, _ = env
    hub.request("lock in")
    n = len(launched)
    hub.call("stop_focus")
    clock.advance(10)
    hub.request("lock in")
    assert len(launched) == n                                     # tabs + music skipped
    assert hub.focus.phase == "focus"                             # but focus restarted


def test_dedupe_survives_restart(env):
    hub, clock, launched, _ = env
    hub.call("open_app", {"name": "vscode"})
    hub2 = Hub(hub.cfg, launcher=launched.append, notifier=lambda *a: None, clock=clock, timer_factory=FakeTimer)
    assert hub2.call("open_app", {"name": "VSCode"})["skipped"]


def test_recent_lines(env):
    hub, clock, *_ = env
    hub.call("open_url", {"url": "https://canvas.asu.edu"})
    hub.call("play_music", {"query": "lofi"})
    hub.call("start_focus", {"minutes": 25, "label": "CSE 355"})
    hub.call("open_url", {"url": "canvas.asu.edu"})                # skipped -> not listed twice
    lines = hub.recent()
    assert len(lines) == 3
    assert lines[0].endswith("opened canvas.asu.edu") and "25 min CSE 355" in lines[2]
    clock.advance(3 * 60)
    assert hub.recent() == []


# ---------------- Claude bridge ----------------

def test_claude_bridge(env, monkeypatch):
    import json as _json
    from assistant import claude_bridge
    hub, clock, _, notes = env
    calls = []

    def runner(cmd, cwd, timeout):
        calls.append(cmd)
        return 0, _json.dumps({"result": "Short answer here.\n---\n## Details\nmore", "is_error": False}), ""

    monkeypatch.setattr(claude_bridge, "find_claude", lambda p=None: "claude.exe")
    hub.claude.runner, hub.claude.threaded = runner, False
    r = hub.call("ask_claude", {"question": "Explain Kosaraju's algorithm"})
    assert r["asked"]
    cmd = calls[0]
    assert cmd[:2] == ["claude.exe", "-p"] and "--bare" not in cmd       # bare mode would skip the subscription login
    assert "--permission-mode" in cmd and "dontAsk" in cmd
    job = list(hub.claude.jobs.values())[0]
    assert job["spoken"] == "Short answer here." and "Details" in job["detail"]
    assert notes[-1][0] == "Claude answered"                             # nobody listening -> toast
    assert hub.claude.undelivered() == [job]
    md = (hub.dir / "Claude").glob("*.md")
    assert "Kosaraju" in next(md).read_text(encoding="utf-8")
    assert any("asked Claude: Explain Kosaraju" in l for l in hub.recent())


def test_claude_missing_or_failing(env, monkeypatch):
    from assistant import claude_bridge
    hub, *_ = env
    monkeypatch.setattr(claude_bridge, "find_claude", lambda p=None: None)
    assert "install" in hub.call("ask_claude", {"question": "x"})["error"]
    monkeypatch.setattr(claude_bridge, "find_claude", lambda p=None: "claude.exe")
    hub.claude.threaded = False
    hub.claude.runner = lambda c, w, t: (1, '{"result": "Not logged in", "is_error": true}', "")
    hub.call("ask_claude", {"question": "x"})
    job = list(hub.claude.jobs.values())[-1]
    assert job["status"] == "error" and "Not logged in" in job["answer"]


# ---------------- browser ----------------

def test_find_browser_and_routing(tmp_path, monkeypatch):
    from assistant import util
    exe = tmp_path / "Programs" / "Opera GX" / "opera.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert util.find_browser("auto") == (str(exe), "Opera GX")
    assert util.find_browser("default") == (None, "default")
    popped, fallback = [], []
    monkeypatch.setattr(util.subprocess, "Popen", lambda args, **k: popped.append(args))
    monkeypatch.setattr(util, "launch", fallback.append)
    go = util.make_launcher(str(exe))
    go("https://canvas.asu.edu")
    go("spotify:search:lofi")
    assert popped == [[str(exe), "https://canvas.asu.edu"]] and fallback == ["spotify:search:lofi"]
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nothing"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "nothing"))
    assert util.find_browser("auto") == (None, "default")


def test_spanish_shortcuts_flights_and_default_music(env):
    hub, clock, launched, _ = env
    hub.call("open_app", {"name": "chat"})
    assert launched[-1] == "https://chatgpt.com"
    hub.call("play_music", {})
    assert launched[-1] == "spotify:search:lofi%20beats"
    r = hub.call("search_flights", {"to": "Bogotá", "dates": "Dec 15 to Jan 10"})
    assert r["query"] == "flights from PHX to Bogotá Dec 15 to Jan 10"
    assert launched[-1].startswith("https://www.google.com/travel/flights?q=flights+from+PHX+to+Bogot")
    assert hub.call("search_flights", {"to": "bogotá"})["skipped"]       # don't reopen the same search
    assert any("flights to Bogotá" in l for l in hub.recent())


# ---------------- textbook ----------------

def test_open_book_with_page_and_routine(env):
    hub, clock, launched, _ = env
    r = hub.call("open_book", {"book": "Taylor", "page": 161})
    assert r["page"] == 161 and launched[-1].startswith("file:") and launched[-1].endswith("free.pdf#page=161")
    assert "pdfcoffee.com_john-taylor" in launched[-1]
    assert "error" in hub.call("open_book", {"book": "griffiths"})
    clock.advance(31)
    launched.clear()
    hub.request("mechanics")                                   # phy 310 routine
    assert launched and launched[0].endswith(".pdf") and hub.focus.block["label"] == "PHY 310"
    assert any("opened book" in l for l in hub.recent())


def test_open_url_local_path(env):
    hub, _, launched, _ = env
    hub.call("open_url", {"url": r"C:\Users\x\notes.pdf"})
    assert launched[-1].startswith("file:")
    hub.call("open_url", {"url": "youtube.com"})
    assert launched[-1] == "https://youtube.com"


def test_launch_strips_page_for_default_app(monkeypatch):
    from assistant import util
    opened = []
    monkeypatch.setattr(util.sys, "platform", "win32")
    monkeypatch.setattr(util.os, "startfile", opened.append, raising=False)
    util.launch("file:///C:/x/book.pdf#page=5")
    assert opened == ["file:///C:/x/book.pdf"]


# ---------------- Spotify ----------------

class FakeSp:
    def __init__(self, premium=True, devices=True):
        self.premium, self.has_devices, self.calls = premium, devices, []

    def current_user_playlists(self, limit=50):
        return {"items": [{"name": "Gym 🔥", "uri": "spotify:playlist:gym"}, {"name": "Deep Work", "uri": "spotify:playlist:dw"}]}

    def search(self, q, limit=10, type="track"):
        assert limit <= 10                                      # Feb 2026 limit
        item = {"name": q.title(), "uri": f"spotify:{type}:{q.replace(' ', '')}", "artists": [{"name": "Artist"}]}
        return {f"{type}s": {"items": [item]}}

    def devices(self):
        return {"devices": [{"id": "pc", "type": "Computer", "is_active": False}] if self.has_devices else []}

    def start_playback(self, device_id=None, context_uri=None, uris=None):
        if not self.premium:
            raise RuntimeError("403 PREMIUM_REQUIRED")
        self.calls.append(("play", device_id, context_uri, uris))

    def pause_playback(self, device_id=None):
        self.calls.append(("pause", device_id))

    def next_track(self, device_id=None):
        self.calls.append(("next", device_id))

    def previous_track(self, device_id=None):
        self.calls.append(("prev", device_id))

    def volume(self, v, device_id=None):
        self.calls.append(("vol", v))

    def current_playback(self):
        return {"is_playing": True, "device": {"volume_percent": 40},
                "item": {"name": "Song", "artists": [{"name": "Bad Bunny"}]}}


def spotify_hub(env, **kw):
    from assistant.spotify import SpotifyControl
    hub, clock, launched, _ = env
    sp = FakeSp(**kw)
    hub.spotify = SpotifyControl(sp, hub.launch, sleep=lambda s: None)
    return hub, sp, launched


def test_spotify_plays_own_playlist_first(env):
    hub, sp, _ = spotify_hub(env)
    r = hub.call("play_music", {"query": "gym"})
    assert r["playing"]["uri"] == "spotify:playlist:gym" and r["playing"]["yours"]
    assert sp.calls[-1] == ("play", "pc", "spotify:playlist:gym", None)
    r = hub.call("play_music", {"query": "despacito", "kind": "song"})
    assert sp.calls[-1][3] == ["spotify:track:despacito"]       # tracks play as uris, not context


def test_spotify_controls(env):
    hub, sp, _ = spotify_hub(env)
    hub.call("music_control", {"action": "pause"})
    hub.call("music_control", {"action": "next"})
    assert hub.call("music_control", {"action": "volume_up"})["volume"] == 55
    assert hub.call("music_control", {"action": "volume", "level": 150})["volume"] == 100
    assert hub.call("music_control", {"action": "now_playing"})["by"] == "Bad Bunny"
    assert [c[0] for c in sp.calls] == ["pause", "next", "vol", "vol"]


def test_spotify_without_premium_opens_exact_item(env):
    hub, sp, launched = spotify_hub(env, premium=False)
    r = hub.call("play_music", {"query": "lofi beats"})
    assert "opened" in r and launched[-1] == "spotify:playlist:lofibeats"


def test_spotify_no_device_opens_app_then_gives_up(env):
    hub, sp, launched = spotify_hub(env, devices=False)
    r = hub.call("play_music", {"query": "lofi beats"})
    assert "spotify:" in launched and "opened" in r


def test_music_control_without_setup(env):
    hub, *_ = env
    assert "run-pakito.bat spotify" in hub.call("music_control", {"action": "pause"})["error"]


# ---------------- focus <-> music ----------------

def test_music_pauses_on_break_and_resumes(env):
    hub, clock, *_ = env
    hub, sp, _ = spotify_hub(env)
    hub.focus.async_listeners = False
    hub.call("start_focus", {})
    clock.advance(20)
    FakeTimer.fire_last()                                   # -> break
    assert sp.calls[-1][0] == "pause" and hub._music_paused_by_us
    hub.call("stop_focus")                                  # skip break
    hub.call("start_focus", {})
    assert sp.calls[-1][0] == "play" and not hub._music_paused_by_us


def test_music_not_resumed_if_we_didnt_pause_it(env):
    hub, clock, *_ = env
    hub, sp, _ = spotify_hub(env)
    hub.focus.async_listeners = False
    hub.call("start_focus", {})
    assert sp.calls == []


# ---------------- debrief ----------------

def test_day_summary_and_end_day(env):
    hub, clock, _, _ = env
    hub.call("start_focus", {"label": "PHY 310"})
    clock.advance(5)
    hub.call("log_distraction", {"note": "phone"})
    clock.advance(15)
    FakeTimer.fire_last()
    hub.call("stop_focus")
    hub.call("start_focus", {"label": "CSE 355", "lockin": True})
    clock.advance(3)
    hub.focus.distracted("opened youtube.com (blocked)")
    s = hub.call("day_summary")
    assert s["blocks"] == 1 and s["completed"] == 1 and s["focus_minutes"] == 20
    r = hub.call("end_day")                                 # stops the running lock-in block
    assert hub.focus.phase == "idle" and r["stopped_block"]
    assert r["blocks"] == 2 and r["distractions"] == 2 and r["blocked_attempts"] == 1
    assert "2 blocks, 23 min focused, 1 finished." in r["spoken"]
    note = (hub.dir / r["note"]).read_text(encoding="utf-8")
    assert "PHY 310 ×1" in note and "CSE 355 ×1" in note


def test_day_summary_empty(env):
    hub, *_ = env
    assert hub.call("day_summary")["spoken"] == "No focus blocks today."


# ---------------- lock-in site blocking server ----------------

def test_status_server(env):
    import json as _json
    import urllib.request
    from assistant import server
    hub, clock, _, notes = env
    srv = server.start(hub, port=0)
    port = srv.server_address[1]
    get = lambda: _json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/state").read())

    def post(site):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/blocked", data=_json.dumps({"site": site}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return _json.loads(urllib.request.urlopen(req).read())
    try:
        assert get()["locked"] is False and "instagram.com" in get()["sites"]
        assert "youtube.com" not in get()["sites"]            # he uses YouTube for podcasts/learning
        assert post("instagram.com")["ok"] is False           # not locked: nothing counted
        hub.call("start_focus", {"label": "study", "lockin": True})
        st = get()
        assert st["locked"] and st["label"] == "study" and st["remaining_min"] == 20
        assert post("instagram.com")["counted"] is True
        assert post("instagram.com")["counted"] is False      # duplicate report within 20 s
        assert hub.focus.status()["distractions"] == 1
        assert notes[-1] == ("Locked in 🔒", "instagram.com is blocked. Break in 20 min.")
    finally:
        srv.shutdown()


# ---------------- Phone Link mute ----------------

def test_phone_link_muted_during_lockin_and_back_on_break(env):
    from assistant.blocker import Blocker
    hub, clock, _, notes = env
    hub.focus.async_listeners = False
    running, killed, relaunched = {"phoneexperiencehost.exe"}, [], []
    b = Blocker(hub.focus, [], hub.notify, lister=lambda: running, killer=killed.append,
                mute=["PhoneExperienceHost.exe"], relaunch=lambda: relaunched.append(1))
    hub.call("start_focus", {"label": "x"})                  # normal block: phone stays
    assert killed == []
    hub.call("stop_focus")
    n_notes = len(notes)
    hub.call("start_focus", {"label": "x", "lockin": True})
    assert killed == ["phoneexperiencehost.exe"]
    assert hub.focus.status()["distractions"] == 0           # muting isn't a distraction
    assert not any(t.startswith("Locked in") for t, _ in notes[n_notes:])
    clock.advance(20)
    FakeTimer.fire_last()                                     # break -> Phone Link comes back
    assert relaunched == [1]


# ---------------- podcasts ----------------

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <entry><id>yt:video:AAA</id><yt:videoId>AAA</yt:videoId><title>Why Men Feel Lost - Dr X</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=AAA"/><published>2026-10-04T12:00:00+00:00</published></entry>
 <entry><id>yt:video:SSS</id><yt:videoId>SSS</yt:videoId><title>Quick tip #shorts</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/SSS"/><published>2026-10-05T12:00:00+00:00</published></entry>
 <entry><id>yt:video:BBB</id><yt:videoId>BBB</yt:videoId><title>The Science Of Sleep - Prof Y</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=BBB"/><published>2026-10-01T12:00:00+00:00</published></entry>
</feed>"""

ZZEN = FEED.replace("AAA", "ZZ1").replace("BBB", "ZZ2").replace("Why Men Feel Lost - Dr X", "Nootrópicos y enfoque") \
    .replace("The Science Of Sleep - Prof Y", "Dopamina").replace("2026-10-04", "2026-10-03").replace("2026-10-01", "2026-10-02")


def podcast_hub(env):
    hub, clock, launched, _ = env
    clock.t = 1791300000.0                                    # 2026-10-06
    hub.podcasts.fetcher = lambda feed: ZZEN if "playlist_id" in feed else FEED
    hub.podcasts.clock = clock
    return hub, clock, launched


def test_podcast_options_mix_shows_skip_shorts(env):
    hub, clock, launched = podcast_hub(env)
    r = hub.call("podcast_options")
    titles = [o["title"] for o in r["options"]]
    assert titles == ["Why Men Feel Lost - Dr X", "Nootrópicos y enfoque", "Dopamina"]  # newest of each show first
    assert "#shorts" not in " ".join(titles)
    assert r["options"][0]["when"] == "2 days ago"   # Oct 4 noon -> Oct 6 evening
    assert hub.call("podcast_options", {"creator": "zzen"})["options"][0]["show"] == "ZZEN Talks"


def test_play_podcast_by_number_or_words_and_not_offered_again(env):
    hub, clock, launched = podcast_hub(env)
    hub.call("podcast_options")
    r = hub.call("play_podcast", {"choice": "2"})
    assert r["title"] == "Nootrópicos y enfoque" and launched[-1] == "https://www.youtube.com/watch?v=ZZ1"
    hub.call("podcast_options")
    r = hub.call("play_podcast", {"choice": "the sleep one"})
    assert r["title"] == "The Science Of Sleep - Prof Y"
    hub.call("podcast_options")
    hub.call("podcast_heard", {"choice": "1"})
    left = [o["title"] for o in hub.call("podcast_options")["options"]]
    assert left == ["Dopamina"]
    # remembered across restarts (from the event log)
    hub2 = Hub(hub.cfg, launcher=lambda x: None, notifier=lambda *a: None, clock=clock, timer_factory=FakeTimer)
    assert {"ZZ1", "BBB", "AAA"} <= hub2.podcasts.heard
    assert any("podcast: Nootrópicos" in l for l in hub.recent(hours=24))


def test_podcast_feed_down_is_reported(env):
    hub, clock, launched = podcast_hub(env)
    def boom(feed):
        if "playlist_id" in feed:
            raise OSError("timeout")
        return FEED
    hub.podcasts.fetcher = boom
    r = hub.call("podcast_options")
    assert len(r["options"]) == 2 and "ZZEN Talks" in r["couldnt_check"][0]


def test_podcasts_fall_back_to_api_then_spotify(env):
    from assistant.podcasts import Podcasts
    hub, clock, launched = podcast_hub(env)
    def rss_down(feed):
        raise OSError("HTTP Error 404")
    api_calls, sp_calls = [], []
    def api(show, key):
        api_calls.append(show["name"])
        if show["name"] == "ZZEN Talks":
            raise OSError("quota")
        return [{"id": "API1", "title": "From API", "show": show["name"], "published": "2026-10-05T00:00:00Z",
                 "url": "https://www.youtube.com/watch?v=API1"}]
    def sp_eps(show, sp):
        sp_calls.append(show["name"])
        return [{"id": "sp:E1", "title": "From Spotify", "show": show["name"], "published": "2026-10-04T12:00:00+00:00",
                 "url": "https://www.youtube.com/results?search_query=From+Spotify", "spotify_uri": "spotify:episode:E1"}]
    hub.podcasts = Podcasts(hub.cfg["podcasts"], set(), fetcher=rss_down, clock=clock,
                            api_key="k", spotify=object(), api=api, sp_eps=sp_eps)
    titles = [o["title"] for o in hub.call("podcast_options")["options"]]
    assert titles == ["From API", "From Spotify"]
    assert sp_calls == ["ZZEN Talks"]                     # spotify only where the API failed
    hub.call("play_podcast", {"choice": "2"})
    assert launched[-1].startswith("https://www.youtube.com/results?search_query=From")


def test_podcasts_all_sources_down(env):
    from assistant.podcasts import Podcasts
    hub, clock, _ = podcast_hub(env)
    hub.podcasts = Podcasts(hub.cfg["podcasts"], set(), fetcher=lambda f: (_ for _ in ()).throw(OSError("404")),
                            clock=clock)
    r = hub.call("podcast_options")
    assert r["options"] == [] and len(r["couldnt_check"]) == 2 and "rss" in r["couldnt_check"][0]
