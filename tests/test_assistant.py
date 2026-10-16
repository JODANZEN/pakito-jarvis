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
    tabs = [x for x in launched if not x.startswith("spotify:")]
    assert len(tabs) == n - 1                                     # tabs skipped; music plays again (not deduped)
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
    def __init__(self, premium=True, devices=True, silent=0):
        self.premium, self.has_devices, self.calls = premium, devices, []
        self.silent = silent            # how many play commands get "ok" but play nothing (the real bug)
        self.now, self.playing = "spotify:track:song", True

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
        if self.silent:
            self.silent -= 1
            return
        if context_uri or uris:
            self.now = context_uri or uris[0]
        self.playing = True

    def transfer_playback(self, device_id, force_play=True):
        self.calls.append(("transfer", device_id))

    def pause_playback(self, device_id=None):
        if not self.playing:
            raise RuntimeError("http status: 403 - Player command failed: Restriction violated")
        self.calls.append(("pause", device_id))
        self.playing = False

    def currently_playing(self):
        return None

    def next_track(self, device_id=None):
        self.calls.append(("next", device_id))

    def previous_track(self, device_id=None):
        self.calls.append(("prev", device_id))

    def volume(self, v, device_id=None):
        self.calls.append(("vol", v))

    def current_playback(self):
        return {"is_playing": self.playing, "device": {"volume_percent": 40}, "context": {"uri": self.now},
                "item": {"name": "Song", "uri": self.now, "artists": [{"name": "Bad Bunny"}]}}


def spotify_hub(env, **kw):
    from assistant.spotify import SpotifyControl
    hub, clock, launched, _ = env
    sp = FakeSp(**kw)
    hub.spotify = SpotifyControl(sp, hub.launch, sleep=lambda s: None, hits_path=None)
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
    srv = server.start(hub, port=0, token="t")
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
    hub.podcasts.shows = hub.cfg["podcasts"][:2]              # Modern Wisdom + ZZEN Talks
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
    hub.podcasts = Podcasts(hub.cfg["podcasts"][:2], set(), fetcher=rss_down, clock=clock,
                            api_key="k", spotify=object(), api=api, sp_eps=sp_eps)
    titles = [o["title"] for o in hub.call("podcast_options")["options"]]
    assert titles == ["From API", "From Spotify"]
    assert sp_calls == ["ZZEN Talks"]                     # spotify only where the API failed
    hub.call("play_podcast", {"choice": "2"})
    assert launched[-1].startswith("https://www.youtube.com/results?search_query=From")


def test_podcasts_all_sources_down(env):
    from assistant.podcasts import Podcasts
    hub, clock, _ = podcast_hub(env)
    hub.podcasts = Podcasts(hub.cfg["podcasts"][:2], set(), fetcher=lambda f: (_ for _ in ()).throw(OSError("404")),
                            clock=clock)
    r = hub.call("podcast_options")
    assert r["options"] == [] and len(r["couldnt_check"]) == 2 and "rss" in r["couldnt_check"][0]


def test_all_followed_creators_configured(env):
    hub, *_ = env
    names = [sh["name"] for sh in hub.cfg["podcasts"]]
    assert len(names) == 7 and all(sh["feed"].split("=")[1][:2] in ("UC", "PL") for sh in hub.cfg["podcasts"])
    desc = hub.tools["podcast_options"].description
    assert "Dr K" in desc and "Jordan B Peterson" in desc and "Johnathan Bi" in desc


# ---------------- tasks + spoken debrief + examen ----------------

def test_task_list_markdown(env):
    hub, clock, *_ = env
    hub.call("add_task", {"task": "PHY 302 Agenda 14"})
    hub.call("add_task", {"task": "Curvilinear coordinates project"})
    assert hub.call("add_task", {"task": "phy 302 agenda 14"})["exists"]
    r = hub.call("complete_task", {"task": "agenda 14"})
    assert r["completed"] == "PHY 302 Agenda 14" and r["was_on_list"]
    t = hub.call("tasks")
    assert t["open"] == ["Curvilinear coordinates project"] and t["done_today"] == ["PHY 302 Agenda 14"]
    md = (hub.dir / "Tasks.md").read_text(encoding="utf-8")
    assert "- [ ] Curvilinear coordinates project" in md and "- [x] PHY 302 Agenda 14  ✅" in md
    # he ticks a box in Obsidian by hand -> Pakito sees it
    (hub.dir / "Tasks.md").write_text(md.replace("- [ ] Curvilinear", "- [x] Curvilinear"), encoding="utf-8")
    assert hub.call("tasks")["open"] == []


