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
# Polled ~100x/s from macOS's own key state (Quartz, through ctypes), like the Windows KeyPoller. No keyboard library:
# pynput segfaulted on recent macOS. Modifier-only hotkeys (Ctrl+Option, Ctrl+Cmd) don't need any permission to
# read; telling a shortcut (Ctrl+Option+A) apart from talking uses Input Monitoring if it's allowed.
_CG = None
FLAGS = {"ctrl": 1 << 18, "alt": 1 << 19, "shift": 1 << 17, "windows": 1 << 20,          # either side
         "left ctrl": 0x1, "right ctrl": 0x2000, "left shift": 0x2, "right shift": 0x4,
         "left alt": 0x20, "right alt": 0x40, "left windows": 0x8, "right windows": 0x10}
# Windows virtual-key codes (what KeyPoller asks about) -> Mac key codes
VK_TO_MAC = {0x08: 51, 0x09: 48, 0x0D: 36, 0x1B: 53, 0x20: 49, 0x2E: 117, 0x25: 123, 0x26: 126, 0x27: 124,
             0x28: 125, **{0x70 + i: k for i, k in enumerate([122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111])}}
MAC_KEYS = {"A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7, "C": 8, "V": 9, "B": 11, "Q": 12,
            "W": 13, "E": 14, "R": 15, "Y": 16, "T": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
            "9": 25, "7": 26, "8": 28, "0": 29, "O": 31, "U": 32, "I": 34, "P": 35, "L": 37, "J": 38, "K": 40,
            "N": 45, "M": 46}
VK_TO_MAC.update({ord(ch): code for ch, code in MAC_KEYS.items()})
NAME_TO_MAC = {"space": 49, "tab": 48, "enter": 36, "esc": 53, **{f"f{i + 1}": k for i, k in enumerate(
    [122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111])}}


def _cg():
    global _CG
    if _CG is None:
        import ctypes
        lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        lib.CGEventSourceFlagsState.restype = ctypes.c_uint64
        lib.CGEventSourceFlagsState.argtypes = [ctypes.c_int32]
        lib.CGEventSourceKeyState.restype = ctypes.c_bool
        lib.CGEventSourceKeyState.argtypes = [ctypes.c_int32, ctypes.c_uint16]
        _CG = lib
    return _CG


def flags() -> int:
    return int(_cg().CGEventSourceFlagsState(0))          # 0 = combined session state


def key_down(mac_code: int) -> bool:
    return bool(_cg().CGEventSourceKeyState(0, mac_code))


def pressed(part: str, read_flags: Callable[[], int] = None, read_key: Callable[[int], bool] = None):
    """Is this hotkey part down right now? True/False, or None if we can't tell."""
    read_flags, read_key = read_flags or flags, read_key or key_down
    if part in FLAGS:
        return bool(read_flags() & FLAGS[part])
    code = NAME_TO_MAC.get(part) or (VK_TO_MAC.get(ord(part.upper())) if len(part) == 1 else None)
    return None if code is None else read_key(code)


def make_poller(hotkey: str, emit: Callable[[str], None]):
    """A KeyPoller (the Windows one: debounced, cancels on Ctrl+Option+<key>) reading the Mac's key state."""
    from .voice import KeyPoller
    poller = KeyPoller(hotkey, emit, pressed=pressed)
    poller.vk_down = lambda vk: key_down(VK_TO_MAC[vk]) if vk in VK_TO_MAC else False
    return poller


def trusted() -> bool:
    """Accessibility permission: needed to paste dictation and close blocked tabs (not for the hotkeys)."""
    try:
        import ctypes
        ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(ax.AXIsProcessTrusted())
    except Exception:
        return True
