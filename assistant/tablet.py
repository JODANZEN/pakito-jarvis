"""The tablet (Galaxy Tab S11 Ultra): Pakito takes a screenshot of it on demand, so a problem he solved by hand in
Samsung Notes goes to Pakito, ChatGPT or Claude without phone photo → WhatsApp → download → upload.

It uses adb (Android's USB debugging tool) over the USB-C cable, or over Wi-Fi once paired. One-time setup:
  1. Tablet: Settings → About tablet → Software information → tap "Build number" 7 times (Developer options).
     Then Settings → Developer options → USB debugging: on.
  2. PC: `winget install Google.PlatformTools` (Mac: `brew install android-platform-tools`), reopen Pakito.
  3. Plug the cable in, unlock the tablet and tap Allow on "Allow USB debugging?" (tick "Always allow").
`run-pakito.bat check tablet` tells you which step is missing.
"""
from __future__ import annotations

import glob
import io
import os
import shutil
import subprocess
import sys
from typing import Callable

PNG = b"\x89PNG\r\n\x1a\n"
INSTALL = ("Install Android's adb: PowerShell `winget install Google.PlatformTools` (Mac: `brew install "
           "android-platform-tools`), then restart Pakito.")


def adb_candidates() -> list[str]:
    home, local = os.path.expanduser("~"), os.environ.get("LOCALAPPDATA", "")
    pats = [os.path.join(local, "Android", "Sdk", "platform-tools", "adb.exe"),
            os.path.join(local, "Microsoft", "WinGet", "Packages", "Google.PlatformTools*", "platform-tools",
                         "adb.exe"),
            os.path.join(local, "Microsoft", "WinGet", "Links", "adb.exe"),
            r"C:\platform-tools\adb.exe", os.path.join(home, "platform-tools", "adb.exe"),
            os.path.join(home, "Library", "Android", "sdk", "platform-tools", "adb"),
            "/opt/homebrew/bin/adb", "/usr/local/bin/adb"]
    out = []
    for p in pats:
        out += sorted(glob.glob(p)) if "*" in p else [p]
    return out


class Tablet:
    def __init__(self, cfg: dict | None = None, run: Callable = subprocess.run, which: Callable = shutil.which,
                 exists: Callable[[str], bool] = os.path.exists):
        self.cfg = cfg or {}
        self.run, self.which, self.exists = run, which, exists

    def adb(self) -> str | None:
        if self.cfg.get("adb") and self.exists(self.cfg["adb"]):
            return self.cfg["adb"]
        return self.which("adb") or next((p for p in adb_candidates() if self.exists(p)), None)

    def _adb(self, *args, timeout: float = 10, binary: bool = False):
        exe = self.adb()
        if not exe:
            return None
        serial = ["-s", self.cfg["serial"]] if self.cfg.get("serial") else []
        kw = {"capture_output": True, "timeout": timeout}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000                    # no console window flashing up
        if not binary:
            kw.update(text=True, encoding="utf-8", errors="replace")
        return self.run([exe, *serial, *args], **kw)

    def status(self) -> dict:
        """Which setup step is missing, in words he can act on."""
        if not self.adb():
            return {"ok": False, "error": INSTALL}
        try:
            p = self._adb("devices", timeout=8)
        except Exception as e:
            return {"ok": False, "error": f"adb didn't run: {e}"}
        rows = [line.split("\t") for line in (p.stdout or "").splitlines()[1:] if "\t" in line]
        if not rows:
            return {"ok": False, "error": "No tablet found. Plug in the USB-C cable (or pair wireless debugging) "
                                          "and turn on USB debugging in Developer options."}
        states = {state.strip() for _, state in rows}
        if "device" in states:
            return {"ok": True, "devices": [s for s, st in rows if st.strip() == "device"]}
        if "unauthorized" in states:
            return {"ok": False, "error": "The tablet is connected but hasn't allowed this PC: unlock it and tap "
                                          "Allow on 'Allow USB debugging?' (tick Always allow)."}
        return {"ok": False, "error": f"The tablet is {', '.join(sorted(states))}: unplug it and plug it back in."}

    def screenshot(self) -> dict:
        """-> {"png": bytes} or {"error": ...}."""
        if not self.adb():
            return {"error": INSTALL}
        try:
            p = self._adb("exec-out", "screencap", "-p", timeout=15, binary=True)
        except subprocess.TimeoutExpired:
            return {"error": "The tablet took too long to answer. Is it unlocked?"}
        except Exception as e:
            return {"error": f"adb failed: {e}"}
        if p.returncode != 0 or not (p.stdout or b"").startswith(PNG):
            st = self.status()
            return {"error": st.get("error") or "The tablet didn't send a screenshot (is the screen on?)."}
        return {"png": p.stdout}


def prepare(png: bytes, max_side: int = 1600) -> dict:
    """PNG -> a JPEG for Gemini (smaller) + the PIL image for the clipboard."""
    from PIL import Image
    from .screen import to_jpeg
    img = Image.open(io.BytesIO(png)).convert("RGB")
    return {"image": img, "jpeg": to_jpeg(img, max_side=max_side), "size": img.size}


def save_shot(png: bytes, folder: str | None = None) -> str | None:
    """Keep each screenshot (Pictures/Pakito tablet/<date time>.png) so nothing is lost if the clipboard is
    overwritten."""
    from datetime import datetime
    from pathlib import Path
    d = Path(os.path.expanduser(folder)) if folder else Path.home() / "Pictures" / "Pakito tablet"
    try:
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{datetime.now():%Y-%m-%d %H.%M.%S}.png"
        f.write_bytes(png)
        return str(f)
    except OSError:
        return None


def clipboard_image(img) -> bool:
    """Put a picture on the clipboard (so Ctrl+V pastes it into ChatGPT / Claude)."""
    if sys.platform == "darwin":
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "pakito-tablet.png")
        img.save(path, "PNG")
        r = subprocess.run(["osascript", "-e", f'set the clipboard to (read (POSIX file "{path}") as «class PNGf»)'],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    if sys.platform != "win32":
        return False
    import ctypes
    buf = io.BytesIO()
    img.save(buf, "BMP")
    dib = buf.getvalue()[14:]                                  # a BMP file minus its header = CF_DIB
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    for _ in range(10):
        if u.OpenClipboard(None):
            break
        import time
        time.sleep(0.05)
    else:
        return False
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(0x0002, len(dib))                    # GMEM_MOVEABLE
        p = k.GlobalLock(h)
        ctypes.memmove(p, dib, len(dib))
        k.GlobalUnlock(h)
        return bool(u.SetClipboardData(8, h))                   # CF_DIB
    finally:
        u.CloseClipboard()