def test_end_day_asks_then_logs_and_examen(env):
    hub, clock, *_ = env
    hub.call("add_task", {"task": "CSE 355 assignment 3"})
    hub.call("add_task", {"task": "Read Taylor ch 5"})
    hub.call("start_focus", {"label": "CSE 355"})
    clock.advance(20)
    FakeTimer.fire_last()
    r = hub.call("end_day")
    assert "ask what he got done" in r["next"] and r["open_tasks"] == ["CSE 355 assignment 3", "Read Taylor ch 5"]
    out = hub.call("log_day", {"done": ["assignment 3 de 355", "gym"], "still_open": ["Read Taylor ch 5", "Lab 4"],
                               "note": "Tired but productive"})
    assert out["logged_done"] == ["CSE 355 assignment 3", "gym"]
    assert out["open_tasks_now"] == ["Read Taylor ch 5", "Lab 4"]
    ex = hub.call("save_examen", {"answers": ["Por mi familia", "Listo", "Paz en la misa; inquieto en el lab",
                                              "Ser impaciente", "Paciencia mañana"]})
    note = (hub.dir / ex["note"]).read_text(encoding="utf-8")
    assert "## Done today" in note and "- [x] CSE 355 assignment 3" in note and "- [x] gym" in note
    assert "## Still open → tomorrow" in note and "- [ ] Lab 4" in note
    assert "## Examen" in note and "**¿Por qué das gracias hoy?**" in note and "Paciencia mañana" in note
    assert "Tired but productive" in note and "1 blocks" not in note
    # a second debrief the same night keeps the logged answers
    hub.call("end_day")
    assert "Paciencia mañana" in (hub.dir / ex["note"]).read_text(encoding="utf-8")


def test_examen_questions_in_tool_description(env):
    hub, *_ = env
    d = hub.tools["save_examen"].description
    assert "ONE at a time" in d and "1. ¿Por qué das gracias hoy?" in d and "5. " in d


def test_spotify_search_down_uses_remembered_result_or_opens_search(env, tmp_path):
    from assistant.spotify import SpotifyControl
    hub, clock, launched, _ = env
    sp = FakeSp()
    ctl = SpotifyControl(sp, hub.launch, sleep=lambda s: None, hits_path=tmp_path / "hits.json")
    hub.spotify = ctl
    assert hub.call("play_music", {"query": "lofi beats"})["playing"]["uri"] == "spotify:playlist:lofibeats"

    def down(**k):
        raise RuntimeError("http status: 429 too many 502 error responses")
    sp.search = down
    ctl._mine = None
    sp.current_user_playlists = lambda limit=50: (_ for _ in ()).throw(RuntimeError("502"))
    r = hub.call("play_music", {"query": "lofi beats"})          # search down -> remembered from before
    assert r["playing"]["uri"] == "spotify:playlist:lofibeats" and r["playing"]["remembered"]
    ctl2 = SpotifyControl(sp, hub.launch, sleep=lambda s: None, hits_path=tmp_path / "hits.json")
    assert ctl2.find("LOFI beats")["remembered"]                  # survives a restart
    r = hub.call("play_music", {"query": "bad bunny", "kind": "artist"})   # never searched before
    assert r["opened_search"] == "bad bunny" and launched[-1] == "spotify:search:bad%20bunny"


def test_own_playlists_fetched_once_per_10_min(env):
    from assistant.spotify import SpotifyControl
    hub, clock, *_ = env
    sp = FakeSp()
    calls = []
    orig = sp.current_user_playlists
    sp.current_user_playlists = lambda limit=50: calls.append(1) or orig()
    ctl = SpotifyControl(sp, hub.launch, sleep=lambda s: None, hits_path=None, clock=clock)
    ctl.find("gym"); ctl.find("deep work")
    assert len(calls) == 1
    clock.advance(11)
    ctl.find("gym")
    assert len(calls) == 2


# ---------------- installed apps + Claude Code ----------------

def _fake_start_apps(hub, launched):
    from assistant.util import StartApps
    apps = [{"Name": "Claude", "AppID": "Claude_pzs8sxrjxfjjc!Claude"},
            {"Name": "Discord", "AppID": "com.squirrel.Discord.Discord"},
            {"Name": "Microsoft Teams", "AppID": "MSTeams_8wekyb3d8bbwe!MSTeams"}]
    hub.start_apps = StartApps(loader=lambda: apps, launch=launched.append)


