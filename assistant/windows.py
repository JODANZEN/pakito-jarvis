"""Windows by voice: open / close / minimize / maximize, move to screen 1 or 2, snap left or right, and layouts
("modo trabajo" puts each app on its screen). Plain Win32 calls, no screenshots: fast and always the same result.

Screen 1 is the main screen, then the others left to right ("screen_numbers": "windows" uses Windows' own numbers). Layouts live in this computer's config.local.json (each computer has its own monitors).

All the Win32 work goes through a small backend so the logic is testable anywhere; `FakeDesktop` in the tests
plays the part of Windows.
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

# what he says -> the program (exe stem); anything else matches the exe name or words in the window title
APP_ALIASES = {
    "vs code": "code", "vscode": "code", "visual studio code": "code", "visual": "code", "código": "code",
    "terminal": "windowsterminal", "la terminal": "windowsterminal", "consola": "windowsterminal",
    "powershell": "windowsterminal", "cmd": "windowsterminal",
    "explorador": "explorer", "file explorer": "explorer", "archivos": "explorer", "carpeta": "explorer",
    "opera": "opera", "opera gx": "opera", "navegador": "opera", "browser": "opera",
    "chrome": "chrome", "edge": "msedge", "firefox": "firefox",
    "claude": "claude", "spotify": "spotify", "obsidian": "obsidian", "discord": "discord",
    "whatsapp": "whatsapp", "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "unity": "unity",
    "notion": "notion", "teams": "ms-teams", "zoom": "zoom", "outlook": "olk",
}
HIDDEN_PROCS = {"textinputhost", "applicationframehost", "shellexperiencehost", "searchhost", "startmenuexperiencehost",
                "systemsettings", "lockapp", "nvidia overlay", "gamebar", "widgets"}


@dataclass
class Screen:
    n: int                       # Windows' own number (Identify)
    x: int
    y: int
    w: int
    h: int
    primary: bool = False

    def contains(self, cx: int, cy: int) -> bool:
        return self.x <= cx < self.x + self.w and self.y <= cy < self.y + self.h


@dataclass
class Win:
    hwnd: int
    title: str
    proc: str                    # exe stem, lowercase ("code", "spotify")
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)    # left, top, right, bottom
    minimized: bool = False
    maximized: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2


# ---------------------------------------------------------------- the real thing
class Win32:
    """The handful of Win32 calls Pakito needs, through ctypes (no pywin32)."""
    SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.c, self.wt = ctypes, wintypes
        self.u, self.k = ctypes.windll.user32, ctypes.windll.kernel32
        try:
            self.u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2: real pixels everywhere
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass
        try:
            self.dwm = ctypes.windll.dwmapi
        except OSError:
            self.dwm = None

    def screens(self) -> list[Screen]:
        c, wt = self.c, self.wt

        class MONITORINFOEXW(c.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                        ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32)]
        out: list[Screen] = []
        PROC = c.WINFUNCTYPE(c.c_int, wt.HMONITOR, wt.HDC, c.POINTER(wt.RECT), wt.LPARAM)

        def cb(hmon, hdc, rect, data):
            mi = MONITORINFOEXW()
            mi.cbSize = c.sizeof(MONITORINFOEXW)
            if self.u.GetMonitorInfoW(hmon, c.byref(mi)):
                m = re.search(r"(\d+)$", mi.szDevice)
                r = mi.rcWork
                out.append(Screen(int(m.group(1)) if m else len(out) + 1, r.left, r.top, r.right - r.left,
                                  r.bottom - r.top, bool(mi.dwFlags & 1)))
            return 1
        self.u.EnumDisplayMonitors(None, None, PROC(cb), 0)
        return out

    def _proc_name(self, hwnd: int) -> str:
        pid = self.wt.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, self.c.byref(pid))
        h = self.k.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = self.c.create_unicode_buffer(520)
            size = self.wt.DWORD(520)
            if self.k.QueryFullProcessImageNameW(h, 0, buf, self.c.byref(size)):
                return re.split(r"[\\/]", buf.value)[-1].lower().removesuffix(".exe")
            return ""
        finally:
            self.k.CloseHandle(h)

    def _cloaked(self, hwnd: int) -> bool:
        if not self.dwm:
            return False
        val = self.c.c_int(0)
        self.dwm.DwmGetWindowAttribute(hwnd, 14, self.c.byref(val), self.c.sizeof(val))   # DWMWA_CLOAKED
        return bool(val.value)

    def windows(self) -> list[Win]:
        """Real app windows, front to back (the first match is the one he used last)."""
        c, wt, u = self.c, self.wt, self.u
        out: list[Win] = []
        PROC = c.WINFUNCTYPE(c.c_bool, wt.HWND, wt.LPARAM)

        def cb(hwnd, _):
            if not u.IsWindowVisible(hwnd) or u.GetWindow(hwnd, 4) or self._cloaked(hwnd):   # GW_OWNER
                return True
            if u.GetWindowLongW(hwnd, -20) & 0x80:                                         # WS_EX_TOOLWINDOW
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if not n:
                return True
            buf = c.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            proc = self._proc_name(hwnd)
            if proc in HIDDEN_PROCS or (proc == "explorer" and buf.value in ("Program Manager", "")):
                return True
            r = wt.RECT()
            u.GetWindowRect(hwnd, c.byref(r))
            out.append(Win(int(hwnd), buf.value, proc, (r.left, r.top, r.right, r.bottom),
                           bool(u.IsIconic(hwnd)), bool(u.IsZoomed(hwnd))))
            return True
        u.EnumWindows(PROC(cb), 0)
        return out

    def foreground(self) -> int:
        return int(self.u.GetForegroundWindow() or 0)

    def show(self, hwnd: int, cmd: int) -> None:
        self.u.ShowWindow(hwnd, cmd)

    def move(self, hwnd: int, x: int, y: int, w: int, h: int) -> None:
        """Place the visible frame at x,y,w,h: Windows 10/11 windows have invisible resize borders around the
        frame, so add them back or snapped windows leave gaps."""
        dl = dt = dr = db = 0
        if self.dwm:
            r, f = self.wt.RECT(), self.wt.RECT()
            self.u.GetWindowRect(hwnd, self.c.byref(r))
            if self.dwm.DwmGetWindowAttribute(hwnd, 9, self.c.byref(f), self.c.sizeof(f)) == 0:  # EXTENDED_FRAME
                dl, dt, dr, db = f.left - r.left, f.top - r.top, r.right - f.right, r.bottom - f.bottom
                if not all(0 <= v <= 20 for v in (dl, dt, dr, db)):
                    dl = dt = dr = db = 0
        self.u.SetWindowPos(hwnd, 0, x - dl, y - dt, w + dl + dr, h + dt + db, 0x0004 | 0x0040)

    def focus(self, hwnd: int) -> None:
        """Bring a window forward with the keyboard focus in it. Windows only lets the program the user last typed
        into do that, so: borrow the front window's input first; if that's not enough, hold Alt (which unlocks it)
        with a dead key in between, so letting go of Alt doesn't open the app's menu bar. (Nov 22: a bare Alt tap
        opened the Claude app's menu and the paste went there instead of the message box.)"""
        u, k = self.u, self.k
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, self.SW_RESTORE)
        fg = u.GetForegroundWindow()
        if fg == hwnd:
            return
        me = k.GetCurrentThreadId()
        theirs = u.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(theirs and theirs != me and u.AttachThreadInput(theirs, me, True))
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(theirs, me, False)
        if u.GetForegroundWindow() != hwnd:
            u.keybd_event(0x12, 0, 0, 0)           # Alt down
            u.keybd_event(0xE8, 0, 0, 0)           # unassigned key: Alt won't count as "tapped" (AutoHotkey's trick)
            u.keybd_event(0xE8, 0, 2, 0)
            u.SetForegroundWindow(hwnd)
            u.keybd_event(0x12, 0, 2, 0)           # Alt up

    def close(self, hwnd: int) -> None:
        self.u.PostMessageW(hwnd, 0x0010, 0, 0)    # WM_CLOSE: the app still asks "save changes?" itself

    def keys(self, *vks: int) -> None:
        for vk in vks:
            self.u.keybd_event(vk, 0, 0, 0)
        for vk in reversed(vks):
            self.u.keybd_event(vk, 0, 2, 0)


