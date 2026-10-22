"""Config: defaults, overridden by assistant/config.local.json (gitignored) and env vars."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS: dict = {
    # Where Pakito keeps your notes, tasks, routines and logs (plain Markdown). Point it at an Obsidian vault if you
    # have one (it then uses an "Assistant" folder and the vault's inbox/). Override with env PAKITO_VAULT,
    # config.local.json, or `run-pakito.bat setup`.
    "vault_path": "~/Documents/Pakito",
    # Who Pakito is talking to. Set by `run-pakito.bat setup`.
    "user": {
        "name": None,                 # e.g. "Jose". None = "the user"
        "about": "",                  # one line Pakito and Claude get, e.g. "He's a CS + Physics student at ASU."
    },
    # More than one computer sharing the same folder (e.g. PC + Mac over OneDrive). The primary one keeps the
    # plain file names; every other one writes its own logs (events.<name>.jsonl...) and reads everyone's.
    # Whichever you used last owns reminders. Set by `setup` when it finds a folder another Pakito already uses.
    "device": {
        "name": None,                 # None = this computer's name
        "primary": True,
    },
    # Gemini key (free: https://aistudio.google.com/apikey). The GEMINI_API_KEY env var wins if both are set.
    "gemini_api_key": None,
    # Folder inside the vault where routines + logs live.
    "assistant_folder": "Assistant",
    # Browser for web links: "auto" = Opera GX if installed, else the Windows default.
    # Or "default", or a full path to any browser .exe.
    "browser": "auto",
    # Spoken/typed name -> what to launch. URLs and URIs open with the default handler,
    # anything else is run as a shell command (e.g. "code" for VS Code).
    "home_airport": "PHX",            # default origin for flight searches
    "default_music": "lofi beats",    # what "pon música" plays when you don't say what
    # PDFs Pakito can open by name, optionally at a page ("open Taylor page 160").
    # e.g. {"taylor": {"path": "C:/.../taylor.pdf", "about": "Taylor, Classical Mechanics"}}
    "books": {},
    # "Pon un podcast": newest unplayed episodes from these (YouTube RSS: channel_id=... or playlist_id=...).
    "podcasts": [
        {"name": "Modern Wisdom (Chris Williamson)", "feed": "channel_id=UCIaH-gZIVC432YRjNVvnyCA",
         "spotify": "Modern Wisdom"},
        {"name": "ZZEN Talks", "feed": "playlist_id=PLuQ93B3Cj_8CdUQbt5l1QZE7BoV7c-lZg", "spotify": "ZZEN Talks"},
        {"name": "Dr K (HealthyGamerGG)", "feed": "channel_id=UClHVl2N3jPEbkNJVx-ItQIQ", "spotify": "HealthyGamerGG"},
        {"name": "Joe Rogan Experience", "feed": "channel_id=UCzQUP1qoWDoEbmsQxvdjxgQ",
         "spotify": "The Joe Rogan Experience"},
        {"name": "Diary of a CEO", "feed": "channel_id=UCGq-a57w-aPwyi3pW7XLiHw", "spotify": "The Diary Of A CEO"},
        {"name": "Jordan B Peterson", "feed": "channel_id=UCL_f53ZEJxp8TtlOkHwMV9Q",
         "spotify": "The Jordan B. Peterson Podcast"},
        {"name": "Johnathan Bi", "feed": "channel_id=UCCrl9a26fDCZvofnCnA5A8g", "spotify": "Johnathan Bi"},
    ],
    "youtube_api_key": None,          # optional; `run-pakito.bat youtube` — used while YouTube RSS is down
    "podcast_player": "youtube",      # or "spotify" (plays the same episode there if Spotify has it)
    # Spotify Web API. Set by `run-pakito.bat spotify`. Without it, music opens Spotify's search page.
    "spotify": {"client_id": None},
    "apps": {
        "vscode": "code",
        "vs code": "code",
        "code": "code",
        "spotify": "spotify:",
        "obsidian": "obsidian://open",
        "gmail": "https://mail.google.com",
        "calendar": "https://calendar.google.com",
        "whatsapp": "whatsapp:",
        "claude": "app:Claude|https://claude.ai",   # desktop app if installed, else the website
        "chatgpt": "https://chatgpt.com",
        "chat gpt": "https://chatgpt.com",
        "chat": "https://chatgpt.com",
        "youtube": "https://www.youtube.com",
        "correo": "https://mail.google.com",
        "email": "https://mail.google.com",
        "calendario": "https://calendar.google.com",
        "google flights": "https://www.google.com/travel/flights",
        "vuelos": "https://www.google.com/travel/flights",
        "maps": "https://www.google.com/maps",
        "drive": "https://drive.google.com",
    },
    "focus": {
        "start_level_min": 20,        # first block length until it adapts
        "min_level_min": 10,
        "max_level_min": 90,
        "step_up_min": 2,             # completed block -> next one is this much longer
        "step_down_min": 2,           # bailed before early_stop_ratio of the block -> shorter
        "early_stop_ratio": 0.5,
        "max_distractions_for_levelup": 2,
        "break_ratio": 0.2,           # break = 1/5 of the block
        "min_break_min": 5,
        "long_break_every": 4,        # every 4th completed block gets a long break
        "long_break_min": 15,
        # Lock-in blocks close these if you open them (until the break). Process names from Task Manager > Details.
        "block_apps": ["Solitaire.exe"],
        # Lock-in blocks also block these sites in the browser (needs the Pakito extension, see README).
        "block_sites": ["instagram.com", "tiktok.com", "x.com", "twitter.com", "reddit.com",
                        "netflix.com", "twitch.tv", "facebook.com"],
        # Closed quietly during lock-in (not counted as distractions) and reopened on the break.
        # Phone Link = your phone's notifications on the PC.
        "mute_apps": ["PhoneExperienceHost.exe", "YourPhone.exe"],
        "mute_relaunch": r"shell:AppsFolder\Microsoft.YourPhone_8wekyb3d8bbwe!App",
        "pause_music_on_break": True,  # Spotify pauses when a break starts, resumes with the next block
    },
    "memory": {
        "dedupe_min": 30,             # don't reopen the same tab/app within this many minutes (say "again" to force)
        "recent_hours": 2,            # how far back the "recently done" list in Gemini's prompt goes
        "recent_limit": 8,
    },
    "claude": {
        "path": None,                 # auto-detects claude.exe; set if it lives somewhere odd
        "model": None,                # e.g. "sonnet" for faster answers; None = Claude Code's default
        "timeout_sec": 180,
        "allowed_tools": "WebSearch,WebFetch",
        "projects_dir": "~/source/repos",   # "abre Claude Code en <proyecto>" looks for <proyecto> here
        "build_timeout_min": 20,      # a background build gets this long, then stops (its commits stay on the branch)
        "build_max_turns": 80,
    },
    "calendar": {
        "feeds": [],                  # set by `run-pakito.bat calendar`: [{"label", "role", "url"}] (secret links!)
        "refresh_min": 30,
        "lookahead_days": 10,         # how far ahead "coming up" looks for exams/projects
    },
    "pet": {
        "enabled": True,              # Pakibot, the on-screen pet (assistant/pet.py)
        "mode": "popin",              # popin = only while talking, stay = always on screen, off = hidden
        "linger_sec": 10,             # pop-in: how long it stays after the conversation goes quiet
        "daily_goal_min": 120,        # focused minutes that fill its chest bar
        "scale": 3,                   # 3 = 144 px tall robot
    },
    "learner": {
        "repeat_threshold": 3,        # same request -> same actions this many times -> suggest a routine
        "episode_window_sec": 180,    # actions within this long after a request count toward it
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    local = Path(path) if path else Path(__file__).with_name("config.local.json")
    if local.exists():
        cfg = _merge(cfg, json.loads(local.read_text(encoding="utf-8")))
    if os.environ.get("PAKITO_VAULT"):
        cfg["vault_path"] = os.environ["PAKITO_VAULT"]
    cfg["vault_path"] = os.path.expandvars(os.path.expanduser(str(cfg["vault_path"])))
    return cfg


def device_name(cfg: dict) -> str:
    import re
    import socket
    raw = ((cfg.get("device") or {}).get("name") or socket.gethostname() or "device").split(".")[0]
    return re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-") or "device"


def is_primary(cfg: dict) -> bool:
    return bool((cfg.get("device") or {}).get("primary", True))


def device_path(cfg: dict, p: Path) -> Path:
    """This computer's copy of a file that must not be shared: the primary keeps the plain name (nothing changes
    for a one-computer setup); others get a suffix, e.g. events.joses-macbook.jsonl."""
    p = Path(p)
    if is_primary(cfg):
        return p
    return p.with_name(f"{p.stem}.{device_name(cfg)}{p.suffix}")


def user_name(cfg: dict) -> str:
    return ((cfg.get("user") or {}).get("name") or "").strip() or "the user"


def user_line(cfg: dict) -> str:
    """'Ana (I study biology at UNAL)': who Pakito is helping, with their own line about themselves."""
    about = ((cfg.get("user") or {}).get("about") or "").strip()
    return user_name(cfg) + (f" ({about.rstrip('.')})" if about else "")


def gemini_key(cfg: dict) -> str | None:
    """The key, from GEMINI_API_KEY or config.local.json; also exports it so genai.Client() finds it."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or cfg.get("gemini_api_key")
    if key and not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GEMINI_API_KEY"] = key
    return key


def data_dir(cfg: dict) -> Path:
    d = Path(cfg["vault_path"]) / cfg["assistant_folder"]
    (d / "logs").mkdir(parents=True, exist_ok=True)
    (d / "state").mkdir(parents=True, exist_ok=True)
    return d
