"""Window control by voice, against a fake Windows (two monitors side by side, Windows numbers them 1 and 2)."""
import copy

import pytest

from assistant.config import DEFAULTS
from assistant.hub import Hub
from assistant.windows import Desktop, Screen, Win, Win32


class FakeDesktop:
    def __init__(self):
        self.scr = [Screen(2, 1920, 0, 1920, 1040), Screen(1, 0, 0, 1920, 1040, primary=True)]
        self.wins = [
            Win(1, "app.py - pakito-jarvis - Visual Studio Code", "code", (0, 0, 1920, 1040), maximized=True),
            Win(2, "Spotify Premium", "spotify", (2000, 100, 3000, 800)),
            Win(3, "Canvas - Opera", "opera", (100, 100, 900, 900)),
            Win(4, "YouTube - Opera", "opera", (120, 120, 920, 920)),
            Win(5, "Claude", "claude", (300, 300, 1300, 1000), minimized=True),
        ]
        self.fg, self.log = 1, []

    def screens(self):
        return list(self.scr)

    def windows(self):
        return list(self.wins)

    def foreground(self):
        return self.fg

    def _w(self, hwnd):
        return next(w for w in self.wins if w.hwnd == hwnd)

    def show(self, hwnd, cmd):
        w = self._w(hwnd)
        self.log.append(("show", hwnd, cmd))
        w.minimized, w.maximized = cmd == Win32.SW_MINIMIZE, cmd == Win32.SW_MAXIMIZE

    def move(self, hwnd, x, y, w, h):
        self.log.append(("move", hwnd, x, y, w, h))
        self._w(hwnd).rect = (x, y, x + w, y + h)

    def focus(self, hwnd):
        self.fg = hwnd
        self._w(hwnd).minimized = False

    def close(self, hwnd):
        self.log.append(("close", hwnd))
        self.wins = [w for w in self.wins if w.hwnd != hwnd]

    def keys(self, *vks):
        self.log.append(("keys",) + vks)


@pytest.fixture
def fake():
    return FakeDesktop()


@pytest.fixture
def hub(tmp_path, fake):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    h = Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)
    h.desktop._backend = fake
    h.desktop.sleep = lambda s: None
    return h


def test_find_by_what_he_calls_it(fake):
    d = Desktop(fake)
    assert d.find("vs code").hwnd == 1 and d.find("Spotify").hwnd == 2 and d.find("la de canvas").hwnd == 3
    assert [w.hwnd for w in d.find_all("opera")] == [3, 4] and d.find(None).hwnd == 1     # None = in front
    assert d.find("netflix") is None


def test_main_screen_is_1_even_when_windows_calls_it_2(fake):
    """Nov 23: his main screen is Windows' display 2; he says 'pantalla 1' and means the main one."""
    fake.scr = [Screen(1, 0, 0, 1920, 1040), Screen(2, 1920, 0, 2560, 1400, primary=True),
                Screen(3, -1920, 0, 1920, 1080)]                       # + the projector, on the left
    d = Desktop(fake)
    assert [(s.n, s.x, s.primary) for s in d.screens()] == [(1, 1920, True), (2, -1920, False), (3, 0, False)]
    assert d.screen(1).primary and d.screen_of(fake.wins[0]).n == 3
    assert [s.n for s in Desktop(fake, numbering="windows").screens()] == [1, 2, 3]


def test_screens_use_windows_numbers(fake):
    d = Desktop(fake)
    assert [s.n for s in d.screens()] == [1, 2] and d.screen(2).x == 1920
    assert d.screen_of(fake.wins[1]).n == 2 and d.screen(3) is None


def test_move_spotify_to_screen_1_left_half(hub, fake):
    r = hub.call("window", {"action": "move", "app": "spotify", "screen": 1, "position": "left"}, "voice")
    assert r == {"moved": "spotify", "screen": 1, "position": "left"}
    assert fake._w(2).rect == (0, 0, 960, 1040)


def test_move_maximized_window_restores_then_maximizes_on_the_other_screen(hub, fake):
    hub.call("window", {"action": "move", "app": "vs code", "screen": 2}, "voice")
    assert ("show", 1, Win32.SW_RESTORE) in fake.log and fake.log[-1] == ("show", 1, Win32.SW_MAXIMIZE)
    assert fake._w(1).rect[0] == 1920


def test_no_such_screen(hub):
    r = hub.call("window", {"action": "move", "app": "spotify", "screen": 3}, "voice")
    assert "no screen 3" in r["error"] and r["screens"] == [1, 2]


def test_minimize_maximize_focus(hub, fake):
    assert hub.call("window", {"action": "minimize", "app": "spotify"}, "voice")["minimized"] == "spotify"
    assert fake._w(2).minimized
    hub.call("window", {"action": "focus", "app": "claude"}, "voice")
    assert fake.fg == 5 and not fake._w(5).minimized
    hub.call("window", {"action": "maximize"}, "voice")                       # the one in front = Claude
    assert fake._w(5).maximized


