"""The Mac side of the Windows-only bits: notifications, opening things, the app list, a terminal, pasting, sounds,
and the hotkeys.

Permissions (System Settings → Privacy & Security), for the app you run Pakito from (Terminal, iTerm, VS Code):
  * Microphone: to hear you.
  * Accessibility + Input Monitoring: the hotkeys (Ctrl+Option to talk, Ctrl+Cmd to dictate) and pasting dictation.
  * Screen Recording: only for "mira esto".
macOS asks the first time each one is needed; if a hotkey does nothing, that's the permission.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

APP_DIRS = [Path("/Applications"), Path("/System/Applications"), Path("/System/Applications/Utilities"),
            Path.home() / "Applications"]
BROWSERS = ["Opera GX", "Opera", "Google Chrome", "Arc", "Brave Browser", "Microsoft Edge", "Vivaldi", "Firefox",
            "Safari"]
CHROMIUM = {"Opera GX", "Opera", "Google Chrome", "Arc", "Brave Browser", "Microsoft Edge", "Vivaldi"}


def _osa(script: str, timeout: float = 5) -> str:
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip()
    except Exception:
        return ""


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, msg: str) -> None:
    threading.Thread(target=_osa, args=(f"display notification {_q(msg[:240])} with title {_q(title[:80])}",),
                     daemon=True).start()


def launch(target: str) -> None:
    """URLs, URIs (spotify:, obsidian://), files and .app bundles all go through `open`; anything else runs as a
    shell command (e.g. 'code')."""
    if ":" in target or target.startswith("/") or target.endswith(".app"):
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(target, shell=True)


def open_with(app: str, target: str) -> None:
    subprocess.Popen(["open", "-a", app, target])


def apps() -> list[dict]:
    """Installed apps, same shape as the Windows Start-menu list: {"Name", "AppID"} (AppID = the .app path)."""
    out, seen = [], set()
    for d in APP_DIRS:
        try:
            for p in sorted(d.glob("*.app")):
                if p.stem not in seen:
                    seen.add(p.stem)
                    out.append({"Name": p.stem, "AppID": str(p)})
        except OSError:
            pass
    return out


def browsers() -> list[dict]:
    found = []
    for name in BROWSERS:
        for d in APP_DIRS[:1] + APP_DIRS[3:]:
            p = d / f"{name}.app"
            if p.exists():
                found.append({"name": name, "path": str(p), "chromium": name in CHROMIUM})
                break
    return found


def open_terminal(cmd: list[str], cwd: str) -> None:
    import shlex
    line = f"cd {shlex.quote(cwd)} && " + " ".join(shlex.quote(c) for c in cmd)
    _osa(f'tell application "Terminal"\nactivate\ndo script {_q(line)}\nend tell')


def frontmost() -> tuple[str, str]:
    """(window title, app name) of what you're looking at."""
    app = _osa('tell application "System Events" to get name of first application process whose frontmost is true')
    title = _osa('tell application "System Events" to tell (first application process whose frontmost is true) '
                 'to get name of front window') if app else ""
    return title, app


def clipboard_get() -> str | None:
    try:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None


def clipboard_set(text: str) -> bool:
    try:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=3, check=True)
        return True
    except Exception:
        return False


def paste(text: str) -> bool:
    """Type text where the cursor is: clipboard + Cmd+V, then put the old clipboard back."""
    time.sleep(0.15)                                   # let go of the hotkey first
    old = clipboard_get()
    if not clipboard_set(text):
        return False
    _osa('tell application "System Events" to keystroke "v" using command down')
    time.sleep(0.35)
    if old is not None:
        clipboard_set(old)
    return True


def play(wav_path: str) -> None:
    try:
        subprocess.Popen(["afplay", wav_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------- hotkeys ----------
def key_name(key) -> str | None:
    """pynput key -> the names HotkeyTracker uses. Option = alt, Cmd = windows (so Ctrl+Win dictation = Ctrl+Cmd)."""
    from pynput import keyboard as kb
    names = {kb.Key.ctrl: "ctrl", kb.Key.ctrl_l: "left ctrl", kb.Key.ctrl_r: "right ctrl",
             kb.Key.alt: "alt", kb.Key.alt_l: "left alt", kb.Key.alt_r: "right alt",
             kb.Key.shift: "shift", kb.Key.shift_l: "left shift", kb.Key.shift_r: "right shift",
             kb.Key.cmd: "windows", kb.Key.cmd_l: "left windows", kb.Key.cmd_r: "right windows",
             kb.Key.space: "space", kb.Key.tab: "tab", kb.Key.enter: "enter", kb.Key.esc: "esc"}
    if key in names:
        return names[key]
    name = getattr(key, "name", None)
    if name:                                           # f1..f20 and the rest
        return name.replace("_", " ")
    ch = getattr(key, "char", None)
    return ch.lower() if ch else None


class HotkeyListener:
    """One pynput listener for all of Pakito's hotkeys. trackers: [(HotkeyTracker, emit)]."""

    def __init__(self, trackers: list[tuple[object, Callable[[str], None]]]):
        self.trackers = trackers
        self.listener = None

    def _feed(self, key, down: bool) -> None:
        name = key_name(key)
        if not name:
            return
        for tracker, emit in self.trackers:
            what = tracker.feed(name, down)
            if what:
                emit(what)

    def start(self) -> None:
        from pynput import keyboard as kb
        self.listener = kb.Listener(on_press=lambda k: self._feed(k, True), on_release=lambda k: self._feed(k, False))
        self.listener.daemon = True
        self.listener.start()
