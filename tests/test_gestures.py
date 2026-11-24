"""Webcam hand control: synthetic hands (21 landmarks) drive a fake mouse."""
import copy

import pytest

from assistant.config import DEFAULTS
from assistant.gestures import HandMouse, OneEuro, pose
from assistant.hub import Hub


def hand(kind="point", at=(0.5, 0.5)):
    """A right hand, palm to the camera, knuckles around `at`. kind: point, scroll, fist, open, pinch, pinch_mid."""
    x, y = at
    lm = [(x, y)] * 21
    lm[0] = (x, y + 0.25)                                       # wrist
    up = {"point": {"index"}, "scroll": {"index", "middle"}, "open": {"index", "middle", "ring", "pinky"},
          "fist": set(), "pinch": {"middle", "ring", "pinky"}, "pinch_mid": {"index", "ring", "pinky"}}[kind]
    for i, (name, dx) in enumerate((("index", -0.04), ("middle", 0.0), ("ring", 0.04), ("pinky", 0.08))):
        base = 5 + 4 * i
        lm[base] = (x + dx, y + 0.08)                           # knuckle (MCP)
        lm[base + 1] = (x + dx, y)                              # PIP
        lm[base + 2] = (x + dx, y - 0.04 if name in up else y + 0.06)
        lm[base + 3] = (x + dx, y - 0.08 if name in up else y + 0.10)   # tip: far up, or curled back down
    lm[4] = (x - 0.16, y + 0.10)                                # thumb tip, out to the side
    if kind == "pinch":
        lm[4] = lm[8] = (x - 0.05, y + 0.02)                   # thumb meets the index tip
    if kind == "pinch_mid":
        lm[4] = lm[12] = (x, y + 0.02)
    return lm


class Mouse:
    def __init__(self):
        self.log, self.x, self.y = [], 0, 0

    def move(self, dx, dy):
        self.x, self.y = self.x + dx, self.y + dy

    def down(self, b):
        self.log.append(("down", b))

    def up(self, b):
        self.log.append(("up", b))

    def click(self, b):
        self.log.append(("click", b))

    def scroll(self, n):
        self.log.append(("scroll", n))


def run(hm, frames, dt=1 / 30, t0=0.0):
    t = t0
    for f in frames:
        t += dt
        if f is None:
            hm.lost()
        else:
            hm.update(f, t)
    return t


def test_reading_gestures():
    assert pose(hand("point"))["gesture"] == "point"
    assert pose(hand("scroll"))["gesture"] == "scroll"
    assert pose(hand("fist"))["gesture"] == "fist" and pose(hand("open"))["gesture"] == "open"
    assert pose(hand("pinch"))["pinch"] < 0.3 and pose(hand("point"))["pinch"] > 0.6


def test_pointing_moves_like_a_trackpad_and_rest_doesnt_jump():
    m = Mouse()
    hm = HandMouse(m, speed=2.0, screen_px=1000)
    run(hm, [hand("point", (0.5 + i * 0.005, 0.5)) for i in range(30)])
    assert m.x > 150 and abs(m.y) < 5                          # moved right
    moved = m.x
    run(hm, [hand("fist", (0.3, 0.5))] * 10, t0=1)             # rest: hand goes back left with a fist
    run(hm, [hand("point", (0.3, 0.5))] * 10, t0=1.4)          # pointing again from there: no jump back
    assert abs(m.x - moved) < 10


def test_quick_pinch_clicks_without_moving():
    m = Mouse()
    hm = HandMouse(m)
    run(hm, [hand("point")] * 5 + [hand("pinch")] * 4 + [hand("point")] * 3)
    assert m.log == [("click", "left")] and abs(m.x) < 3 and abs(m.y) < 3


def test_held_pinch_drags_and_drops():
    m = Mouse()
    hm = HandMouse(m, speed=2.0, screen_px=1000, drag_hold_sec=0.4)
    run(hm, [hand("point")] * 3 + [hand("pinch", (0.5, 0.5))] * 20                    # hold ~0.66 s
        + [hand("pinch", (0.5 + i * 0.01, 0.5)) for i in range(15)] + [hand("point", (0.65, 0.5))] * 3)
    assert m.log == [("down", "left"), ("up", "left")] and m.x > 100