def test_closing_several_asks_first_and_agents_cant_confirm(hub, fake):
    r = hub.call("window", {"action": "close", "app": "opera", "all_matching": True}, "voice")
    assert "confirm_first" in r and len(fake.wins) == 5
    r = hub.call("window", {"action": "close", "app": "opera", "all_matching": True, "confirm": True}, "agent")
    assert "confirm_first" in r and len(fake.wins) == 5                       # an agent's confirm is ignored
    r = hub.call("window", {"action": "close", "app": "opera", "all_matching": True, "confirm": True}, "voice")
    assert r["count"] == 2 and not any(w.proc == "opera" for w in fake.wins)


def test_closing_one_needs_no_confirmation(hub, fake):
    assert hub.call("window", {"action": "close", "app": "spotify"}, "voice")["closed"] == "spotify"


def test_save_and_apply_a_layout(hub, fake):
    saved = {}
    hub.save_local = lambda u: saved.update(u)
    fake.move(2, 1920, 0, 960, 1040)                                          # Spotify: left half of screen 2
    r = hub.call("layout", {"action": "save", "name": "modo trabajo"}, "voice")
    assert "spotify → screen 2 (left)" in r["apps"] and "code → screen 1 (max)" in r["apps"]
    assert saved["layouts"]["modo trabajo"]
    fake.move(2, 0, 0, 500, 500)                                              # he messes things up
    fake.wins = [w for w in fake.wins if w.proc != "claude"]                  # and closes Claude
    hub.cfg["layouts"]["modo trabajo"].append({"app": "claude", "screen": 1, "position": "right"})
    opened = []

    def open_app(name):
        opened.append(name)
        fake.wins.append(Win(9, "Claude", "claude", (10, 10, 500, 500)))
        return {"opened": name}
    hub.desktop.open_app = open_app
    r = hub.call("layout", {"action": "apply", "name": "trabajo"}, "voice")
    assert opened == ["claude"] and "claude" in r["arranged"]
    assert fake._w(2).rect == (1920, 0, 2880, 1040) and fake._w(9).rect == (960, 0, 1920, 1040)


def test_stop_interrupts_a_layout(hub, fake):
    hub.cfg["layouts"] = {"trabajo": [{"app": "spotify", "screen": 1}, {"app": "code", "screen": 2}]}
    hub.desktop.cancelled = lambda: True
    r = hub.desktop.apply(hub.cfg["layouts"]["trabajo"])
    assert r == {"stopped": True, "done": []}
    assert hub.call("stop_actions", {}, "voice") == {"stopped": True} and hub.cancel.is_set()


def test_routine_can_apply_a_layout(hub, fake):
    hub.cfg["layouts"] = {"trabajo": [{"app": "spotify", "screen": 1, "position": "right"}]}
    hub.routines.path.write_text("## work mode\naliases: trabajo\n- layout: trabajo\n", encoding="utf-8")
    r = hub.call("run_routine", {"name": "work mode"}, "voice")
    assert r["ran"][0]["result"]["arranged"] == ["spotify"]


def test_clipboard_paste_into_an_app(hub, fake, monkeypatch):
    from assistant import dictation
    pasted = []
    monkeypatch.setattr(dictation, "paste", lambda t: pasted.append(t) or True)
    monkeypatch.setattr("assistant.hub.time.sleep", lambda s: None)
    r = hub.call("clipboard", {"action": "paste", "app": "claude", "text": "explícame esto"}, "voice")
    assert r == {"pasted": True, "into": "claude"} and fake.fg == 5 and pasted == ["explícame esto"]
    logged = str(hub.events.read()[-1])
    assert "explícame" not in logged                                          # what he pastes stays out of the logs


def test_claude_code_in_a_named_folder_and_screen(hub, fake, tmp_path):
    folder = tmp_path / "Ercilia Works"
    folder.mkdir()
    hub.cfg["claude"]["folders"] = {"Ercilia Works": str(folder)}
    opened = []
    hub.open_claude_terminal = lambda args, f, title: (opened.append((f, title)),
                                                       fake.wins.append(Win(7, title, "windowsterminal",
                                                                            (0, 0, 800, 600))))
    import assistant.claude_bridge as cb
    cb_find = cb.find_claude
    cb.find_claude = lambda p=None: "claude"
    try:
        r = hub.call("open_claude_code", {"project": "ercilia", "screen": 2}, "voice")
    finally:
        cb.find_claude = cb_find
    assert r["folder"] == str(folder) and opened == [(str(folder), "Claude · ercilia works")]
    import time
    for _ in range(30):
        if fake._w(7).rect[0] == 1920:
            break
        time.sleep(0.05)
    assert fake._w(7).rect[0] == 1920                                         # moved to screen 2


def test_second_brain_is_a_named_folder(hub, tmp_path, fake):
    import assistant.claude_bridge as cb
    hub.open_claude_terminal = lambda args, f, title: None
    cb_find = cb.find_claude
    cb.find_claude = lambda p=None: "claude"
    try:
        r = hub.call("open_claude_code", {"project": "second brain"}, "voice")
    finally:
        cb.find_claude = cb_find
    assert r["folder"] == str(tmp_path)


def test_agents_cant_move_windows():
    from assistant.server import AGENT_HIDDEN
    assert {"window", "layout", "clipboard"} <= AGENT_HIDDEN


def test_not_windows_says_so():
    import sys
    if sys.platform == "win32":
        pytest.skip("on Windows the real backend loads")
    with pytest.raises(RuntimeError, match="Windows"):
        Desktop().screens()
