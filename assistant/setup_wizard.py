"""First-time setup: run-pakito.bat setup (setup-pakito.bat runs it after installing).

Walks through everything Pakito needs, one question at a time, and saves your answers in
assistant/config.local.json (private, never committed). Re-run it any time: Enter keeps what you have.

  1. You: name, one line about you, your city
  2. Gemini key (free) — opens the page and checks the key works
  3. Where Pakito keeps notes: a plain folder, or your Obsidian vault if you have one
  4. Browser for links (and for the lock-in site blocker)
  5. Microphone, with a 3-second level test
  6. Spotify (Premium lets Pakito press play; without it, it opens Spotify search)
  7. Claude Code (optional: hard questions and background builds)
"""
from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import Callable

HERE = Path(__file__).parent
LOCAL = HERE / "config.local.json"
KEY_PAGE = "https://aistudio.google.com/apikey"

BROWSERS = [  # (name, path relative to a base folder, chromium = can use the lock-in extension)
    ("Opera GX", ("LOCALAPPDATA", r"Programs\Opera GX\opera.exe"), True),
    ("Opera", ("LOCALAPPDATA", r"Programs\Opera\opera.exe"), True),
    ("Chrome", ("ProgramFiles", r"Google\Chrome\Application\chrome.exe"), True),
    ("Chrome", ("ProgramFiles(x86)", r"Google\Chrome\Application\chrome.exe"), True),
    ("Chrome", ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"), True),
    ("Edge", ("ProgramFiles(x86)", r"Microsoft\Edge\Application\msedge.exe"), True),
    ("Edge", ("ProgramFiles", r"Microsoft\Edge\Application\msedge.exe"), True),
    ("Brave", ("ProgramFiles", r"BraveSoftware\Brave-Browser\Application\brave.exe"), True),
    ("Brave", ("LOCALAPPDATA", r"BraveSoftware\Brave-Browser\Application\brave.exe"), True),
    ("Vivaldi", ("LOCALAPPDATA", r"Vivaldi\Application\vivaldi.exe"), True),
    ("Firefox", ("ProgramFiles", r"Mozilla Firefox\firefox.exe"), False),
    ("Firefox", ("ProgramFiles(x86)", r"Mozilla Firefox\firefox.exe"), False),
]
DEFAULT_PROGIDS = {"opera": "Opera", "chrome": "Chrome", "msedge": "Edge", "brave": "Brave", "firefox": "Firefox",
                   "vivaldi": "Vivaldi", "appxq0fevzme2pys62n3e0fbqa7peapykr8v": "Edge"}


# ---------------- pure helpers (tested) ----------------
def load_local(path: Path = LOCAL) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def merge(base: dict, updates: dict) -> dict:
    """Deep-merge updates into base; a None value removes the key."""
    out = dict(base)
    for k, v in updates.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def save_local(updates: dict, path: Path = LOCAL) -> dict:
    cfg = merge(load_local(path), updates)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def installed_browsers(env: dict | None = None, exists: Callable[[str], bool] = os.path.exists) -> list[dict]:
    env = os.environ if env is None else env
    out, names = [], set()
    for name, (base, rel), chromium in BROWSERS:
        root = env.get(base)
        if not root or name in names:
            continue
        p = os.path.join(root, rel)
        if exists(p):
            names.add(name)
            out.append({"name": name, "path": p, "chromium": chromium})
    return out


def default_browser_name() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                                                      r"\UrlAssociations\https\UserChoice") as k:
            prog = winreg.QueryValueEx(k, "ProgId")[0].lower()
    except OSError:
        return None
    for key, name in DEFAULT_PROGIDS.items():
        if key in prog:
            return name
    return prog.replace("url", "").replace("html", "").strip(".") or None


