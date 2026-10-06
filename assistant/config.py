"""Config: defaults, overridden by assistant/config.local.json (gitignored) and env vars."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS: dict = {
    # The LIVE vault. Override with env PAKITO_VAULT or config.local.json.
    "vault_path": r"C:\Users\jdsn2\OneDrive\Second Brain",
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
    "books": {
        "taylor": {
            "path": r"C:\Users\jdsn2\Downloads\pdfcoffee.com_john-taylor-classical-mechanics-pdf-pdf-free.pdf",
            "about": "Taylor, Classical Mechanics (PHY 310 textbook)",
        },
    },
    # "Pon un podcast": newest unplayed episodes from these (YouTube RSS: channel_id=... or playlist_id=...).
    "podcasts": [
        {"name": "Modern Wisdom (Chris Williamson)", "feed": "channel_id=UCIaH-gZIVC432YRjNVvnyCA",
         "spotify": "Modern Wisdom"},
        {"name": "ZZEN Talks", "feed": "playlist_id=PLuQ93B3Cj_8CdUQbt5l1QZE7BoV7c-lZg", "spotify": "ZZEN Talks"},
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
        "canvas": "https://canvas.asu.edu",
        "pwn college": "https://pwn.college",
        "perusall": "https://app.perusall.com",
        "edfinity": "https://edfinity.com",
        "matlab": "matlab",
        "obsidian": "obsidian://open",
        "gmail": "https://mail.google.com",
        "calendar": "https://calendar.google.com",
        "whatsapp": "whatsapp:",
        "claude": "https://claude.ai",
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
    return cfg


def data_dir(cfg: dict) -> Path:
    d = Path(cfg["vault_path"]) / cfg["assistant_folder"]
    (d / "logs").mkdir(parents=True, exist_ok=True)
    (d / "state").mkdir(parents=True, exist_ok=True)
    return d
