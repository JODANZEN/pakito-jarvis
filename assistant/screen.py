"""'Mira esto': one screenshot, only when you ask, so Pakito (or Claude) can see what you're looking at.

  window  — the window in front (default)
  screen  — the whole monitor that window is on
  region  — you drag a box around just the part you mean (Esc cancels)

Nothing is recorded or kept: the image goes to Gemini in that one conversation and is dropped. A Claude
"explain properly" job gets a temporary copy that's deleted when Claude finishes.
Refuses while you're on a call or when a password manager / Windows security prompt is in front.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from pathlib import Path

MAX_SIDE = 1600                  # long side in px: sharp enough for equations and code, small enough to send fast
SENSITIVE = ("1password", "bitwarden", "keepass", "lastpass", "dashlane", "keeper", "nordpass", "proton pass",
             "credential", "windows security", "password", "contraseña", "authenticator", "online banking",
             "banco", "paypal")


def foreground() -> tuple[str, str, tuple[int, int, int, int] | None]:
    """-> (window title, process name, (left, top, right, bottom)) of the window in front."""
    if sys.platform != "win32":
        return "", "", None
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)          # real pixels on scaled displays
    except Exception:
        pass
    hwnd = u.GetForegroundWindow()
    n = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = ""
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid.value)            # PROCESS_QUERY_LIMITED_INFORMATION
        name = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if k.QueryFullProcessImageNameW(h, 0, name, ctypes.byref(size)):
            proc = Path(name.value).name
        k.CloseHandle(h)
    except Exception:
        pass
    rect = wintypes.RECT()
    try:                                                         # the visible frame, without the invisible shadow
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
    except Exception:
        u.GetWindowRect(hwnd, ctypes.byref(rect))
    box = (rect.left, rect.top, rect.right, rect.bottom)
    return buf.value, proc, box if box[2] - box[0] > 50 and box[3] - box[1] > 50 else None


def is_sensitive(title: str, proc: str) -> bool:
    t = f"{title} {proc}".lower()
    return any(s in t for s in SENSITIVE)


def monitor_of(box: tuple[int, int, int, int] | None) -> tuple[int, int, int, int] | None:
    if sys.platform != "win32" or not box:
        return None
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]
    pt = wintypes.POINT((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    mon = ctypes.windll.user32.MonitorFromPoint(pt, 2)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    r = mi.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def grab(box: tuple[int, int, int, int] | None):
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=box, all_screens=True)


def to_jpeg(img, max_side: int = MAX_SIDE, quality: int = 82) -> bytes:
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        f = max_side / max(w, h)
        img = img.resize((round(w * f), round(h * f)))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def select_region(timeout: float = 60) -> tuple[int, int, int, int] | None:
    """Runs the drag-a-box overlay in its own process (Tk can't share Pakito's thread). None = cancelled."""
    try:
        p = subprocess.run([sys.executable, "-m", "assistant.screen", "select"], capture_output=True, text=True,
                           timeout=timeout, cwd=str(Path(__file__).resolve().parent.parent))
        box = json.loads((p.stdout or "null").strip().splitlines()[-1])
        return tuple(box) if box else None
    except Exception:
        return None


def _overlay() -> None:
    """python -m assistant.screen select -> prints [l, t, r, b] or null."""
    import tkinter as tk
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.25)
    vx, vy = root.winfo_vrootx(), root.winfo_vrooty()
    w, h = root.winfo_vrootwidth() or root.winfo_screenwidth(), root.winfo_vrootheight() or root.winfo_screenheight()
    if sys.platform == "win32":
        import ctypes
        u = ctypes.windll.user32
        vx, vy, w, h = u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79)
    root.geometry(f"{w}x{h}+{vx}+{vy}")
    c = tk.Canvas(root, bg="black", highlightthickness=0, cursor="crosshair")
    c.pack(fill="both", expand=True)
    c.create_text(w // 2, 40, text="Drag a box around what you mean · Esc to cancel", fill="white",
                  font=("Segoe UI", 16))
    st = {"x": 0, "y": 0, "r": None, "box": None}

    def down(e):
        st["x"], st["y"] = e.x, e.y
        st["r"] = c.create_rectangle(e.x, e.y, e.x, e.y, outline="#5DCAA5", width=3, fill="white")

    def move(e):
        if st["r"]:
            c.coords(st["r"], st["x"], st["y"], e.x, e.y)

    def up(e):
        x0, x1 = sorted((st["x"], e.x))
        y0, y1 = sorted((st["y"], e.y))
        if x1 - x0 > 10 and y1 - y0 > 10:
            st["box"] = [x0 + vx, y0 + vy, x1 + vx, y1 + vy]
        root.destroy()

    c.bind("<ButtonPress-1>", down)
    c.bind("<B1-Motion>", move)
    c.bind("<ButtonRelease-1>", up)
    root.bind("<Escape>", lambda e: root.destroy())
    root.focus_force()
    root.mainloop()
    print(json.dumps(st["box"]))


class ScreenLook:
    """Takes the screenshot for the look_at_screen tool. Everything OS-specific is injectable for tests."""

    def __init__(self, pet=None, fg=foreground, grabber=grab, selector=select_region, sleep=time.sleep):
        self.pet, self.fg, self.grabber, self.selector, self.sleep = pet, fg, grabber, selector, sleep

    def capture(self, area: str = "window") -> dict:
        """-> {"jpeg": bytes, "title": str, "size": (w, h)} or {"error": ...}"""
        if self.pet is not None and getattr(self.pet, "on_call", False):
            return {"error": "You're on a call, so I don't take screenshots right now."}
        title, proc, box = self.fg()
        if is_sensitive(title, proc):
            return {"error": "That looks like a password or security window, so I won't screenshot it."}
        if self.pet is not None:
            self.pet.hide_for(2.0)                    # keep Pakibot out of the picture
            self.sleep(0.45)
        if area == "region":
            box = self.selector()
            if not box:
                return {"error": "Cancelled: no box drawn."}
        elif area == "screen":
            box = monitor_of(box) or None
        try:
            img = self.grabber(box)
        except Exception as e:
            return {"error": f"Couldn't take the screenshot: {e}"}
        jpeg = to_jpeg(img)
        return {"jpeg": jpeg, "title": title or proc, "size": img.size}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "select":
        _overlay()