def test_best_name_match():
    from assistant.util import best_name_match
    names = ["pakito-jarvis", "RabbitHole", "RabbitHole-MVP", "sputnik", "hw4", "CSE240HW4"]
    assert best_name_match("pakito", names) == "pakito-jarvis"
    assert best_name_match("rabbit hole", names) == "RabbitHole"
    assert best_name_match("rabbit hole mvp", names) == "RabbitHole-MVP"
    assert best_name_match("hw4", names) == "hw4"
    assert best_name_match("sputnick", names) == "sputnik"
    assert best_name_match("blender", names) is None


def test_open_claude_desktop_app_or_website(env):
    hub, clock, launched, _ = env
    _fake_start_apps(hub, launched)
    r = hub.call("open_app", {"name": "Claude"})
    assert r["opened"] == "Claude" and launched[-1] == "shell:AppsFolder\\Claude_pzs8sxrjxfjjc!Claude"
    hub.start_apps.loader = lambda: []          # not installed -> website
    hub.start_apps.apps = None
    r = hub.call("open_app", {"name": "claude", "force": True})
    assert launched[-1] == "https://claude.ai"


def test_open_any_installed_app(env):
    hub, clock, launched, _ = env
    _fake_start_apps(hub, launched)
    hub.call("open_app", {"name": "teams"})
    assert launched[-1] == "shell:AppsFolder\\MSTeams_8wekyb3d8bbwe!MSTeams"
    hub.call("open_app", {"name": "discord"})
    assert launched[-1] == "shell:AppsFolder\\com.squirrel.Discord.Discord"
    hub.call("open_app", {"name": "notepad"})    # not in the list -> run as a command
    assert launched[-1] == "notepad"


def test_open_claude_code_in_new_terminal(env, tmp_path, monkeypatch):
    hub, clock, launched, _ = env
    repos = tmp_path / "repos"
    for d in ("pakito-jarvis", "RabbitHole", "RabbitHole-MVP"):
        (repos / d).mkdir(parents=True)
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    hub.cfg["claude"].update(path=str(exe), projects_dir=str(repos))
    opened = []
    hub.open_terminal = lambda cmd, cwd: opened.append((cmd, cwd))
    r = hub.call("open_claude_code", {"project": "pakito"})
    assert opened[-1] == ([str(exe)], str(repos / "pakito-jarvis")) and r["project"] == "pakito-jarvis"
    hub.call("open_claude_code", {"project": "rabbit hole", "resume": True})
    assert opened[-1] == ([str(exe), "--continue"], str(repos / "RabbitHole"))
    hub.call("open_claude_code", {})
    assert opened[-1] == ([str(exe)], str(repos))
    r = hub.call("open_claude_code", {"project": "blender"})
    assert "error" in r and "pakito-jarvis" in r["projects"]
    hub.call("open_app", {"name": "Claude Code"})     # Gemini picking open_app still works
    assert opened[-1] == ([str(exe)], str(repos))


# ---------------- lock-in fixes (Oct 13) ----------------

def test_lockin_mid_block_upgrades_instead_of_restarting(env):
    hub, clock, *_ = env
    hub.call("start_focus", {"label": "AWS"})
    clock.advance(20)
    r = hub.call("start_focus", {"lockin": True, "label": "AWS con lock-in"})
    assert r["upgraded"] and hub.focus.locked
    assert hub.focus.status()["remaining_min"] == pytest.approx(hub.focus.block["planned_min"] - 20)  # same block
    assert "NOT connected" in r["site_blocker"]                                # no extension check-ins yet
    hub.blocker.ext_seen = clock()
    hub.focus.stop()
    assert hub.call("start_focus", {"lockin": True})["site_blocker"] == "extension connected"


def test_title_matching_only_exact_site_names():
    from assistant.blocker import title_site
    sites = ["instagram.com", "x.com", "reddit.com", "facebook.com", "netflix.com", "tiktok.com"]
    assert title_site("Instagram - Opera", sites) == "instagram.com"
    assert title_site("Mario (@mario) • Instagram photos and videos - Opera", sites) == "instagram.com"
    assert title_site("(3) Facebook - Opera", sites) == "facebook.com"
    assert title_site("Home / X - Opera", sites) == "x.com"
    assert title_site("Elon Musk on X: \"hi\" / X - Google Chrome", sites) == "x.com"
    assert title_site("r/Physics - Reddit - Opera", sites) == "reddit.com"
    assert title_site("Reddit - Dive into anything", sites) == "reddit.com"
    assert title_site("Home - Netflix - Microsoft​ Edge", sites) == "netflix.com"
    assert title_site("TikTok - Make Your Day", sites) == "tiktok.com"
    # things that merely mention a site stay open
    assert title_site("CSE 301: Instagram and privacy - Canvas - Opera", sites) is None
    assert title_site("Facebook's data practices.pdf - Opera", sites) is None
    assert title_site("Taylor Classical Mechanics - Opera", sites) is None