# ---------------------------------------------------------------- the logic
POSITIONS = {"max": "max", "maximize": "max", "maximizar": "max", "full": "max", "completa": "max",
             "pantalla completa": "max", "left": "left", "izquierda": "left", "right": "right", "derecha": "right",
             "top": "top", "arriba": "top", "bottom": "bottom", "abajo": "bottom", "keep": "keep", "center": "center",
             "centro": "center"}


class Desktop:
    def __init__(self, backend=None, open_app: Callable[[str], dict] | None = None,
                 sleep: Callable[[float], None] = time.sleep, cancelled: Callable[[], bool] = lambda: False,
                 aliases: dict | None = None, numbering: str = "main_first"):
        self._backend = backend
        self.numbering = numbering
        self.open_app = open_app
        self.sleep = sleep
        self.cancelled = cancelled
        self.aliases = {**APP_ALIASES, **{k.lower(): v.lower() for k, v in (aliases or {}).items()}}

    @property
    def b(self):
        if self._backend is None:
            if sys.platform != "win32":
                raise RuntimeError("Window control works on Windows for now (the Mac is next).")
            self._backend = Win32()
        return self._backend

    # ---------- finding ----------
    def screens(self) -> list[Screen]:
        """Screen 1 = his main screen, then the rest left to right (Nov 23: Windows called his main screen 2).
        `"screen_numbers": "windows"` in config uses Windows' own numbers (Settings → Display → Identify) instead."""
        raw = self.b.screens()
        if self.numbering == "windows":
            return sorted(raw, key=lambda s: s.n)
        ordered = sorted(raw, key=lambda s: (not s.primary, s.x, s.y))
        return [Screen(i + 1, s.x, s.y, s.w, s.h, s.primary) for i, s in enumerate(ordered)]

    def screen(self, n: int | float | str | None) -> Screen | None:
        scr = self.screens()
        if n in (None, ""):
            return None
        try:
            n = int(float(n))
        except (TypeError, ValueError):
            return None
        return next((s for s in scr if s.n == n), None) or (scr[n - 1] if 0 < n <= len(scr) else None)

    def screen_of(self, w: Win) -> Screen | None:
        scr = self.screens()
        cx, cy = w.center
        return next((s for s in scr if s.contains(cx, cy)), None) or (scr[0] if scr else None)

    def find_all(self, ref: str | None) -> list[Win]:
        wins = self.b.windows()
        if ref is None or str(ref).strip().lower() in ("", "esta", "this", "esta ventana", "this window", "current",
                                                        "la actual", "front"):
            fg = self.b.foreground()
            return [w for w in wins if w.hwnd == fg][:1] or wins[:1]
        r = str(ref).strip().lower()
        proc = self.aliases.get(r) or self.aliases.get(r.removeprefix("el ").removeprefix("la "))
        if proc:
            hits = [w for w in wins if w.proc == proc]
            if hits:
                return hits
        hits = [w for w in wins if w.proc == r or w.proc == r.replace(" ", "")]
        if hits:
            return hits
        words = [x for x in re.findall(r"\w+", r) if len(x) > 2 and x not in ("the", "las", "los", "del", "ventana",
                                                                                "window", "app")]
        if not words:
            return []
        scored = [(sum(x in f"{w.title} {w.proc}".lower() for x in words) / len(words), w) for w in wins]
        best = max((s for s, _ in scored), default=0)
        return [w for s, w in scored if s == best and s >= 0.5]

    def find(self, ref: str | None) -> Win | None:
        hits = self.find_all(ref)
        return hits[0] if hits else None

    def listing(self) -> dict:
        wins = self.b.windows()
        fg = self.b.foreground()
        out = []
        for w in wins[:20]:
            s = self.screen_of(w)
            out.append({"app": w.proc, "title": w.title[:70], "screen": s.n if s else None,
                        **({"minimized": True} if w.minimized else {}), **({"in_front": True} if w.hwnd == fg else {})})
        return {"windows": out, "screens": [{"n": s.n, "size": f"{s.w}x{s.h}", **({"primary": True} if s.primary
                                                                                  else {})} for s in self.screens()]}

    # ---------- doing ----------
    def place(self, w: Win, screen: Screen | None, position: str = "max") -> dict:
        pos = POSITIONS.get(str(position or "max").lower(), "max")
        target = screen or self.screen_of(w)
        if target is None:
            return {"error": "No screens found."}
        if w.minimized or w.maximized:
            self.b.show(w.hwnd, Win32.SW_RESTORE)
        x, y, wd, ht = target.x, target.y, target.w, target.h
        if pos == "left":
            wd //= 2
        elif pos == "right":
            x, wd = x + wd // 2, wd - wd // 2
        elif pos == "top":
            ht //= 2
        elif pos == "bottom":
            y, ht = y + ht // 2, ht - ht // 2
        elif pos in ("keep", "center"):
            l, t, r, b = w.rect
            ow, oh = min(r - l, target.w), min(b - t, target.h)
            x, y, wd, ht = target.x + (target.w - ow) // 2, target.y + (target.h - oh) // 2, ow, oh
        self.b.move(w.hwnd, x, y, wd, ht)
        if pos == "max":
            self.b.show(w.hwnd, Win32.SW_MAXIMIZE)
        return {"moved": w.proc or w.title[:40], "screen": target.n, "position": pos}

    def act(self, action: str, app: str | None = None, screen=None, position: str | None = None,
            all_matching: bool = False, confirm: bool = False) -> dict:
        a = (action or "").lower().strip()
        if a in ("list", "listar", ""):
            return self.listing()
        if a == "screens":
            return {"screens": self.listing()["screens"]}
        hits = self.find_all(app)
        if not hits:
            return {"error": f"No open window like '{app}'.", "open_windows": sorted({w.proc for w in self.b.windows()})[:15]}
        w = hits[0]
        name = w.proc or w.title[:40]
        if a in ("focus", "show", "switch", "go"):
            self.b.focus(w.hwnd)
            self.sleep(0.25)                         # let it take the keyboard before anything gets typed
            return {"focused": name, "title": w.title[:70]}
        if a in ("minimize", "minimizar", "hide"):
            for x in (hits if all_matching else [w]):
                self.b.show(x.hwnd, Win32.SW_MINIMIZE)
            return {"minimized": name, "count": len(hits) if all_matching else 1}
        if a in ("maximize", "maximizar"):
            self.b.show(w.hwnd, Win32.SW_MAXIMIZE)
            return {"maximized": name}
        if a in ("restore", "restaurar", "unmaximize"):
            self.b.show(w.hwnd, Win32.SW_RESTORE)
            return {"restored": name}
        if a in ("close", "cerrar", "quit"):
            targets = hits if all_matching else [w]
            if len(targets) > 1 and not confirm:
                return {"confirm_first": f"Close {len(targets)} {name} windows?",
                        "hint": "ask him; call again with confirm=true only if he says yes"}
            for x in targets:
                self.b.close(x.hwnd)
            return {"closed": name, "count": len(targets), "note": "apps with unsaved work still ask to save"}
        if a in ("move", "mover", "snap", "put"):
            scr = self.screen(screen) if screen not in (None, "") else None
            if screen not in (None, "") and scr is None:
                return {"error": f"There's no screen {screen}.", "screens": [s.n for s in self.screens()]}
            return self.place(w, scr, position or ("max" if a != "snap" else "left"))
        return {"error": f"Unknown action '{action}'. Use focus, minimize, maximize, restore, close, move, list."}

    # ---------- layouts ----------
    def capture(self) -> list[dict]:
        """What's on screen now, as a layout: one entry per app (its front-most window)."""
        seen, out = set(), []
        for w in self.b.windows():
            if w.minimized or not w.proc or w.proc in seen or w.proc in ("explorer",):
                continue
            seen.add(w.proc)
            s = self.screen_of(w)
            if not s:
                continue
            l, t, r, b = w.rect
            if w.maximized:
                pos = "max"
            elif abs((r - l) - s.w / 2) < s.w * 0.08:
                pos = "left" if abs(l - s.x) < s.w * 0.08 else "right"
            else:
                pos = "keep"
            out.append({"app": w.proc, "screen": s.n, "position": pos})
        return out[:8]

    def apply(self, entries: list[dict], wait_s: float = 12) -> dict:
        done, missing = [], []
        for e in entries:
            if self.cancelled():
                return {"stopped": True, "done": done}
            app = e.get("app", "")
            w = self.find(app)
            if w is None and self.open_app:
                self.open_app(e.get("open") or app)
                for _ in range(int(wait_s / 0.5)):
                    if self.cancelled():
                        return {"stopped": True, "done": done}
                    self.sleep(0.5)
                    w = self.find(app)
                    if w:
                        self.sleep(0.6)                  # let it finish drawing before moving it
                        break
            if w is None:
                missing.append(app)
                continue
            r = self.place(w, self.screen(e.get("screen")), e.get("position", "max"))
            done.append(r.get("moved") or r.get("error"))
        return {"arranged": done, **({"not_found": missing} if missing else {})}