def test_right_click_and_scroll():
    m = Mouse()
    hm = HandMouse(m)
    run(hm, [hand("point")] * 3 + [hand("pinch_mid")] * 3 + [hand("point")] * 3)
    assert m.log == [("click", "right")]
    run(hm, [hand("scroll", (0.5, 0.6 - i * 0.01)) for i in range(20)], t0=1)          # two fingers moving up
    assert any(e[0] == "scroll" and e[1] > 0 for e in m.log)


def test_losing_the_hand_lets_go_of_a_drag():
    m = Mouse()
    hm = HandMouse(m, drag_hold_sec=0.2)
    run(hm, [hand("pinch")] * 12 + [None])
    assert m.log == [("down", "left"), ("up", "left")]


def test_one_euro_smooths_jitter():
    f = OneEuro()
    outs = [f(0.5 + (0.01 if i % 2 else -0.01), i / 30) for i in range(60)]
    assert max(outs[30:]) - min(outs[30:]) < 0.01                # ±0.01 noise comes out much smaller


@pytest.fixture
def hub(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    return Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)


def test_mouse_by_voice_jumps_to_a_screen_and_clicks(hub):
    from tests.test_windows import FakeDesktop
    hub.desktop._backend = FakeDesktop()
    from assistant.gestures import HandControl

    class M(Mouse):
        def goto(self, x, y):
            self.x, self.y = x, y
    hub.hands = HandControl({}, mouse=M())
    assert hub.call("mouse", {"action": "screen", "screen": 2}, "voice") == {"cursor_on_screen": 2}
    assert (hub.hands.mouse.x, hub.hands.mouse.y) == (1920 + 960, 520)
    hub.call("mouse", {"action": "double_click"}, "voice")
    assert hub.hands.mouse.log == [("click", "left"), ("click", "left")]


def test_without_mediapipe_it_says_how_to_install(hub, monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name in ("mediapipe", "cv2"):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    from assistant.gestures import HandControl
    hub.hands = HandControl({}, mouse=Mouse())
    assert "pip install mediapipe" in hub.call("hand_control", {"on": True}, "voice")["error"]


def test_agents_cant_use_the_mouse():
    from assistant.server import AGENT_HIDDEN
    assert {"hand_control", "mouse"} <= AGENT_HIDDEN


def test_a_crash_in_the_camera_thread_is_reported_not_silent():
    import threading
    from assistant.gestures import HandControl
    said = []
    h = HandControl({}, mouse=Mouse(), log=said.append)

    def boom(ready):
        h.stage = "opening the camera"
        raise OSError("device busy")
    h._run = boom
    ready = threading.Event()
    h._safe_run(ready)
    assert ready.is_set() and "opening the camera" in h.error and "device busy" in said[0]


def test_camera_tries_the_next_driver():
    from assistant.gestures import open_camera

    class Cap:
        def __init__(self, ok):
            self.ok = ok

        def isOpened(self):
            return self.ok

        def set(self, *a):
            pass

        def read(self):
            return self.ok, None

        def release(self):
            pass

    class CV2:
        CAP_DSHOW, CAP_MSMF, CAP_ANY = 700, 1400, 0
        CAP_PROP_FRAME_WIDTH = CAP_PROP_FRAME_HEIGHT = 3

        def VideoCapture(self, idx, api):
            return Cap(api == 1400)
    import sys
    if sys.platform != "win32":
        import assistant.gestures as g
        real = g.backends
        g.backends = lambda cv2: [("dshow", 700), ("msmf", 1400), ("any", 0)]
        try:
            cap, how = open_camera(CV2(), 0)
        finally:
            g.backends = real
    else:
        cap, how = open_camera(CV2(), 0)
    assert how == "msmf" and cap.ok


def test_only_his_chosen_hand_drives_the_mouse():
    from assistant.gestures import pick_hand

    class C:
        def __init__(self, name):
            self.category_name = name
    both = [[C("Left")], [C("Right")]]                # on the mirrored picture, his right hand is labeled "Left"
    assert pick_hand(both, "right") == 0 and pick_hand(both, "left") == 1 and pick_hand(both, "any") == 0
    assert pick_hand([[C("Right")]], "right") is None and pick_hand([], "right") is None
