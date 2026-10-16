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
from typing import Callable


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


# ---------- installed apps (the Start menu's list) ----------
def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def best_name_match(query: str, names: list[str], cutoff: float = 0.8) -> str | None:
    """Exact (ignoring case/spaces/dashes) > shortest name containing it > fuzzy ≥ cutoff."""
    from difflib import SequenceMatcher
    q = _norm(query)
    if not q:
        return None
    normed = [(n, _norm(n)) for n in names]
    exact = [n for n, k in normed if k == q]
    if exact:
        return exact[0]
    contains = [n for n, k in normed if q in k and len(q) >= 3]
    if contains:
        return min(contains, key=len)
    scored = [(SequenceMatcher(None, q, k).ratio(), n) for n, k in normed]
    score, name = max(scored, default=(0.0, None))
    return name if score >= cutoff else None


def _get_start_apps() -> list[dict]:
    if sys.platform != "win32":
        return []
    p = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=20, creationflags=0x08000000)        # CREATE_NO_WINDOW
    data = json.loads(p.stdout or "[]")
    return data if isinstance(data, list) else [data]


class StartApps:
    """Every app Windows knows (same list as the Start menu), so "abre <any app>" works.
    Loaded once in the background (PowerShell takes ~1-2 s); reloaded if a name isn't found."""

    def __init__(self, loader: Callable[[], list[dict]] = _get_start_apps, launch=None):
        self.loader = loader
        self.launch = launch or launch_app_quietly
        self.apps: dict[str, str] | None = None
        self._lock = threading.Lock()

    def load(self) -> dict[str, str]:
        with self._lock:
            try:
                self.apps = {a["Name"]: a["AppID"] for a in self.loader() if a.get("Name") and a.get("AppID")}
            except Exception:
                self.apps = self.apps or {}
            return self.apps

    def preload(self) -> None:
        threading.Thread(target=self.load, daemon=True).start()

    def find(self, name: str) -> tuple[str, str] | None:
        """-> (display name, AppID) or None."""
        apps = self.apps if self.apps is not None else self.load()
        hit = best_name_match(name, list(apps))
        if not hit:                              # maybe installed since we loaded
            apps = self.load()
            hit = best_name_match(name, list(apps))
        return (hit, apps[hit]) if hit else None

    def open(self, name: str) -> str | None:
        found = self.find(name)
        if not found:
            return None
        self.launch("shell:AppsFolder\\" + found[1])
        return found[0]


def open_terminal(cmd: list[str], cwd: str) -> None:
    """Run cmd in a NEW console window (Windows Terminal hosts it if that's your default terminal).
    `cmd /k` keeps the window open after the program exits."""
    if sys.platform == "win32":
        subprocess.Popen(["cmd", "/k", *cmd], cwd=cwd, creationflags=0x00000010)   # CREATE_NEW_CONSOLE
    else:
        subprocess.Popen(cmd, cwd=cwd)


def open_in_editor(paths: list[str]) -> None:
    """Open files in VS Code (the review + diff); falls back to the default app if `code` isn't on PATH."""
    import shutil
    if shutil.which("code"):
        if sys.platform == "win32":
            subprocess.Popen(["cmd", "/c", "code", *paths], creationflags=0x08000000)
        else:
            subprocess.Popen(["code", *paths])
        return
    for p in paths:
        if sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        else:
            webbrowser.open(Path(p).as_uri())
