"""Webcam hand control (experimental): move the mouse, click, drag and scroll with one hand in front of the webcam.

Gestures (one hand, palm toward the camera):
  ☝  point (only the index finger up)   moves the cursor, like a trackpad: lower the hand to rest, nothing moves
  🤏 thumb + index pinch                 click; hold the pinch ~½ s to drag, open it to drop
  🤌 thumb + middle pinch                right click
  ✌  index + middle up, move up/down     scroll
  ✊ fist / ✋ open hand / no hand         nothing (rest your arm)
The cursor freezes while your fingers close for a click, so the click lands where you were pointing. Which screen:
say "mouse a la pantalla 2" (the cursor jumps there; the hand does the fine moves).

Arms get tired: this is for short bursts ("click that", "drag this there"), not all day.
Install once: `run-pakito.bat pip install mediapipe` (the hand model, ~8 MB, downloads the first time).
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/"
             "hand_landmarker.task")
INSTALL = "Hand control needs MediaPipe: run `run-pakito.bat pip install mediapipe`, then restart Pakito."
HAND_DEFAULTS = {"camera": 0, "speed": 2.2, "preview": True, "drag_hold_sec": 0.45,
                 "hand": "right",            # only this hand moves the mouse ("left", or "any")
                 "idle_off_sec": 120}        # turns itself off after this long without his hand in view
ACTIVE_STATES = {"pointing", "dragging", "scroll", "clicking"}


def pick_hand(handedness: list, want: str) -> int | None:
    """Which detected hand to follow. We feed MediaPipe a mirrored picture, and on it MediaPipe calls his right hand
    "Left" (Nov 24: "right" was following his left hand), so the label is swapped back here."""
    if not handedness:
        return None
    if want == "any":
        return 0
    label = {"right": "left", "left": "right"}.get(want, want)
    for i, cats in enumerate(handedness):
        if cats and str(getattr(cats[0], "category_name", "")).lower() == label:
            return i
    return None

WRIST, THUMB_TIP, INDEX_MCP, INDEX_PIP, INDEX_TIP = 0, 4, 5, 6, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP, RING_PIP, RING_TIP, PINKY_PIP, PINKY_TIP = 9, 10, 12, 14, 16, 18, 20
PINCH_ON, PINCH_OFF, PINCH_NEAR = 0.30, 0.45, 0.60     # thumb–fingertip distance / hand size (hysteresis)


# ---------------------------------------------------------------- smoothing
class OneEuro:
    """The One Euro filter: steady when the hand is still, little lag when it moves fast."""

    def __init__(self, min_cutoff: float = 1.2, beta: float = 0.02, d_cutoff: float = 1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x = self.dx = self.t = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x: float, t: float) -> float:
        if self.t is None or t <= self.t:
            self.x, self.dx, self.t = x, 0.0, t
            return x
        dt = t - self.t
        dx = (x - self.x) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        self.dx = a_d * dx + (1 - a_d) * self.dx
        a = self._alpha(self.min_cutoff + self.beta * abs(self.dx), dt)
        self.x, self.t = a * x + (1 - a) * self.x, t
        return self.x

    def reset(self) -> None:
        self.x = self.dx = self.t = None


# ---------------------------------------------------------------- reading a hand
def _d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def pose(lm: list) -> dict:
    """21 landmarks (x, y in 0..1, image already mirrored) -> which fingers are up and how close the pinches are."""
    size = max(_d(lm[WRIST], lm[MIDDLE_MCP]), 1e-4)
    up = {name: _d(lm[tip], lm[WRIST]) > _d(lm[pip], lm[WRIST]) * 1.12
          for name, tip, pip in (("index", INDEX_TIP, INDEX_PIP), ("middle", MIDDLE_TIP, MIDDLE_PIP),
                                 ("ring", RING_TIP, RING_PIP), ("pinky", PINKY_TIP, PINKY_PIP))}
    pinch = _d(lm[THUMB_TIP], lm[INDEX_TIP]) / size
    pinch_mid = _d(lm[THUMB_TIP], lm[MIDDLE_TIP]) / size
    if up["index"] and not up["middle"] and not up["ring"] and not up["pinky"]:
        g = "point"
    elif up["index"] and up["middle"] and not up["ring"] and not up["pinky"]:
        g = "scroll"
    elif not any(up.values()):
        g = "fist"
    elif all(up.values()):
        g = "open"
    else:
        g = "other"
    return {"gesture": g, "pinch": pinch, "pinch_mid": pinch_mid, "up": up, "size": size,
            "anchor": lm[INDEX_MCP]}        # the knuckle: doesn't jump when the fingertip pinches


# ---------------------------------------------------------------- gestures -> mouse
class HandMouse:
    """Turns a stream of hand poses into mouse moves/clicks. Pure logic: the mouse is passed in."""

    def __init__(self, mouse, speed: float = 2.2, screen_px: int = 1920, drag_hold_sec: float = 0.45):
        self.mouse, self.speed, self.screen_px, self.drag_hold = mouse, speed, screen_px, drag_hold_sec
        self.fx, self.fy = OneEuro(), OneEuro()
        self.last: tuple[float, float] | None = None    # where the knuckle was last frame (None = clutch open)
        self.pinch_t: float | None = None               # when the index pinch closed
        self.dragging = False
        self.mid_pinched = False
        self.scroll_acc = 0.0
        self.state = "rest"

    def lost(self) -> None:
        """No hand in view: let go of anything held."""
        if self.dragging:
            self.mouse.up("left")
        self.dragging, self.pinch_t, self.last, self.mid_pinched = False, None, None, False
        self.fx.reset(), self.fy.reset()
        self.state = "no hand"

    def update(self, lm: list, t: float) -> str:
        p = pose(lm)
        if self.last is None:                    # (re)engaging: smooth from here, not from where the hand used to be
            self.fx.reset(), self.fy.reset()
        x, y = self.fx(p["anchor"][0], t), self.fy(p["anchor"][1], t)
        ev = ""
        # ---- thumb + index: click / drag ----
        if self.pinch_t is None and p["pinch"] < PINCH_ON:
            self.pinch_t = t
        elif self.pinch_t is not None and p["pinch"] > PINCH_OFF:
            if self.dragging:
                self.mouse.up("left")
                ev = "drop"
            else:
                self.mouse.click("left")
                ev = "click"
            self.pinch_t, self.dragging = None, False
        if self.pinch_t is not None and not self.dragging and t - self.pinch_t >= self.drag_hold:
            self.mouse.down("left")
            self.dragging, ev = True, "drag"
        # ---- thumb + middle: right click ----
        if not self.mid_pinched and p["pinch_mid"] < PINCH_ON and p["pinch"] > PINCH_OFF:
            self.mid_pinched = True
            self.mouse.click("right")
            ev = "right click"
        elif self.mid_pinched and p["pinch_mid"] > PINCH_OFF:
            self.mid_pinched = False
        # ---- moving ----
        closing = self.pinch_t is not None and not self.dragging or PINCH_ON <= p["pinch"] < PINCH_NEAR
        moving = (p["gesture"] == "point" and not closing) or self.dragging
        if p["gesture"] == "scroll" and self.pinch_t is None:
            if self.last is not None:
                self.scroll_acc += (self.last[1] - y) * 40          # hand up = scroll up
                ticks = int(self.scroll_acc)
                if ticks:
                    self.mouse.scroll(ticks)
                    self.scroll_acc -= ticks
                    ev = ev or "scroll"
            self.last = (x, y)
            self.state = "scroll"
        elif moving:
            if self.last is not None:
                dx = (x - self.last[0]) * self.speed * self.screen_px
                dy = (y - self.last[1]) * self.speed * self.screen_px
                if abs(dx) + abs(dy) >= 1:
                    self.mouse.move(round(dx), round(dy))
            self.last = (x, y)
            self.state = "dragging" if self.dragging else "pointing"
        else:
            self.last = None if not closing else self.last           # clutch: re-entering point doesn't jump
            self.scroll_acc = 0.0
            self.state = "clicking" if closing else {"fist": "paused", "open": "rest"}.get(p["gesture"], "rest")
        return ev


# ---------------------------------------------------------------- the real mouse
class WinMouse:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.c, self.u, self.wt = ctypes, ctypes.windll.user32, wintypes

    def pos(self) -> tuple[int, int]:
        p = self.wt.POINT()
        self.u.GetCursorPos(self.c.byref(p))
        return p.x, p.y

    def move(self, dx: int, dy: int) -> None:
        x, y = self.pos()
        self.u.SetCursorPos(x + dx, y + dy)                        # absolute: no mouse acceleration surprises

    def goto(self, x: int, y: int) -> None:
        self.u.SetCursorPos(x, y)

    def down(self, btn: str) -> None:
        self.u.mouse_event(0x0002 if btn == "left" else 0x0008, 0, 0, 0, 0)

    def up(self, btn: str) -> None:
        self.u.mouse_event(0x0004 if btn == "left" else 0x0010, 0, 0, 0, 0)

    def click(self, btn: str) -> None:
        self.down(btn)
        self.up(btn)

    def scroll(self, ticks: int) -> None:
        self.u.mouse_event(0x0800, 0, 0, ticks * 120, 0)


def model_path() -> Path:
    d = Path.home() / ".pakito" / "models"
    f = d / "hand_landmarker.task"
    if not f.exists():
        import urllib.request
        d.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL, tmp)
        os.replace(tmp, f)
    return f


# ---------------------------------------------------------------- the camera loop
def backends(cv2) -> list[tuple[str, int]]:
    """Camera drivers to try, in order. On Windows DirectShow opens most webcams; Media Foundation the rest."""
    if sys.platform == "win32":
        return [("dshow", cv2.CAP_DSHOW), ("msmf", cv2.CAP_MSMF), ("any", cv2.CAP_ANY)]
    return [("any", cv2.CAP_ANY)]


def open_camera(cv2, index: int, prefer: str | None = None, log: Callable[[str], None] = lambda m: None):
    """-> (capture, driver name) for the first driver that opens the camera AND gives a frame, else (None, why)."""
    tried = []
    order = sorted(backends(cv2), key=lambda b: b[0] != prefer)
    for name, api in order:
        t = time.monotonic()
        cap = cv2.VideoCapture(index, api)
        ok = cap.isOpened()
        if ok:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            ok, _ = cap.read()
        took = time.monotonic() - t
        log(f"camera {index} via {name}: {'ok' if ok else 'no'} ({took:.1f} s)")
        if ok:
            return cap, name
        cap.release()
        tried.append(f"{name} {'no frame' if cap else 'closed'}")
    return None, f"camera {index} didn't open ({', '.join(tried)}). Another app using it? Other index: \"hands\": " \
                 f"{{\"camera\": 1}}"


class quiet_stderr:
    """MediaPipe's C++ side prints INFO/WARNING lines straight to the console (not through Python): silence them
    while it starts up and runs its first frame."""

    def __enter__(self):
        try:
            sys.stderr.flush()
            self.saved = os.dup(2)
            self.null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(self.null, 2)
        except OSError:
            self.saved = None
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            os.dup2(self.saved, 2)
            os.close(self.saved)
            os.close(self.null)
        return False


def make_marker(vision, BaseOptions):
    opts = vision.HandLandmarkerOptions(base_options=BaseOptions(model_asset_path=str(model_path())),
                                        running_mode=vision.RunningMode.VIDEO, num_hands=2,
                                        min_hand_detection_confidence=0.6, min_tracking_confidence=0.5)
    return vision.HandLandmarker.create_from_options(opts)


class HandControl:
    def __init__(self, cfg: dict | None = None, mouse=None, screen_px: Callable[[], int] = lambda: 1920,
                 log: Callable[[str], None] = print):
        self.cfg = {**HAND_DEFAULTS, **(cfg or {})}
        self._mouse, self.screen_px, self.log = mouse, screen_px, log
        self.stop_ev = threading.Event()
        self.thread: threading.Thread | None = None
        self.hand: HandMouse | None = None
        self.error: str | None = None
        self.stage = "off"

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    @property
    def mouse(self):
        if self._mouse is None:
            if sys.platform != "win32":
                raise RuntimeError("Hand control works on Windows for now.")
            self._mouse = WinMouse()
        return self._mouse

    def start(self, preview: bool | None = None, wait: float = 25) -> dict:
        if self.running:
            if self.stop_ev.is_set():
                return {"error": f"Still closing the camera ({self.stage}). Try again in a few seconds; if it's stuck, "
                                 "restart Pakito and run `run-pakito.bat check hands`."}
            return {"hand_control": "already on", "stage": self.stage}
        try:
            import cv2  # noqa: F401
            import mediapipe  # noqa: F401
        except ImportError:
            return {"error": INSTALL}
        try:
            self.mouse
        except RuntimeError as e:
            return {"error": str(e)}
        if preview is not None:
            self.cfg["preview"] = preview
        self.stop_ev.clear()
        self.error = None
        ready = threading.Event()
        self.thread = threading.Thread(target=self._safe_run, args=(ready,), daemon=True, name="hand-control")
        self.thread.start()
        if not ready.wait(wait):
            return {"hand_control": "starting", "stage": self.stage,
                    "note": "Slow to start; Pakito prints when it's ready or what went wrong."}
        if self.error:
            return {"error": self.error}
        return {"hand_control": "on", "preview": self.cfg["preview"]}

    def stop(self) -> None:
        self.stop_ev.set()

    def _safe_run(self, ready: threading.Event) -> None:
        try:
            self._run(ready)
        except Exception as e:                           # never die silently
            self.error = f"Hand control stopped while {self.stage}: {type(e).__name__}: {e}"
            self.log(f"\n⚠ {self.error}  (details: run-pakito.bat check hands)")
        finally:
            self.stage = "off"
            ready.set()

    def _run(self, ready: threading.Event) -> None:
        os.environ.setdefault("GLOG_minloglevel", "2")             # keep MediaPipe's startup chatter out of the
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")         # console
        self.stage = "loading the hand model"
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
        with quiet_stderr():
            marker = make_marker(vision, BaseOptions)
        self.stage = "opening the camera"
        cam, how = open_camera(cv2, int(self.cfg["camera"]), self.cfg.get("backend"))
        if cam is None:
            marker.close()
            self.error = f"Hand control couldn't start: {how}"
            self.log(f"\n⚠ {self.error}")
            ready.set()
            return
        self.hand = HandMouse(self.mouse, self.cfg["speed"], self.screen_px(), self.cfg["drag_hold_sec"])
        self.stage = "running"
        ready.set()
        self.log(f"\n🖐 Hand control on (camera {self.cfg['camera']}, {how}): point with your "
                 f"{'' if self.cfg['hand'] == 'any' else self.cfg['hand'] + ' '}index finger.\n› ")
        t0 = time.monotonic()
        win = "Pakito hands"
        last_active = 0.0
        first = quiet_stderr().__enter__()                   # the first frames print alignment warnings
        try:
            while not self.stop_ev.is_set():
                if first is not None and time.monotonic() - t0 > 3:
                    first.__exit__(None, None, None)
                    first = None
                ok, frame = cam.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                frame = cv2.flip(frame, 1)                       # mirror: your right is the screen's right
                t = time.monotonic() - t0
                res = marker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB,
                                                       data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), int(t * 1000))
                i = pick_hand(res.handedness, str(self.cfg["hand"]).lower()) if res.hand_landmarks else None
                if i is not None:
                    self.hand.update([(p.x, p.y) for p in res.hand_landmarks[i]], t)
                else:
                    self.hand.lost()                             # no hand, or only the other one
                if i is not None:                                # his hand is in view: he's using it
                    last_active = t                              # (Nov 24: counting only pointing turned it off mid-use)
                elif t - last_active > float(self.cfg["idle_off_sec"]):
                    self.log(f"\n🖐 No hand in view for {int(self.cfg['idle_off_sec'] // 60) or 1} min: turning hand "
                             "control off. ('activa las manos' turns it back on.)")
                    break
                if self.cfg["preview"]:
                    self._draw(cv2, frame, res, win)
        finally:
            if first is not None:
                first.__exit__(None, None, None)
            self.stage = "closing the camera"
            if self.hand:
                self.hand.lost()
            cam.release()
            marker.close()
            if self.cfg["preview"]:
                try:
                    cv2.destroyWindow(win)
                    cv2.waitKey(1)
                except Exception:
                    pass
            self.log("\n🖐 Hand control off.\n› ")

    def _draw(self, cv2, frame, res, win: str) -> None:
        h, w = frame.shape[:2]
        for hand in res.hand_landmarks or []:
            for p in hand:
                cv2.circle(frame, (int(p.x * w), int(p.y * h)), 3, (80, 220, 120), -1)
        cv2.putText(frame, self.hand.state, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        small = cv2.resize(frame, (320, 240))
        cv2.imshow(win, small)
        try:
            cv2.setWindowProperty(win, cv2.WND_PROP_TOPMOST, 1)
        except Exception:
            pass
        cv2.waitKey(1)