def obsidian_vaults(appdata: str | None = None) -> list[str]:
    """Vault folders Obsidian knows about (from %APPDATA%/obsidian/obsidian.json), existing ones only."""
    appdata = appdata or os.environ.get("APPDATA") or ""
    try:
        data = json.loads((Path(appdata) / "obsidian" / "obsidian.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    vaults = [v.get("path") for v in (data.get("vaults") or {}).values() if isinstance(v, dict)]
    return [p for p in vaults if p and Path(p).is_dir()]


def mask(key: str | None) -> str:
    return f"…{key[-4:]}" if key and len(key) > 8 else ("set" if key else "not set")


# ---------------- the interactive part ----------------
class Wizard:
    def __init__(self, ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
                 local: Path = LOCAL, check_key: Callable[[str], str | None] | None = None):
        self.ask, self.say, self.local = ask, say, local
        self.cfg = load_local(local)
        self.check_key = check_key or check_gemini_key

    def q(self, prompt: str, current: str | None = None) -> str:
        hint = f" [{current}]" if current else ""
        a = self.ask(f"{prompt}{hint}: ").strip()
        return a or (current or "")

    def yes(self, prompt: str, default: bool = True) -> bool:
        a = self.ask(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not a else a.startswith(("y", "s"))       # "sí" works too

    def choose(self, prompt: str, options: list[str], default: int = 1) -> int:
        for i, o in enumerate(options, 1):
            self.say(f"  {i}. {o}")
        while True:
            a = self.ask(f"{prompt} [{default}]: ").strip()
            if not a:
                return default
            if a.isdigit() and 1 <= int(a) <= len(options):
                return int(a)
            self.say(f"  Type a number from 1 to {len(options)}.")

    def save(self, updates: dict) -> None:
        self.cfg = save_local(updates, self.local)

    def header(self, n: int, title: str) -> None:
        self.say(f"\n── {n}. {title} " + "─" * max(4, 44 - len(title)))

    # 1
    def you(self) -> None:
        self.header(1, "You")
        u = self.cfg.get("user") or {}
        name = self.q("Your name (what Pakito calls you)", u.get("name"))
        about = self.q("One line about you, so answers fit (e.g. \"I'm a CS student at ASU\"; Enter to skip)",
                       u.get("about"))
        city = self.q("Your city, for weather and \"near me\" (e.g. Tempe, AZ)", (self.cfg.get("voice") or {}).get("location"))
        self.save({"user": {"name": name or None, "about": about}, "voice": {"location": city or None}})

    # 2
    def gemini(self) -> None:
        self.header(2, "Gemini key (free)")
        env = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        have = self.cfg.get("gemini_api_key") or env
        if have:
            self.say(f"  You have one ({mask(have)}{', from the GEMINI_API_KEY variable' if env else ''}).")
            if not self.yes("  Paste a different one?", default=False):
                return
        else:
            self.say("  Pakito's voice is Gemini Live. The key is free; you need a Google account.")
            self.say(f"  Opening {KEY_PAGE} … click \"Create API key\", then copy it.")
            try:
                webbrowser.open(KEY_PAGE)
            except Exception:
                pass
        while True:
            key = self.ask("  Paste the key here (Enter to skip for now): ").strip()
            if not key:
                self.say("  Skipped. Pakito can't talk without it: run `run-pakito.bat setup` again when you have it.")
                return
            self.say("  Checking it…")
            err = self.check_key(key)
            if not err:
                self.save({"gemini_api_key": key})
                self.say("  ✅ Works. Saved (only in config.local.json on this PC).")
                self.say("  Heads-up: on the free tier Google may use what you say to improve its models.")
                return
            self.say(f"  ❌ That didn't work: {err}")

    # 3
    def notes(self) -> None:
        self.header(3, "Where Pakito keeps your stuff")
        self.say("  Routines, tasks, reminders, the notes you dictate (\"toma nota…\") and its logs. All plain Markdown.")
        current = self.cfg.get("vault_path")
        default_dir = str(Path.home() / "Documents" / "Pakito")
        vaults = obsidian_vaults()
        opts = [f"A plain folder: {default_dir}"] + [f"My Obsidian vault: {v}" for v in vaults] + ["Somewhere else…"]
        paths = [default_dir] + vaults + [None]
        if current and current not in paths:
            opts.insert(0, f"Keep what I have: {current}")
            paths.insert(0, current)
        if vaults:
            self.say("  Found Obsidian. With a vault, Pakito writes into an Assistant/ folder and your inbox/,"
                     " and its notes link up with yours.")
        pick = self.choose("  Pick one", opts, 1 if not current else paths.index(current) + 1)
        path = paths[pick - 1] or self.q("  Folder path")
        if path:
            Path(path).expanduser().mkdir(parents=True, exist_ok=True)
            self.save({"vault_path": path})
            self.say(f"  ✅ {path}")

    # 4
    def browser(self) -> None:
        self.header(4, "Browser")
        found = installed_browsers()
        default = default_browser_name()
        opts = [f"Windows default{f' ({default})' if default else ''}"] + [b["name"] for b in found]
        values = ["default"] + [b["path"] for b in found]
        cur = self.cfg.get("browser")
        start = values.index(cur) + 1 if cur in values else 1
        pick = self.choose("  Which browser should links open in?", opts, start)
        self.save({"browser": values[pick - 1]})
        chosen = found[pick - 2] if pick > 1 else next((b for b in found if b["name"] == default), None)
        if chosen and not chosen["chromium"]:
            self.say("  Note: lock-in's site blocker extension needs a Chromium browser (Chrome, Edge, Opera, Brave)."
                     " In Firefox, Pakito still closes blocked tabs when they're in front.")
        else:
            self.say("  For lock-in site blocking, load the browser-extension folder later: your browser's "
                     "extensions page → Developer mode → Load unpacked.")

    # 5
    def mic(self) -> None:
        self.header(5, "Microphone")
        try:
            from .voice import list_mics, shared_pyaudio
            pa = shared_pyaudio()
            mics = list_mics(pa)
            default = pa.get_default_input_device_info()
        except Exception as e:
            self.say(f"  Couldn't list mics ({type(e).__name__}). Pakito will use the Windows default.")
            return
        if not mics:
            self.say("  No microphone found. Plug one in and run setup again.")
            return
        cur = (self.cfg.get("voice") or {}).get("mic")
        opts = [f"Windows default ({default.get('name')})"] + [m["name"] for m in mics]
        start = next((i + 2 for i, m in enumerate(mics) if cur and cur.lower() in m["name"].lower()), 1)
        pick = self.choose("  Which mic?", opts, start)
        dev = default if pick == 1 else mics[pick - 2]
        self.save({"voice": {"mic": None if pick == 1 else dev["name"]}})
        if self.yes("  Test it for 3 seconds? (talk normally)"):
            self.level_test(pa, dev)

    def level_test(self, pa, dev) -> None:
        import array
        import pyaudio
        try:
            s = pa.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1600,
                        input_device_index=dev["index"])
        except Exception as e:
            self.say(f"  ❌ Couldn't open it: {e}")
            return
        peak, end = 0, time.time() + 3
        while time.time() < end:
            a = array.array("h", s.read(1600, exception_on_overflow=False))
            lvl = max(abs(x) for x in a) / 32768
            peak = max(peak, lvl)
            print("\r  " + "█" * int(lvl * 40) + " " * 40, end="", flush=True)
        s.close()
        print()
        self.say("  ✅ Heard you." if peak > 0.05 else
                 "  ⚠ Very quiet. Check it's not muted (Windows Settings → Sound → Input), or pick another.")

    # 6
    def spotify(self) -> None:
        self.header(6, "Spotify")
        if (self.cfg.get("spotify") or {}).get("client_id"):
            self.say("  Already connected ✅")
            return
        a = self.ask("  Do you have Spotify Premium? [y/n, Enter to skip]: ").strip().lower()
        if a.startswith(("y", "s")):
            self.say("  Premium lets Pakito play, pause and skip. It needs a free Spotify developer app (~2 min).")
            if self.yes("  Set it up now?"):
                try:
                    from .spotify import setup
                    setup()
                    self.cfg = load_local(self.local)
                except Exception as e:
                    self.say(f"  Didn't finish ({type(e).__name__}). Later: run-pakito.bat spotify")
            else:
                self.say("  Later: run-pakito.bat spotify")
        elif a:
            self.say("  OK: without Premium Spotify doesn't let apps press play, so \"pon música\" opens the song or"
                     " playlist in Spotify and you hit play.")

    # 7
    def claude(self) -> None:
        self.header(7, "Claude Code (optional)")
        from .claude_bridge import find_claude
        exe = find_claude((self.cfg.get("claude") or {}).get("path"))
        if exe:
            self.say(f"  ✅ Found it ({exe}). \"Pregúntale a Claude\" and background builds will work.")
            self.say("  If it ever says the login expired: open a terminal, run `claude`, log in, /exit.")
        else:
            self.say("  Not installed. Optional: it lets Pakito hand hard questions and coding jobs to Claude")
            self.say("  (needs a Claude subscription). Install in PowerShell:  irm https://claude.ai/install.ps1 | iex")
            self.say("  then run `claude` once to log in. Everything else works without it.")

    def run(self) -> None:
        self.say("Pakito setup. Enter keeps the value in [brackets]; you can re-run this any time.")
        for step in (self.you, self.gemini, self.notes, self.browser, self.mic, self.spotify, self.claude):
            try:
                step()
            except (KeyboardInterrupt, EOFError):
                self.say("\nStopped. What you answered so far is saved.")
                return
        v = self.cfg.get("voice") or {}
        self.say("\n── Ready " + "─" * 40)
        self.say(f"  Talk: hold {v.get('hotkey', 'ctrl+alt')}, speak, let go. Tap it to hang up.")
        self.say(f"  Dictation anywhere: hold {v.get('dictation_hotkey', 'ctrl+windows')}, speak, let go.")
        self.say("  Try: \"pon música\", \"modo lock in\", \"abre YouTube\", \"toma nota: …\", \"recuérdame mañana…\"")
        self.say("  Extras, any time:  run-pakito.bat calendar (Google Calendar) · install-startup.bat (start with"
                 " Windows) · run-pakito.bat check (test the connection)")


def check_gemini_key(key: str) -> str | None:
    """None if the key works, else a short reason."""
    try:
        from google import genai
        client = genai.Client(api_key=key)
        next(iter(client.models.list(config={"page_size": 1})), None)
        return None
    except Exception as e:
        msg = str(e)
        if "API_KEY_INVALID" in msg or "API key not valid" in msg:
            return "Google says the key isn't valid. Copy it again (the whole thing)."
        return f"{type(e).__name__}: {msg[:160]}"


def main() -> None:
    Wizard().run()


if __name__ == "__main__":
    main()