def test_backup_blocker_closes_the_tab_in_front(env):
    from assistant.blocker import Blocker
    hub, clock, _, notes = env
    closed = []
    win = {"t": ("Instagram - Opera", "opera.exe", 42)}
    b = Blocker(hub.focus, [], hub.notify, lister=lambda: set(), killer=lambda n: None,
                sites=hub.cfg["focus"]["block_sites"], fg=lambda: win["t"], closer=closed.append, clock=clock)
    assert b.check_sites() is None and not closed                  # not locked in: nothing happens
    hub.call("start_focus", {"lockin": True})                      # lock-in starts: checks right away
    assert closed == [42] and hub.focus.block["distractions"][-1]["note"] == "opened instagram.com (blocked)"
    assert b.check_sites() == "instagram.com"                      # still in front a second later: closed again,
    assert closed == [42, 42] and len(hub.focus.block["distractions"]) == 1   # but counted once
    win["t"] = ("Instagram - Notepad", "notepad.exe", 7)
    assert b.check_sites() is None                                 # not a browser
    hub.focus.stop()


# ---------------- Spotify: silent playback + wrong matches (Oct 14) ----------------

def test_spotify_checks_that_music_actually_started(env):
    hub, sp, launched = spotify_hub(env, silent=1)
    r = hub.call("play_music", {"query": "gym"})
    assert r["playing"]["uri"] == "spotify:playlist:gym"           # recovered after waking the app
    assert ("transfer", "pc") in sp.calls
    hub2, sp2, launched2 = spotify_hub(env, silent=99)              # never starts: say so, don't pretend
    r = hub2.call("play_music", {"query": "deep work", "force": True})
    assert "opened" in r and "didn't start" in r["note"] and launched2[-1] == "spotify:playlist:dw"


def test_pause_when_nothing_plays_is_not_an_error(env):
    hub, sp, _ = spotify_hub(env)
    sp.playing = False
    r = hub.call("music_control", {"action": "pause"})
    assert r["ok"] and r["note"] == "it already was"


def test_song_by_artist_uses_field_search_and_flags_bad_matches(env):
    from assistant.spotify import SpotifyControl, split_by
    assert split_by("Armageddon by Tut Tut Child") == ("Armageddon", "Tut Tut Child")
    assert split_by("Tusa de Karol G") == ("Tusa", "Karol G")

    class Sp(FakeSp):
        def search(self, q, limit=10, type="track"):
            self.calls.append(("search", q))
            items = [{"name": "Quiet Please!", "uri": "spotify:track:qp", "artists": [{"name": "Dan Bull"}]},
                     {"name": "Armageddon", "uri": "spotify:track:arm", "artists": [{"name": "Tut Tut Child"}]}]
            if "Nope" in q:
                items = items[:1]
            return {f"{type}s": {"items": items}}
    hub = env[0]
    sp = Sp()
    hub.spotify = SpotifyControl(sp, hub.launch, sleep=lambda s: None, hits_path=None)
    r = hub.call("play_music", {"query": "Armageddon by Tut Tut Child", "kind": "track"})
    assert ("search", "track:Armageddon artist:Tut Tut Child") in sp.calls
    assert r["playing"]["uri"] == "spotify:track:arm"               # best match, not just the first result
    r = hub.call("play_music", {"query": "Nope by Nobody", "kind": "track"})
    assert r["playing"].get("unsure")                               # Gemini says what it found instead


def test_expired_claude_login_is_explained(env, monkeypatch):
    from assistant import claude_bridge
    hub, _, _, notes = env
    monkeypatch.setattr(claude_bridge, "find_claude", lambda p=None: "claude.exe")
    hub.claude.threaded = False
    hub.claude.runner = lambda c, w, t: (1, '{"result": "Failed to authenticate: OAuth session expired and could '
                                            'not be refreshed", "is_error": true}', "")
    hub.call("ask_claude", {"question": "SQuInT schedule"})
    job = hub.claude.find()
    assert job["login_expired"] and "run claude, log in again" in job["spoken"]
    assert "log in again" in notes[-1][1]
