"""Pakibot: the on-screen pet. A small always-on-top window that reads Pakito's state from /pet.

Started automatically by Pakito's voice mode (turn off with "pet": {"enabled": false}). It's its own
process: if it crashes, the voice side doesn't notice, and if Pakito quits, it closes itself.
  * drag it anywhere with the left mouse button (position is remembered)
  * right-click: pop-in / stay / hide
  * never takes keyboard focus, no taskbar button

The drawing is a pure function (draw -> grid of colors) so it's testable without a screen.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
import urllib.request
from pathlib import Path

W, H = 48, 43                                   # sprite grid (pixels before scaling)
K = {"o": "#444441", "b": "#D3D1C7", "l": "#F1EFE8", "m": "#B4B2A9", "s": "#888780", "scr": "#12302A",
     "scr2": "#163A33", "gl": "#5DCAA5", "gls": "#2E7D66", "am": "#EF9F27", "co": "#D85A30", "bl": "#378ADD",
     "gr": "#97C459", "pu": "#7F77DD", "dim": "#0E2420", "glare": "#3B6F64", "ink": "#2C2C2A",
     "fuelbg": "#3B3B38", "hi": "#C9F0E2", "white": "#FFFFFF", "amber_soft": "#FAC775", "blue_soft": "#B5D4F4"}
FONT = {"0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
        "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "011", "001", "111"],
        "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
        "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
        "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
        ":": ["0", "1", "0", "1", "0"]}


class Grid:
    def __init__(self):
        self.px = [[None] * W for _ in range(H)]

    def dot(self, x: int, y: int, c: str | None) -> None:
        if 0 <= x < W and 0 <= y < H:
            self.px[y][x] = c

    def rect(self, x: int, y: int, w: int, h: int, c: str | None) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.dot(xx, yy, c)

    def box(self, x: int, y: int, w: int, h: int, fill: str, out: str) -> None:
        self.rect(x, y, w, h, out)
        self.rect(x + 1, y + 1, w - 2, h - 2, fill)

    def text(self, s: str, x: int, y: int, c: str) -> None:
        for ch in s:
            g = FONT[ch]
            for yy, row in enumerate(g):
                for xx, b in enumerate(row):
                    if b == "1":
                        self.dot(x + xx, y + yy, c)
            x += len(g[0]) + 1


def text_width(s: str) -> int:
    return sum(len(FONT[ch][0]) + 1 for ch in s) - 1


def fit_lines(text: str, width: int, max_lines: int, measure) -> str:
    """Wrap text to the bubble's width (measure = pixel width of a string in the bubble's font) and keep the LAST
    lines that fit, so live speech always shows its newest words and never spills out of the frame."""
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            while measure(word) > width and len(word) > 1:          # a word longer than the bubble: split it
                cut = len(word)
                while cut > 1 and measure((cur + " " if cur else "") + word[:cut]) > width:
                    cut -= 1
                if cur and measure(cur + " " + word[:cut]) > width:
                    lines.append(cur)
                    cur = ""
                    continue
                lines.append((cur + " " if cur else "") + word[:cut])
                cur, word = "", word[cut:]
            trial = (cur + " " + word) if cur else word
            if measure(trial) <= width:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        lines.append(cur)
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    keep = lines[-max_lines:]
    first = "…" + keep[0]
    while measure(first) > width and len(first) > 2:
        first = "…" + first[2:]
    return "\n".join([first] + keep[1:])


def bubble_font() -> tuple[str, int]:
    """A monospace font this system has: Consolas on Windows, Menlo on a Mac."""
    if sys.platform == "win32":
        return "Consolas", 10
    if sys.platform == "darwin":
        return "Menlo", 11
    return "Courier", 10


def mmss(sec: float) -> str:
    sec = max(0, int(sec))
    m = sec // 60
    return f"{m:02d}:{sec % 60:02d}" if m < 100 else f"{m}m"


def draw(snap: dict, t: float, timer_left: float | None = None) -> Grid:
    """t in seconds. timer_left: seconds left on the focus/break timer, counted down locally."""
    g = Grid()
    st = snap.get("state", "idle")
    ms = t * 1000
    bob = -1 if ((st == "talk" and int(ms / 240) % 2) or (st == "happy" and int(ms / 200) % 2)
                 or (st == "idle" and math.sin(ms / 700) > 0.7)) else 0
    Y = bob
    if st == "nap":
        ant = K["s"]
    elif st == "listen":
        ant = K["bl"] if int(ms / 150) % 2 else K["blue_soft"]
    elif st == "think":
        ant = (K["am"], K["co"], K["pu"])[int(ms / 250) % 3]
    elif st == "focus":
        ant = K["co"]
    elif st == "talk":
        ant = K["gl"] if int(ms / 200) % 2 else K["gls"]
    else:
        ant = K["am"] if int(ms / 1200) % 2 else K["amber_soft"]
    # antenna
    g.rect(23, 3 + Y, 2, 5, K["s"]); g.dot(23, 3 + Y, K["m"])
    g.box(21, 0 + Y, 6, 4, ant, K["o"]); g.dot(22, 1 + Y, K["white"])
    # head
    g.box(10, 7 + Y, 28, 22, K["b"], K["o"])
    for x, y in ((10, 7), (37, 7), (10, 28), (37, 28)):
        g.dot(x, y + Y, None)
    for x, y in ((11, 8), (36, 8), (11, 27), (36, 27)):
        g.dot(x, y + Y, K["o"])
    g.rect(12, 8 + Y, 22, 1, K["l"]); g.rect(11, 9 + Y, 1, 15, K["l"])
    g.rect(36, 10 + Y, 1, 17, K["m"]); g.rect(12, 26 + Y, 24, 2, K["m"])
    # ears
    for ex in (6, 37):
        g.box(ex, 13 + Y, 5, 9, K["m"], K["o"])
        for yy in (15, 17, 19):
            g.rect(ex + (1 if ex == 6 else 2), yy + Y, 2, 1, K["s"])
    # screen
    g.box(13, 10 + Y, 22, 15, K["scr"], K["o"]); g.rect(13, 10 + Y, 22, 1, K["s"])
    dim = st == "nap"
    for yy in range(11, 24, 2):
        g.rect(14, yy + Y, 20, 1, K["dim"] if dim else K["scr2"])
    g.dot(15, 12 + Y, K["glare"]); g.dot(16, 12 + Y, K["glare"]); g.dot(15, 13 + Y, K["glare"])
    g.rect(32, 27 + Y, 2, 1, K["s"]); g.dot(15, 27 + Y, K["co"]); g.dot(17, 27 + Y, K["am"])
    _face(g, snap, st, ms, Y, dim, timer_left)
    # neck + body
    g.rect(21, 29 + Y, 6, 2, K["s"]); g.rect(21, 29 + Y, 6, 1, K["o"])
    g.box(13, 30 + Y, 22, 10, K["b"], K["o"])
    g.rect(14, 31 + Y, 20, 1, K["l"]); g.rect(14, 38 + Y, 20, 1, K["m"]); g.rect(33, 32 + Y, 1, 6, K["m"])
    # chest panel: today's focus fuel + blinking LEDs
    g.box(16, 32 + Y, 16, 5, K["ink"], K["o"])
    fw = round(12 * max(0.0, min(1.0, snap.get("fuel", 0))))
    for yy in (33, 34):
        g.rect(17, yy + Y, 12, 1, K["fuelbg"]); g.rect(17, yy + Y, fw, 1, K["gr"])
    led = int(ms / 400) % 3
    g.dot(30, 33 + Y, K["co"] if led == 0 else "#5F2A18")
    g.dot(30, 34 + Y, K["am"] if led == 1 else "#5F4210")
    g.dot(30, 35 + Y, K["gl"] if led == 2 else "#1C4A3D")
    for xx in (17, 19, 21):
        g.dot(xx, 35 + Y, K["s"])
    # arms
    up = st == "happy" and int(ms / 300) % 2
    if up:
        g.box(7, 24 + Y, 5, 8, K["m"], K["o"]); g.box(5, 22 + Y, 4, 3, K["s"], K["o"])
        g.box(36, 24 + Y, 5, 8, K["m"], K["o"]); g.box(39, 22 + Y, 4, 3, K["s"], K["o"])
    else:
        wave = int(ms / 350) % 2 if st == "talk" else 0
        g.box(8, 31 + Y, 5, 8, K["m"], K["o"]); g.box(8, 38 + Y, 5, 3, K["s"], K["o"])
        g.box(35, 31 - wave * 2 + Y, 5, 8, K["m"], K["o"]); g.box(35, 38 - wave * 2 + Y, 5, 3, K["s"], K["o"])
    # feet
    g.box(15, 40, 7, 3, K["s"], K["o"]); g.box(26, 40, 7, 3, K["s"], K["o"])
    g.rect(16, 40, 5, 1, K["m"]); g.rect(27, 40, 5, 1, K["m"])
    if st == "nap":
        k = int(ms / 600) % 3
        for i, (x, y) in enumerate(((40, 6), (43, 3), (45, 0))):
            if i <= k:
                g.rect(x, y, 3, 1, K["pu"]); g.dot(x + 1, y + 1, K["pu"]); g.rect(x, y + 2, 3, 1, K["pu"])
    if st == "happy":
        on = int(ms / 250) % 2
        for i, (x, y) in enumerate(((3, 5), (44, 9), (44, 30), (2, 34))):
            if (i + on) % 2:
                for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
                    g.dot(x + dx, y + dy, K["am"])
    return g


def _eye(g: Grid, x: int, y: int, w: int, h: int, c: str) -> None:
    g.rect(x, y, w, h, c)
    g.dot(x, y, K["scr"]); g.dot(x + w - 1, y, K["scr"])


def _face(g: Grid, snap: dict, st: str, ms: float, Y: int, dim: bool, timer_left: float | None) -> None:
    gl = K["gl"]
    timer = snap.get("timer")
    if st == "focus" and timer:
        left = timer_left if timer_left is not None else timer.get("left", 0)
        s = mmss(left)
        g.rect(18, 13 + Y, 3, 1, gl); g.rect(27, 13 + Y, 3, 1, gl)          # focused squint
        g.text(s, 24 - math.ceil(text_width(s) / 2), 16 + Y, gl)
        total = timer.get("total") or 1
        done = 1 - max(0.0, min(1.0, left / total))
        g.rect(15, 22 + Y, 18, 1, K["gls"]); g.rect(15, 22 + Y, round(18 * done), 1, gl)
        return
    if st == "idle" and snap.get("mode") == "stay" and (ms % 9000) > 7000 and snap.get("time"):
        s = snap["time"]
        g.text(s, 24 - math.ceil(text_width(s) / 2), 15 + Y, K["am"])
        return
    if dim:
        g.rect(17, 17 + Y, 4, 1, K["gls"]); g.rect(27, 17 + Y, 4, 1, K["gls"])
        if timer:
            s = mmss(timer_left if timer_left is not None else timer.get("left", 0))
            g.text(s, 24 - math.ceil(text_width(s) / 2), 19 + Y, K["gls"])
        return
    if st == "happy":
        for x0 in (17, 27):
            g.dot(x0, 15 + Y, gl); g.dot(x0 + 1, 14 + Y, gl); g.dot(x0 + 2, 14 + Y, gl); g.dot(x0 + 3, 15 + Y, gl)
        g.rect(21, 19 + Y, 6, 1, gl); g.dot(20, 18 + Y, gl); g.dot(27, 18 + Y, gl)
        return
    if st == "think":
        _eye(g, 19, 13 + Y, 3, 3, gl); _eye(g, 29, 13 + Y, 3, 3, gl)
        n = int(ms / 120) % 16
        for i in range(4):
            g.dot(16 + (n + i) % 16, 20 + Y, gl if i == 3 else K["gls"])
        return
    big = st == "listen"
    blink = (ms % 3400) < 150
    if blink and not big:
        g.rect(17, 16 + Y, 4, 1, gl); g.rect(27, 16 + Y, 4, 1, gl)
    else:
        h, y0 = (6, 12) if big else (5, 13)
        _eye(g, 17, y0 + Y, 4, h, gl); _eye(g, 27, y0 + Y, 4, h, gl)
        g.dot(19, 14 + Y, K["hi"]); g.dot(29, 14 + Y, K["hi"])
    level = float(snap.get("level") or 0)
    if st == "listen":                                   # mouth = live mic meter
        for i in range(8):
            wob = abs(math.sin(ms / 90 + i * 1.7))
            h = 1 + round(min(3, level * 6 * (0.5 + wob)))
            g.rect(20 + i, 21 + Y - h, 1, h, gl if i % 2 else K["gls"])
        return
    if st == "talk":                                     # mouth opens with his voice
        o = 2 if level > 0.25 else (1 if level > 0.05 or int(ms / 140) % 2 else 0)
        g.rect(21, 20 + Y, 6, max(1, o), gl)
        if o == 2:
            g.rect(22, 22 + Y, 4, 1, K["gls"])
        return
    g.rect(22, 21 + Y, 4, 1, gl)


def to_photo_rows(g: Grid, scale: int, key: str) -> str:
    """Grid -> the string Tk's PhotoImage.put() takes, scaled up with crisp pixels."""
    rows = []
    for row in g.px:
        line = "{" + " ".join(c or key for c in row for _ in range(scale)) + "}"
        rows.extend([line] * scale)
    return " ".join(rows)


