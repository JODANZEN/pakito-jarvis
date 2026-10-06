"""Small shared pieces: JSONL log, notifier, launcher."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path


def now_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


class JsonlLog:
    """Append-only JSON-lines file. One event per line, easy to load in pandas."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # a half-written line from a crash shouldn't kill the stats
        return out


def make_notifier():
    """Windows toast if winotify is installed, otherwise print. Returns notify(title, msg)."""
    if sys.platform == "win32":
        try:
            from winotify import Notification, audio  # type: ignore

            def notify(title: str, msg: str) -> None:
                n = Notification(app_id="Pakito", title=title, msg=msg)
                n.set_audio(audio.Default, loop=False)
                n.show()
                print(f"[{title}] {msg}")

            return notify
        except ImportError:
            pass

    def notify(title: str, msg: str) -> None:
        print(f"\a[{title}] {msg}")

    return notify


def find_browser(setting: str | None) -> tuple[str | None, str]:
    """-> (exe path or None for the system default, display name)."""
    if not setting or setting == "default":
        return None, "default"
    if setting != "auto":
        return (setting, Path(setting).stem) if Path(setting).exists() else (None, "default")
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    for exe in (local / "Programs" / "Opera GX" / "opera.exe",
                local / "Programs" / "Opera GX" / "launcher.exe",
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Opera GX" / "opera.exe"):
        if exe.exists():
            return str(exe), "Opera GX"
    return None, "default"


def make_launcher(browser_exe: str | None):
    """launch() that sends http(s) links to a specific browser; everything else as usual."""
    if not browser_exe:
        return launch

    def launch_with_browser(target: str) -> None:
        if target.startswith(("http://", "https://", "file:")):     # PDFs too, so #page=N works
            subprocess.Popen([browser_exe, target])
        else:
            launch(target)

    return launch_with_browser


def file_uri(path: str, page: int | None = None) -> str:
    from pathlib import PureWindowsPath
    if path.startswith("file:"):
        uri = path
    elif len(path) > 2 and path[1] == ":":                  # C:\... (works on any OS)
        uri = PureWindowsPath(path).as_uri()
    else:
        uri = Path(path).resolve().as_uri()
    return uri + (f"#page={int(page)}" if page else "")


def launch_app_quietly(target: str) -> None:
    """Start a Store app by AUMID (shell:AppsFolder\\...) — used to bring Phone Link back after lock-in."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer.exe", target])


def launch(target: str) -> None:
    """Open a URL/URI with the default handler, or run a command (e.g. 'code')."""
    if target.startswith("file:") and "#" in target:
        target = target.split("#", 1)[0]       # the default PDF app can't take a page anchor
    is_uri = ":" in target and not os.path.isabs(target)
    if sys.platform == "win32":
        if is_uri or os.path.exists(target):
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(target, shell=True)
    else:
        if is_uri:
            webbrowser.open(target)
        else:
            subprocess.Popen(target, shell=True)
