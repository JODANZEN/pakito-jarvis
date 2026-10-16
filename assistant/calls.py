"""Notice when another app is using the mic (Zoom, Meet in Opera GX, WhatsApp, Phone Link calls...).

Windows records every app's mic use for the privacy icon in the taskbar, under
HKCU\\...\\CapabilityAccessManager\\ConsentStore\\microphone. An app whose LastUsedTimeStop is 0 is
using the mic right now. Pakito reads that once a second: while anything else has the mic, it hangs up
and leaves the mic alone until the call ends.

    run-pakito.bat mic      shows live who's using the mic (to find an app to ignore)
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Callable

MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
EPOCH_DIFF = 11644473600          # seconds between 1601 (FILETIME) and 1970


def _filetime(v: int) -> float:
    return v / 1e7 - EPOCH_DIFF if v else 0.0


def read_registry() -> list[tuple[str, float, float]]:
    """-> [(app key, start ts, stop ts)] for every app that ever used the mic. stop == 0: using it now."""
    if sys.platform != "win32":
        return []
    import winreg
    out = []

    def scan(path: str, prefix: str = "") -> None:
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return
        with root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if name == "NonPackaged":
                    continue
                try:
                    with winreg.OpenKey(root, name) as k:
                        start = winreg.QueryValueEx(k, "LastUsedTimeStart")[0]
                        stop = winreg.QueryValueEx(k, "LastUsedTimeStop")[0]
                except OSError:
                    continue
                out.append((prefix + name, _filetime(start), _filetime(stop)))

    scan(MIC_KEY)                                   # Store apps: WhatsApp, Phone Link, Teams...
    scan(MIC_KEY + r"\NonPackaged", "#")            # normal programs: Zoom, Opera GX, Discord...
    return out


def friendly(key: str) -> str:
    if key.startswith("#"):                         # "#C:#Program Files#Zoom#bin#Zoom.exe"
        return key.rsplit("#", 1)[-1]
    return key.split("_", 1)[0].split(".")[-1]      # "5319275A.WhatsAppDesktop_cv1g..." -> WhatsAppDesktop


def _is_own(key: str, own_prefix: str) -> bool:
    return key.startswith("#") and key[1:].replace("#", "\\").lower().startswith(own_prefix)


def mic_users(entries: list[tuple[str, float, float]], now: float, own_prefix: str,
              ignore: list[str] = (), stale_hours: float = 12) -> list[str]:
    """Apps other than Pakito using the mic right now. A 'using it' mark older than stale_hours is
    ignored (an app that crashed mid-call never writes its stop time)."""
    ign = [i.lower() for i in ignore]
    users = []
    for key, start, stop in entries:
        if stop or not start or now - start > stale_hours * 3600:
            continue
        if _is_own(key, own_prefix):
            continue
        name = friendly(key)
        if any(i in key.lower() or i == name.lower() for i in ign):
            continue
        users.append(name)
    return sorted(set(users))


class CallWatcher:
    """Polls once a second; calls on_change(on_call: bool, apps: list[str]) when a call starts/ends."""

    def __init__(self, on_change: Callable[[bool, list[str]], None], ignore: list[str] = (),
                 reader: Callable[[], list] = read_registry, clock: Callable[[], float] = time.time,
                 own_prefix: str | None = None, interval: float = 1.0):
        self.on_change = on_change
        self.ignore = list(ignore)
        self.reader = reader
        self.clock = clock
        # everything under Pakito's Python folder is Pakito itself
        self.own_prefix = (own_prefix or str(Path(sys.prefix))).lower().rstrip("\\") + "\\"
        self.interval = interval
        self.apps: list[str] = []
        self._stop = threading.Event()

    @property
    def on_call(self) -> bool:
        return bool(self.apps)

    def poll(self) -> None:
        try:
            apps = mic_users(self.reader(), self.clock(), self.own_prefix, self.ignore)
        except Exception:
            return
        was = self.on_call
        self.apps = apps
        if self.on_call != was:
            self.on_change(self.on_call, apps)

    def start(self) -> "CallWatcher":
        def run():
            while not self._stop.wait(self.interval):
                self.poll()
        self.poll()
        threading.Thread(target=run, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()


def main() -> None:
    """run-pakito.bat mic"""
    print("Who's using the mic (Ctrl+C to quit). Start a call to see it show up.\n")
    w = CallWatcher(lambda on, apps: None)
    last = None
    try:
        while True:
            w.poll()
            if w.apps != last:
                print(time.strftime("%H:%M:%S"), "  " + (", ".join(w.apps) if w.apps else "nobody (Pakito ignored)"),
                      flush=True)
                last = w.apps
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