# ---------------- the window ----------------
class PetWindow:
    KEY = "#ff00fe"                                  # transparent color (never used in the art)

    def __init__(self, base: str, token: str, scale: int, pos_file: Path):
        import tkinter as tk
        self.tk = tk
        self.base, self.token, self.scale, self.pos_file = base, token, scale, pos_file
        self.snap: dict = {"show": False, "state": "idle"}
        self.got_at = time.time()
        self.fail_since: float | None = None
        self.alpha = 0.0
        self.visible = False
        self._last_rows = ""
        self._last_alpha = -1.0
        self._last_bubble = None
        self.sw, self.sh = W * scale, H * scale
        self.bw = max(220, self.sw + 120)            # window: speech bubble on top, robot below-right
        self.bh = 74
        self.ww, self.wh = self.bw + 8, self.bh + 10 + self.sh
        root = self.root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-transparentcolor", self.KEY)
        except tk.TclError:
            pass
        root.configure(bg=self.KEY)
        c = self.canvas = tk.Canvas(root, width=self.ww, height=self.wh, bg=self.KEY, highlightthickness=0)
        c.pack()
        self.img = tk.PhotoImage(width=self.sw, height=self.sh)
        sx, sy = self.ww - self.sw - 4, self.bh + 10
        c.create_image(sx, sy, image=self.img, anchor="nw")
        self.bub = c.create_rectangle(2, 2, self.bw, self.bh, fill="#F1EFE8", outline="#444441", width=3)
        tx = sx + self.sw // 2
        self.tail = c.create_polygon(tx - 10, self.bh, tx + 6, self.bh, tx, self.bh + 10,
                                     fill="#444441", outline="#444441")
        import tkinter.font as tkfont
        family, size = bubble_font()
        self.font = tkfont.Font(root=root, family=family, size=size)
        self.text_w = self.bw - 22
        self.max_lines = max(1, (self.bh - 22 - 6) // max(1, self.font.metrics("linespace")))
        self.who = c.create_text(12, 8, anchor="nw", fill="#5F5E5A", font=(family, size - 2))
        self.txt = c.create_text(12, 22, anchor="nw", fill="#2C2C2A", font=self.font, width=self.text_w + 4)
        self._place()
        self._drag = None
        c.bind("<ButtonPress-1>", self._drag_start)
        c.bind("<B1-Motion>", self._drag_move)
        c.bind("<ButtonRelease-1>", self._drag_end)
        menu = self.menu = tk.Menu(root, tearoff=0)
        menu.add_command(label="Pop-in (only when talking)", command=lambda: self._mode("popin"))
        menu.add_command(label="Stay on screen", command=lambda: self._mode("stay"))
        menu.add_command(label="Hide", command=lambda: self._mode("off"))
        c.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        root.withdraw()
        root.update_idletasks()
        self._no_focus()
        threading.Thread(target=self._poll, daemon=True).start()
        self._tick()

    def _no_focus(self) -> None:
        """Never steal keyboard focus, and no taskbar button."""
        if sys.platform != "win32":
            return
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x08000000, 0x80, 0x40000
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                            (style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)

    def _place(self) -> None:
        try:
            x, y = json.loads(self.pos_file.read_text(encoding="utf-8"))["pos"]
        except (OSError, ValueError, KeyError):
            x = self.root.winfo_screenwidth() - self.ww - 24
            y = self.root.winfo_screenheight() - self.wh - 64
        self.root.geometry(f"{self.ww}x{self.wh}+{int(x)}+{int(y)}")

    def _drag_start(self, e) -> None:
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e) -> None:
        if self._drag:
            self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drag_end(self, e) -> None:
        self._drag = None
        try:
            self.pos_file.parent.mkdir(parents=True, exist_ok=True)
            self.pos_file.write_text(json.dumps({"pos": [self.root.winfo_x(), self.root.winfo_y()]}), encoding="utf-8")
        except OSError:
            pass

    def _req(self, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body else None,
                                     method="POST" if body else "GET",
                                     headers={"X-Pakito-Token": self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read())

    def _mode(self, mode: str) -> None:
        try:
            self._req("/call", {"tool": "pet_mode", "args": {"mode": mode}, "source": "pet"})
        except Exception:
            pass

    def _poll(self) -> None:
        while True:
            try:
                snap = self._req("/pet")
                left = (snap.get("timer") or {}).get("left")
                self.snap, self.got_at, self.fail_since = snap, time.time(), None
                self.timer_left0 = left
            except Exception:
                self.fail_since = self.fail_since or time.time()
            time.sleep(0.1 if self.snap.get("conv") else 0.5)

    def _tick(self) -> None:
        now = time.time()
        if self.fail_since and now - self.fail_since > 15:      # Pakito is gone: so are we
            self.root.destroy()
            return
        snap = self.snap
        want = bool(snap.get("show"))
        self.alpha = min(1.0, self.alpha + 0.2) if want else max(0.0, self.alpha - 0.08)
        if snap.get("hide_now"):                         # a screenshot is about to be taken: vanish instantly
            self.alpha = 0.0
        if self.alpha > 0 and not self.visible:
            self.root.deiconify()
            self.root.lift()
            self.visible = True
        if self.alpha <= 0 and self.visible:
            self.root.withdraw()
            self.visible = False
        if self.visible:
            if self.alpha != self._last_alpha:
                self.root.attributes("-alpha", self.alpha)
                self._last_alpha = self.alpha
            left = getattr(self, "timer_left0", None)
            if left is not None:
                left = max(0.0, left - (now - self.got_at))
            rows = to_photo_rows(draw(snap, now, left), self.scale, self.KEY)
            if rows != self._last_rows:                  # most calm frames are identical: skip the costly upload
                self.img.put(rows, to=(0, 0))
                self._last_rows = rows
            self._bubble(snap)
        busy = snap.get("conv") or snap.get("state") in ("happy", "think", "talk", "listen") or 0 < self.alpha < 1
        self.root.after(80 if busy else 250, self._tick)

    def _bubble(self, snap: dict) -> None:
        text = (snap.get("text") or "").strip()
        note = snap.get("note") or ""
        if not text and snap.get("claude") and not snap.get("conv"):
            text, who = snap["claude"], "claude"
        else:
            who = {"you": "tú", "pakito": "pakito"}.get(snap.get("who"), snap.get("who") or "")
        if note:
            text = (text + "\n" if text else "") + note
        text = fit_lines(text, self.text_w, self.max_lines, self.font.measure)
        shown = bool(text) and (snap.get("conv") or snap.get("state") == "happy" or bool(snap.get("claude")))
        if (shown, who, text) == self._last_bubble:
            return
        self._last_bubble = (shown, who, text)
        st = "normal" if shown else "hidden"
        for item in (self.bub, self.tail, self.who, self.txt):
            self.canvas.itemconfigure(item, state=st)
        if shown:
            self.canvas.itemconfigure(self.who, text=who)
            self.canvas.itemconfigure(self.txt, text=text)


DEMO = [  # (seconds, what Pakito would send) — run-pakito.bat pet
    (2.5, {"state": "listen", "level": 0.5, "conv": True, "who": "you",
           "text": "pon música de estudio y empieza un bloque de física"}),
    (1.5, {"state": "think", "conv": True, "who": "you", "text": "pon música de estudio y empieza un bloque de física",
           "note": "⚙ play music"}),
    (3.0, {"state": "talk", "level": 0.4, "conv": True, "who": "pakito",
           "text": "Listo. Lofi en Spotify y bloque de 30 min para PHY 310."}),
    (5.0, {"state": "focus", "timer": {"left": 30 * 60, "total": 30 * 60, "label": "PHY 310"}}),
    (3.0, {"state": "nap", "timer": {"left": 6 * 60, "total": 0, "label": "break"}}),
    (3.0, {"state": "happy", "who": "pakito", "text": "Build lista: settings page en RabbitHole."}),
    (4.0, {"state": "idle", "mode": "stay"}),
]


def demo() -> None:
    """Shows every state on a loop without Pakito or Gemini. Right-click → Hide (or Ctrl+C here) to quit."""
    from datetime import datetime
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    t0 = time.time()
    total = sum(d for d, _ in DEMO)
    quit_ = threading.Event()

    def current() -> dict:
        t = (time.time() - t0) % total
        base = {"show": True, "mode": "stay", "fuel": 0.62, "time": datetime.now().strftime("%H:%M")}
        for dur, snap in DEMO:
            if t < dur:
                out = {**base, **snap}
                if "timer" in out:          # make the countdown actually move
                    out["timer"] = {**out["timer"], "left": max(0, out["timer"]["left"] - int(t))}
                return out
            t -= dur
        return base

    class Fake(BaseHTTPRequestHandler):
        def do_GET(self):
            b = json.dumps(current()).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
            self.wfile.write(b)

        def do_POST(self):                  # right-click menu: "Hide" ends the demo
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if (body.get("args") or {}).get("mode") == "off":
                quit_.set()
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    win = PetWindow(f"http://127.0.0.1:{srv.server_address[1]}", "demo", 3, Path.home() / ".pakibot_demo_pos.json")

    def watch():
        if quit_.is_set():
            win.root.destroy()
        else:
            win.root.after(200, watch)
    win.root.after(200, watch)
    print("Pakibot demo: listening → thinking → talking → focus timer → break → done → idle, on a loop.\n"
          "Drag him around. Right-click → Hide to quit.")
    win.root.mainloop()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
        return
    from .config import data_dir, device_path, load_config
    from .server import PORT, TOKEN_FILE
    # one Pakibot at a time: hold a local port as a lock for as long as this window lives
    import socket
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", PORT + 2))
    except OSError:
        return                                  # another Pakibot is already on screen
    cfg = load_config()
    pcfg = cfg.get("pet") or {}
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        print("Start Pakito first (it creates the token the pet needs).")
        return
    win = PetWindow(f"http://127.0.0.1:{PORT}", token, int(pcfg.get("scale", 3)),
                    device_path(cfg, data_dir(cfg) / "state" / "pet_pos.json"))    # each screen keeps its own spot
    win.root.mainloop()


if __name__ == "__main__":
    main()
