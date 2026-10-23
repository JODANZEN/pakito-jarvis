"""The hub: every action Pakito can take is a registered tool.

* Voice (Gemini Live) and the typed console both go through `Hub.call`, so every
  action is logged to <vault>/Assistant/logs/events.jsonl — that log is what the
  learner and the stats feed on.
* `Hub.declarations()` returns the tools in the function-declaration shape the
  Gemini Live API expects, so the voice layer doesn't need its own list.
"""
from __future__ import annotations

import time
from datetime import datetime
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from .claude_bridge import ClaudeBridge
from .config import data_dir, device_path, device_name, load_config, user_name
from .focus import FocusTrainer
from .blocker import Blocker
from .learner import Learner
from .routines import Routines
from .util import (JsonlLog, StartApps, best_name_match, file_uri, launch_app_quietly, find_browser, make_launcher,
                   make_notifier, now_iso, open_in_editor, open_terminal)

SEED_ROUTINES = """# Routines

Pakito runs these when you say the name or an alias. Edit freely.
Actions: `open: <url>` · `app: <name>` · `book: <name> [page]` · `music: <search>` · `search: <query>` ·
`focus: [minutes] <label>` · `lockin: on`
(no minutes on `focus` = use the trainer's current level; `lockin: on` = blocked apps get closed until the break).
Accepted suggestions get added at the bottom.

## work mode
aliases: work, research, research mode, trabajo
- open: https://claude.ai
- music: lofi beats

## lock in
aliases: study, study mode, lock in mode, lockin, estudiar
- open: https://chatgpt.com
- music: deep focus
- lockin: on
- focus: study

## homework
aliases: homework mode, hw, tarea
- open: https://chatgpt.com
- music: chill study

## break
aliases: descanso, pausa
- music: chill
"""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable


def _obj(props: dict | None = None, required: list[str] | None = None) -> dict:
    schema: dict = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required
    return schema


S = {"type": "string"}
N = {"type": "number"}
B = {"type": "boolean", "description": "true only if he explicitly wants it opened again"}

# tools whose effect is "something opens" — skipped if the same thing opened recently
DEDUPE = {"open_url": "url", "open_app": "name", "web_search": "query", "search_flights": "to",
          "open_book": "book"}
VERB = {"open_url": "opened", "open_app": "opened", "play_music": "music:", "web_search": "searched",
        "start_focus": "focus started", "stop_focus": "focus stopped", "run_routine": "routine",
        "ask_claude": "asked Claude:", "save_routine": "saved routine", "search_flights": "flights to", "open_book": "opened book", "play_podcast": "podcast:",
        "add_task": "added task", "complete_task": "finished"}


REDACT = {"type_text": {"text"}}      # tool -> arg names whose content never goes in the logs


def _target(name: str, args: dict) -> str:
    v = str(args.get(DEDUPE.get(name, ""), "")).strip().lower()
    if name == "open_url":
        v = v.split("://", 1)[-1].removeprefix("www.").rstrip("/")
    return v


def _expand_home(p: str):
    import os
    from pathlib import Path
    if p.startswith("~"):
        p = (os.environ.get("USERPROFILE") or str(Path.home())) + p[1:]
    return Path(p)


def _res(e: dict) -> dict:
    """An event's result as a dict (long results are logged as text, which has no fields)."""
    r = e.get("result")
    return r if isinstance(r, dict) else {}


class Hub:
    def __init__(self, cfg: dict | None = None, launcher: Callable[[str], None] | None = None,
                 notifier: Callable[[str, str], None] | None = None,
                 clock: Callable[[], float] = time.time, timer_factory=None):
        self.cfg = cfg or load_config()
        self.dir = data_dir(self.cfg)
        self.device = device_name(self.cfg)
        self.local = lambda p: device_path(self.cfg, p)     # this computer's own copy of a file (see devices.py)

        def log(name: str, folder: str = "logs") -> JsonlLog:
            base = self.dir / folder / name
            return JsonlLog(self.local(base), family=base)
        from .devices import Presence
        self.presence = Presence(self.dir / "state" / "devices", self.device, clock)
        exe, self.browser_name = find_browser(self.cfg.get("browser", "auto"))
        self.launch = launcher or make_launcher(exe)
        self.notify = notifier or make_notifier()
        self.clock = clock
        self.events = log("events.jsonl")
        focus_kwargs = {"timer_factory": timer_factory} if timer_factory else {}
        self.focus = FocusTrainer(self.cfg["focus"], self.dir / "state" / "focus_state.json",
                                  log("focus.jsonl"), self.notify,
                                  clock=clock, **focus_kwargs)
        self.routines = Routines(self.dir / "Routines.md", seed=SEED_ROUTINES)
        self.learner = Learner(self.cfg["learner"], log("episodes.jsonl"), log("dismissed.jsonl", "state"),
                               clock=clock)
        self.mem = self.cfg["memory"]
        self.recent_opens: dict[tuple, float] = {}
        cutoff = clock() - self.mem["dedupe_min"] * 60
        for e in self.events.read()[-300:]:
            if e.get("tool") in DEDUPE and e.get("ok") and not _res(e).get("skipped") \
                    and e.get("ts", 0) >= cutoff:
                self.recent_opens[(e["tool"], _target(e["tool"], e.get("args", {})))] = e["ts"]
        self.spotify = None
        cid = (self.cfg.get("spotify") or {}).get("client_id")
        if cid:
            from . import spotify as _sp
            if _sp.CACHE.exists():                       # only after `run-pakito.bat spotify` logged in
                try:
                    self.spotify = _sp.SpotifyControl(_sp.make_client(cid, open_browser=False), self.launch)
                except Exception as e:
                    print(f"Spotify API off: {e}")
        self._music_paused_by_us = False
        self.focus.listeners.append(self._music_follows_focus)
        relaunch_target = self.cfg["focus"].get("mute_relaunch")
        self.blocker = Blocker(self.focus, self.cfg["focus"].get("block_apps", []), self.notify,
                               mute=self.cfg["focus"].get("mute_apps", []),
                               relaunch=(lambda: launch_app_quietly(relaunch_target)) if relaunch_target else None,
                               sites=self.cfg["focus"].get("block_sites", []), clock=clock)
        from .tasks import LocalTasks
        self.tasks = LocalTasks(self.dir / "Tasks.md")
        from .podcasts import Podcasts
        heard = {_res(e).get("id") for e in self.events.read()
                 if e.get("tool") in ("play_podcast", "podcast_heard") and e.get("ok")}
        self.podcasts = Podcasts(self.cfg.get("podcasts", []), {h for h in heard if h}, clock=clock,
                                 api_key=self.cfg.get("youtube_api_key"),
                                 spotify=self.spotify.sp if self.spotify else None)
        self.claude = ClaudeBridge(self.cfg["claude"], self.dir, self.notify, clock=clock, local=self.local,
                                   who=user_name(self.cfg), about=(self.cfg.get("user") or {}).get("about", ""))
        self.start_apps = StartApps()                 # installed apps, for "abre <anything>"
        self.open_terminal = open_terminal
        from .builds import Builds
        launched_files = lambda paths: [self.launch(p) for p in paths]   # tests: record instead of opening
        from .calendar_feed import Calendar
        ccfg = self.cfg.get("calendar") or {}
        self.calendar = Calendar(ccfg.get("feeds", []), self.local(self.dir / "state" / "calendar_cache.json"), clock=clock,
                                 refresh_min=ccfg.get("refresh_min", 30))
        if launcher is None:
            self.calendar.maybe_refresh()
        from .pet_state import PetState
        self.pet = PetState(self.local(self.dir / "state" / "pet.json"), clock, self.cfg.get("pet"))
        from pathlib import Path as _P
        from .notes import Inbox, Reminders
        self.inbox = Inbox(_P(self.cfg["vault_path"]), clock)
        self.reminders = Reminders(self.dir / "state" / "reminders.json", self.dir / "Reminders.md", clock=clock,
                                   notify=self.notify, on_fire=self._pet_reminder,
                                   in_charge=self.presence.in_charge)
        if launcher is None:
            self.presence.start()
            self.reminders.start()
        self._fuel = (0.0, 0.0)
        self.focus.listeners.append(lambda phase: phase == "break" and self.pet.flash("happy", 8))
        self.claude.listeners.append(self._pet_news)
        self.builds = Builds(self.claude, self.cfg["claude"], self.dir,
                             open_terminal=lambda cmd, cwd: self.open_terminal(cmd, cwd),
                             open_files=(lambda paths: launched_files(paths)) if launcher else open_in_editor)
        if launcher is None:                          # real run (not tests): load the app list in the background
            self.start_apps.preload()
        self.conversations = log("conversations.jsonl")
        self.latency = log("latency.jsonl")       # seconds from letting go to its voice
        self.note_taker = None                    # voice: saves notes from his own recording (voice_notes.py)
        self.pending_note_audio: bytes | None = None   # voice puts his recent turns here right before take_note
        self.tools: dict[str, Tool] = {}
        self._register_builtins()

    # ---------- the pet ----------
    def _pet_reminder(self, r: dict) -> None:
        self.pet.say("pakito", ("⏰ " if r["kind"] == "due" else "❓ ") + r["what"][:200])
        self.pet.flash("happy", 10)

    def _pet_news(self, job: dict) -> bool:
        """A Claude job finished: the pet pops in with it. Returns False: the voice side still decides 'heard'."""
        if job.get("status") == "done":
            self.pet.say("pakito", ("Build lista: " if job.get("kind") == "build" else "Claude: ") + job["spoken"][:200])
            self.pet.flash("happy", 8)
        return False

    def pet_snapshot(self) -> dict:
        at, minutes = self._fuel
        if self.clock() - at > 30:                     # focused minutes today, refreshed every 30 s
            day = time.localtime(self.clock())[:3]
            minutes = sum(b.get("actual_min", 0) for b in self.focus.log.read()
                          if time.localtime(b.get("start_ts", 0))[:3] == day)
            self._fuel = (self.clock(), minutes)
        return self.pet.snapshot(self.focus.status(), minutes, list(self.claude.jobs.values()))

    # ---------- registry ----------
    def tool(self, name: str, description: str, parameters: dict | None = None):
        def deco(fn):
            self.tools[name] = Tool(name, description, parameters or _obj(), fn)
            return fn
        return deco

    def declarations(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in self.tools.values()]

    def call(self, name: str, args: dict | None = None, source: str = "typed") -> dict:
        args = {k: v for k, v in (args or {}).items() if v is not None}
        if source in ("voice", "typed"):
            self.presence.beat(used=True)            # you're on this computer: it's in charge now
        t0 = self.clock()
        tool = self.tools.get(name)
        if tool is None:
            result, ok = {"error": f"unknown tool {name}"}, False
        else:
            force = bool(args.pop("force", False))
            key = (name, _target(name, args)) if name in DEDUPE else None
            last = self.recent_opens.get(key) if key else None
            if last is not None and not force and t0 - last < self.mem["dedupe_min"] * 60:
                result, ok = {"skipped": True, "reason": f"already opened at {now_iso(last)[11:16]}",
                              "hint": "it's probably still open; call again with force=true if he wants another"}, True
            else:
                try:
                    result = tool.fn(**args)
                    ok = not (isinstance(result, dict) and "error" in result)
                except Exception as e:  # a broken tool shouldn't kill the assistant
                    result, ok = {"error": f"{type(e).__name__}: {e}"}, False
                if ok and key:
                    self.recent_opens[key] = t0
        if name in REDACT:                               # never keep what he dictated/typed in the logs
            args = {k: (f"<{len(str(v))} chars>" if k in REDACT[name] else v) for k, v in args.items()}
            result = {k: v for k, v in (result or {}).items() if k not in REDACT[name]} if isinstance(result, dict) else result
        self.events.append({
            "time": now_iso(t0), "ts": t0, "source": source, "tool": name, "args": args, "ok": ok,
            "episode": self.learner.current["id"] if self.learner.current else None,
            "result": result if len(str(result)) < 500 else str(result)[:500],
            "ms": round((self.clock() - t0) * 1000),
        })
        if ok and source != "routine":
            self.learner.observe(name, args)
        return result

    # ---------- focus <-> music ----------
    def _music_follows_focus(self, phase: str) -> None:
        if not self.spotify or not self.cfg["focus"].get("pause_music_on_break", True):
            return
        try:
            if phase == "break":
                if (self.spotify.control("now_playing") or {}).get("is_playing"):
                    self.spotify.control("pause")
                    self._music_paused_by_us = True
            elif phase == "focus" and self._music_paused_by_us:
                self._music_paused_by_us = False
                self.spotify.control("resume")
        except Exception:
            pass                                    # music is a nicety; never break the timer over it

    # ---------- short-term memory ----------
    def recent(self, hours: float | None = None, limit: int | None = None) -> list[str]:
        """Compact lines like '1:05 PM opened canvas.asu.edu' for the last few hours."""
        hours = self.mem["recent_hours"] if hours is None else hours
        limit = self.mem["recent_limit"] if limit is None else limit
        cutoff = self.clock() - hours * 3600
        lines = []
        for e in self.events.read()[-400:]:
            if e.get("ts", 0) < cutoff or not e.get("ok") or e.get("tool") not in VERB:
                continue
            if _res(e).get("skipped"):
                continue
            a = e.get("args", {})
            what = a.get("url") or a.get("name") or a.get("query") or a.get("label") or a.get("question") \
                or a.get("to") or a.get("book") or a.get("task") or ""
            if e["tool"] == "play_podcast":
                what = _res(e).get("title", "")
            if e["tool"] == "open_book" and a.get("page"):
                what = f"{what} p.{a['page']:g}"
            if e["tool"] == "open_url":
                what = _target("open_url", a)
            if e["tool"] == "start_focus" and a.get("minutes"):
                what = f"{a['minutes']:g} min {what}".strip()
            t = time.strftime("%I:%M %p", time.localtime(e["ts"])).lstrip("0")
            lines.append(f"{t} {VERB[e['tool']]} {str(what)[:60]}".rstrip())
        return lines[-limit:]

    # ---------- requests (what you said/typed) ----------
    def request(self, utterance: str, source: str = "typed") -> dict:
        """Something you asked for. If it's a routine, run it; otherwise start an episode
        so the actions that follow get learned."""
        r = self.routines.match(utterance)
        if r:
            suggestion = self.learner.close()
            out = self.call("run_routine", {"name": r["name"]}, source=source)
            if suggestion:
                out["suggestion"] = suggestion
            return out
        suggestion = self.learner.close()
        ep = self.learner.begin(utterance, source)
        out = {"routine": None, "episode": ep}
        if suggestion:
            out["suggestion"] = suggestion
        return out

    def end_request(self) -> dict | None:
        """Close the open episode; returns a routine suggestion if one just became due."""
        s = self.learner.close()
        if s:
            self.notify("Save as routine?", f"You've done '{s['name']}' {s['count']}× the same way. Type 'save'.")
        return s

    # ---------- built-in tools ----------
    def _register_builtins(self) -> None:
        apps = self.cfg["apps"]

        @self.tool("open_url", "Open a URL in his browser.", _obj({"url": S, "force": B}, ["url"]))
        def open_url(url: str):
            if len(url) > 2 and url[1] == ":" and url[2] in "\\/":    # C:\... local path
                url = file_uri(url)
            elif "://" not in url and not url.startswith("file:"):
                url = "https://" + url
            self.launch(url)
            return {"opened": url}

        @self.tool("open_app", "Open an app or site by name: vscode, spotify, canvas, matlab, pwn college, perusall, gmail, "
                   "whatsapp, claude (the Claude desktop app)... or ANY installed app (discord, obsidian, blender...).",
                   _obj({"name": S, "force": B}, ["name"]))
        def open_app(name: str):
            key = name.lower().strip()
            if "".join(c for c in key if c.isalnum()) in ("claudecode", "claudeterminal", "claudeenlaterminal",
                                                           "claudeintheterminal", "claudeinterminal"):
                return self.tools["open_claude_code"].fn()
            target = apps.get(key)
            if target and target.startswith("app:"):           # "app:Claude|https://claude.ai"
                app, _, fallback = target[4:].partition("|")
                opened = self.start_apps.open(app)
                if opened:
                    return {"opened": opened, "app": True}
                if not fallback:
                    return {"error": f"{app} isn't installed."}
                target = fallback
            if not target:
                opened = self.start_apps.open(name)
                if opened:
                    return {"opened": opened, "app": True}
                target = name                                  # last try: run it as a command
            self.launch(target)
            return {"opened": name, "target": target}

        claude_cfg = self.cfg["claude"]

        def find_project(project: str | None):
            """-> (folder, name) or (None, error dict). Fuzzy match in his repos folder."""
            root = _expand_home(claude_cfg.get("projects_dir") or "~")
            if not project:
                return (root if root.is_dir() else _expand_home("~")), None
            subs = [d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")] \
                if root.is_dir() else []
            name = best_name_match(project, subs)
            if not name:
                return None, {"error": f"No project like '{project}' in {root}.", "projects": subs[:15]}
            return root / name, name

        @self.tool("open_claude_code", "Open a Claude Code session in a new terminal window (abre Claude Code / "
                   "Claude en la terminal). project = a folder in his repos (pakito, rabbithole, sputnik...); "
                   "omit for the repos folder. resume=true to continue the last session there (donde lo dejé).",
                   _obj({"project": S, "resume": B}))
        def open_claude_code(project: str | None = None, resume: bool = False):
            from .claude_bridge import find_claude
            exe = find_claude(claude_cfg.get("path"))
            if not exe:
                return {"error": "Claude Code isn't installed (PowerShell: irm https://claude.ai/install.ps1 | iex)."}
            folder, name = find_project(project)
            if folder is None:
                return name
            self.open_terminal([exe] + (["--continue"] if resume else []), str(folder))
            return {"opened": "Claude Code", "folder": str(folder), "project": name, "resumed": resume}

        @self.tool("play_music", "Play music or a podcast on Spotify (pon música). Omit query for his default. "
                   "kind: playlist (default; his own playlists match first), track, artist, album, podcast. For a song "
                   "by someone, query = 'song by artist'. If the result says unsure, tell him what you found and ask "
                   "if that's it. Call it once per request.",
                   _obj({"query": S, "kind": S, "force": B}))
        def play_music(query: str | None = None, kind: str = "playlist"):
            query = query or self.cfg.get("default_music", "lofi beats")
            if self.spotify:
                return self.spotify.play(query, kind)
            self.launch("spotify:search:" + urllib.parse.quote(query))
            return {"spotify_search": query}

        @self.tool("music_control", "Control Spotify: pause, resume, next, previous, volume_up, volume_down, "
                   "volume (with level 0-100), now_playing (what song is this).",
                   _obj({"action": S, "level": N}, ["action"]))
        def music_control(action: str, level: float | None = None):
            if not self.spotify:
                return {"error": "Spotify API isn't set up (run-pakito.bat spotify)."}
            return self.spotify.control(action, level)

        books = self.cfg.get("books", {})

        @self.tool("open_book", "Open one of his textbooks/PDFs, optionally at a PDF page. Books: " +
                   "; ".join(f"{k} = {v.get('about', '')}" for k, v in books.items()),
                   _obj({"book": S, "page": N}, ["book"]))
        def open_book(book: str, page: float | None = None):
            books = self.cfg.get("books") or {}
            key = book.lower().strip()
            match = books.get(key) or next((v for k, v in books.items()
                                            if k in key or key in k or key in v.get("about", "").lower()), None)
            if not match:
                return {"error": f"No book called '{book}'. Known: {', '.join(books)}"}
            self.launch(file_uri(match["path"], page))
            return {"opened": match.get("about", book), "page": page}

        @self.tool("day_summary", "How his day went so far (focus blocks, minutes, distractions, level). "
                   "Read the 'spoken' field out loud.")
        def day_summary():
            from .debrief import summary
            return summary(self)

        @self.tool("end_day", "He's done for the day (terminé, done for today, debrief, me voy a dormir): stops any "
                   "block, pauses music, saves the day's note. Read the 'spoken' field out loud.")
        def end_day():
            from .debrief import end_day as _end
            return _end(self)

        @self.tool("podcast_options", "Pon un podcast: newest episodes he hasn't played from the creators he follows ("
                   + ", ".join(sh["name"] for sh in self.cfg.get("podcasts", []))
                   + "). Pass creator if he names one. Read the 2-3 titles (and shows) and let him pick.",
                   _obj({"creator": S}))
        def podcast_options(creator: str | None = None):
            return self.podcasts.options(creator)

        @self.tool("play_podcast", "Play an episode: a number (1-3) from podcast_options, or words from the title/guest/show "
                   "(works without podcast_options too: 'el de Dana White en Diary of a CEO'). "
                   "where = youtube or spotify (default from his settings).",
                   _obj({"choice": S, "where": S}, ["choice"]))
        def play_podcast(choice: str, where: str | None = None):
            ep = self.podcasts.pick(choice)
            if not ep:
                if not str(choice).strip().isdigit() and len(str(choice).split()) >= 2:   # named one we don't have
                    self.launch("https://www.youtube.com/results?search_query=" + urllib.parse.quote(str(choice)))
                    return {"searching_youtube": choice, "note": "Not in the recent episodes of his shows; opened a "
                                                                 "YouTube search for it."}
                return {"error": "Couldn't tell which one; call podcast_options first or say the number."}
            where = (where or self.cfg.get("podcast_player", "youtube")).lower()
            self.podcasts.heard.add(ep["id"])
            if where == "spotify" and self.spotify:
                r = self.spotify.play(ep["title"], "episode")
                if "error" not in r:
                    return {"id": ep["id"], "title": ep["title"], "show": ep["show"], "on": "spotify", **r}
            self.launch(ep["url"])
            return {"id": ep["id"], "title": ep["title"], "show": ep["show"], "on": "youtube"}

        @self.tool("podcast_heard", "He already heard one of the offered episodes; don't offer it again.",
                   _obj({"choice": S}, ["choice"]))
        def podcast_heard(choice: str):
            ep = self.podcasts.pick(choice)
            if not ep:
                return {"error": "Which one?"}
            self.podcasts.heard.add(ep["id"])
            return {"id": ep["id"], "title": ep["title"], "ok": True}

        @self.tool("log_day", "Save what he finished today (done) and what's still open (still_open), in his words. "
                   "Updates his task list and today's note.",
                   _obj({"done": {"type": "array", "items": S}, "still_open": {"type": "array", "items": S}, "note": S}))
        def log_day(done: list | None = None, still_open: list | None = None, note: str | None = None):
            from .debrief import log_day as _log
            return _log(self, done, still_open, note)

        examen_qs = self.cfg.get("examen") or __import__("assistant.debrief", fromlist=["EXAMEN"]).EXAMEN

        @self.tool("save_examen", "The spoken Daily Examen. Before calling: ask these questions ONE at a time, in order, "
                   "and wait for each answer: " + " | ".join(f"{i + 1}. {q}" for i, q in enumerate(examen_qs)) +
                   " Be gentle and brief, don't analyze or give advice, just listen. Then call this with his answers "
                   "as close to his own words as possible, one per question.",
                   _obj({"answers": {"type": "array", "items": S}}, ["answers"]))
        def save_examen(answers: list):
            from .debrief import save_examen as _save
            return _save(self, answers)

        @self.tool("tasks", "His task list: what's open, and what was done today (¿qué tenemos para hoy?).")
        def tasks_tool():
            from .tasks import today_str
            return {"open": self.tasks.open(), "done_today": self.tasks.done_on(today_str(self.clock()))}

        @self.tool("add_task", "Add something to his task list (anota que tengo que...).", _obj({"task": S}, ["task"]))
        def add_task(task: str):
            return self.tasks.add(task)

        @self.tool("complete_task", "Mark a task done (ya terminé...).", _obj({"task": S}, ["task"]))
        def complete_task(task: str):
            from .tasks import today_str
            return self.tasks.complete(task, today_str(self.clock()))

        @self.tool("search_flights", "Open Google Flights with a search filled in (busca vuelos). "
                   "Pass what he said: destination, and origin/dates/return if he gave them.",
                   _obj({"to": S, "origin": S, "dates": {"type": "string", "description": "as he said it, e.g. 'Dec 15 to Jan 10'"}},
                        ["to"]))
        def search_flights(to: str, origin: str | None = None, dates: str | None = None):
            q = f"flights from {origin or self.cfg.get('home_airport', 'PHX')} to {to}" + (f" {dates}" if dates else "")
            url = "https://www.google.com/travel/flights?q=" + urllib.parse.quote_plus(q)
            self.launch(url)
            return {"opened": "Google Flights", "query": q}

        @self.tool("web_search", "Open a Google search in the browser (for when the user wants the results on screen).",
                   _obj({"query": S, "force": B}, ["query"]))
        def web_search(query: str):
            self.launch("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
            return {"searched": query}

        @self.tool("lookup", "Search the web for things that change (weather, news, prices, schedules). Slow: don't "
                   "use it for general knowledge you already have.",
                   _obj({"query": S}, ["query"]))
        def lookup(query: str):
            try:
                from ddgs import DDGS
            except ImportError:
                return {"error": "ddgs not installed (pip install ddgs)"}
            try:
                hits = DDGS(timeout=5).text(query, max_results=4)
            except Exception as e:
                return {"error": f"search failed ({type(e).__name__}); answer from what you know and say so"}
            return {"results": [{"title": h.get("title"), "snippet": h.get("body"), "url": h.get("href")} for h in hits]}

        @self.tool("current_time", "The exact time and date right now on this computer, in his time zone. Use it "
                   "when he asks the time and the conversation has gone on a while.")
        def current_time():
            t = datetime.fromtimestamp(self.clock()).astimezone()
            off = t.strftime("%z")
            return {"time": t.strftime("%I:%M %p").lstrip("0"), "date": t.strftime("%A %B %d %Y"),
                    "zone": f"{t.tzname()} (UTC{off[:3]}:{off[3:]})" if off else "local"}

        @self.tool("recent_actions", "What Pakito did recently (opened, played, started), to avoid repeating things.",
                   _obj({"hours": N}))
        def recent_actions(hours: float = 2):
            return {"recent": self.recent(hours=hours, limit=20)}

        from .screen import ScreenLook
        self.screen = ScreenLook(pet=self.pet)
        self.pending_image: bytes | None = None        # handed to Gemini by the voice side, then dropped

        @self.tool("look_at_screen", "Take ONE screenshot so you can see what he's looking at ('mira esto', "
                   "'¿qué es esto?', 'explícame esto', 'what's this error'). area: window (default, the window in "
                   "front), screen (whole monitor), region (he drags a box: 'mira esta parte'). The image arrives "
                   "right after this call; then answer his question about it. claude=true when he wants it "
                   "explained properly / step by step (Claude works it out in the background and saves it to his notes).",
                   _obj({"area": S, "question": S, "claude": {"type": "boolean"}}))
        def look_at_screen(area: str = "window", question: str | None = None, claude: bool = False):
            area = area if area in ("window", "screen", "region") else "window"
            shot = self.screen.capture(area)
            if "error" in shot:
                return shot
            self.pending_image = shot["jpeg"]
            out = {"looking_at": shot["title"] or area, "size": list(shot["size"]),
                   "note": "The screenshot is the image you just received. Answer about it; don't describe it "
                           "pixel by pixel. School: explain, don't do graded work for him."}
            if claude:
                self.claude.work.mkdir(parents=True, exist_ok=True)
                path = self.claude.work / f"screen-{int(self.clock())}.jpg"
                path.write_bytes(shot["jpeg"])
                job = self.claude.ask(question or "Explain what's on his screen, step by step.", image=path)
                out["claude"] = job.get("job") or job.get("error")
            return out

        @self.tool("whats_coming", "His calendar (classes, work, study blocks, exams, projects). '¿Qué tengo hoy?' "
                   "-> when=today. '¿Qué viene esta semana?' -> days=7. 'What exams are coming?' -> big=true. "
                   "Canvas assignments only when he asks about them (assignments=true).",
                   _obj({"when": S, "days": N, "big": {"type": "boolean"}, "assignments": {"type": "boolean"}}))
        def whats_coming(when: str | None = None, days: float = 7, big: bool = False, assignments: bool = False):
            from datetime import datetime as _dt, timedelta as _td
            from .calendar_feed import describe, is_big
            cal = self.calendar
            if not cal.feeds:
                return {"error": "His calendar isn't connected yet (run-pakito.bat calendar)."}
            cal.maybe_refresh(background=False)
            now = cal.now()
            start = _dt.combine(now.date(), _dt.min.time())
            if when in ("today", "hoy"):
                end = start + _td(days=1)
            elif when in ("tomorrow", "mañana", "manana"):
                start, end = start + _td(days=1), start + _td(days=2)
            else:
                end = now + _td(days=float(days))
            roles = None if assignments else ("main", "classes", "work")
            evs = cal.between(start, end, roles)
            if not assignments:                          # Canvas still counts for exams/projects
                evs += [e for e in cal.between(start, end, ("canvas",)) if is_big(e)]
                evs.sort(key=lambda e: e["start"])
            if big:
                evs = [e for e in evs if is_big(e)]
            out = {"events": [describe(e, now) for e in evs[:25]]}
            if cal.error:
                out["note"] = f"Some calendars didn't refresh ({cal.error}); this may be a bit old."
            return out

        from .dictation import paste
        self.typer = paste

        @self.tool("type_text", "Type text where his cursor is, in whatever app is in front ('escribe por mí que…', "
                   "'type this', 'contéstale que…'). Write exactly what he wants typed, in the language he wants, "
                   "ready to send (no quotes, no preamble). Don't press send: he reviews it.",
                   _obj({"text": S}, ["text"]))
        def type_text(text: str):
            ok = self.typer(text)
            return {"typed": bool(ok), "chars": len(text)} if ok else {"error": "Couldn't type it (clipboard busy)."}

        @self.tool("take_note", "Save a note to his second brain's inbox ('toma nota', 'anota esta idea', 'apunta "
                   "que…'): ideas, thoughts, things that come to mind. Call it AFTER he has said the note. In voice, "
                   "Pakito saves his own words from the recording, so don't retype long notes: give title (2-5 word "
                   "topic), kind (idea | note | thought) and turns = how many of his last turns hold the note (1 = "
                   "the turn he just said; 2 if he said 'toma nota' first and the content after). text = optional, "
                   "a one-line gist (or the note itself when typed). Not for to-dos (add_task) or things to bring "
                   "back later (remember).",
                   _obj({"text": S, "title": S, "kind": S, "turns": N}))
        def take_note(text: str | None = None, title: str | None = None, kind: str = "note", turns: float = 1):
            audio, self.pending_note_audio = self.pending_note_audio, None
            if audio is not None and self.note_taker is not None:
                from .voice_notes import MIN_NOTE_SPEECH_SEC, speech_seconds_pcm
                if speech_seconds_pcm(audio) >= MIN_NOTE_SPEECH_SEC:
                    r = self.note_taker.submit(audio, title, kind, fallback=text or "")
                    return {**r, "from": "his own recording (word for word, nothing summarized)",
                            "say": "Tell him it's being saved; it lands in his inbox in a few seconds."}
                if not (text or "").strip():
                    return {"error": "He hasn't said the note yet. Tell him you're listening, wait until he says "
                                     "it, then call take_note again."}
            if not (text or "").strip():
                return {"error": "He hasn't said the note yet. Tell him you're listening, then call take_note "
                                 "after he says it."}
            return self.inbox.add(text, title, kind)

        @self.tool("dream_questions", "Questions his Second Brain (the nightly dream) wants him to answer about himself "
                   "and his projects. Use when he asks '¿tienes preguntas para mí?' / 'what did the dream ask?'. Ask "
                   "one at a time, in Spanish, and save each answer with dream_answer.")
        def dream_questions():
            from .debrief import open_dream_questions
            qs = open_dream_questions(self)
            return {"questions": qs} if qs else {"questions": [], "note": "No open questions right now."}

        @self.tool("dream_answer", "Save his answer to one of the dream's questions, in his words (clean filler only, "
                   "never answer for him). id from dream_questions or the prompt.",
                   _obj({"id": S, "answer": S}, ["answer"]))
        def dream_answer(answer: str, id: str | None = None):
            from .debrief import answer_dream_question
            return answer_dream_question(self, id, answer)

        @self.tool("remember", "Remember something and bring it back later ('recuérdame…', 'remember that…'). "
                   "due = when to remind him, ISO local time like 2026-10-20T09:00 (work it out from 'el martes', "
                   "'en 3 días', 'mañana a las 8'; a day with no time = 9:00). No due = something to ask him about "
                   "later (follow up in ask_in_days, default 2): e.g. 'recuerda que le presté 20 a Mario'.",
                   _obj({"what": S, "due": S, "ask_in_days": N}, ["what"]))
        def remember(what: str, due: str | None = None, ask_in_days: float | None = None):
            return self.reminders.add(what, due, ask_in_days)

        @self.tool("reminders", "What he asked you to remember: upcoming reminders and follow-ups.")
        def reminders():
            return {"reminders": self.reminders.upcoming()}

        @self.tool("reminder_done", "Close a reminder once he's heard it, or record his answer to a follow-up "
                   "question (answer = what he said). item = id or a word from it; omit for the one just mentioned.",
                   _obj({"item": S, "answer": S}))
        def reminder_done(item: str | None = None, answer: str | None = None):
            return self.reminders.done(item, answer)

        @self.tool("snooze_reminder", "Push a reminder later ('recuérdamelo mañana', 'en una hora'). until = ISO "
                   "local time, or days.", _obj({"item": S, "until": S, "days": N}))
        def snooze_reminder(item: str | None = None, until: str | None = None, days: float | None = None):
            return self.reminders.snooze(item, until, days)

        @self.tool("pet_mode", "Pakibot, the on-screen pet: popin = appears only while you talk, stay = always "
                   "on screen ('quédate'), off = hide it ('desaparece'). 'Aparece' = popin.",
                   _obj({"mode": S}, ["mode"]))
        def pet_mode(mode: str):
            m = self.pet.set_mode(mode)
            return {"pet": m} if m else {"error": "mode is popin, stay or off"}

        @self.tool("ask_claude", "Hand a hard question to Claude: deep explanations, planning, comparisons, research. "
                   "Takes 10-60 s and the answer comes back on its own; say you're checking with Claude. "
                   "Not for quick facts.", _obj({"question": S, "context": S}, ["question"]))
        def ask_claude(question: str, context: str | None = None):
            return self.claude.ask(question, context)

        # ---- the slow brain: build jobs (see builds.py for the safety layers) ----
        def pick_build(job: str | None):
            j = self.claude.find(job, kind="build")
            return j or {"error": "No build job like that." if job else "No build jobs yet."}

        @self.tool("delegate_build", "Hand a coding job to Claude Code in one of his repos (haz que Claude "
                   "construya / arregle / agregue X en <proyecto>). It works in the background on its own branch "
                   "with a safety reviewer, takes minutes, and Pakito says when it's ready to review. For real code "
                   "changes, not questions. Put his exact request in task.",
                   _obj({"task": S, "project": S, "context": S}, ["task", "project"]))
        def delegate_build(task: str, project: str, context: str | None = None):
            folder, name = find_project(project)
            if folder is None:
                return name
            return self.builds.start(task, folder, name, context)

        @self.tool("claude_jobs", "Background Claude jobs (builds and questions): what's running, what's done, "
                   "what's waiting for his review. '¿Cómo va lo de X?' -> pass job='X'.", _obj({"job": S}))
        def claude_jobs(job: str | None = None):
            if job:
                j = self.claude.find(job)
                return self.claude.summary(j) if j else {"error": "No job like that."}
            jobs = sorted(self.claude.jobs.values(), key=lambda j: j.get("started", 0))[-6:]
            return {"jobs": [self.claude.summary(j) for j in jobs]}

        @self.tool("continue_job", "Send a follow-up to an earlier Claude job, same session and same branch "
                   "('dile a Claude que también…', 'que cambie…'). job = id or a word from its task; omit for the "
                   "latest.", _obj({"instruction": S, "job": S}, ["instruction"]))
        def continue_job(instruction: str, job: str | None = None):
            prev = self.claude.find(job, with_session=True)
            if not prev:
                return {"error": "No earlier Claude job to continue."}
            if prev["status"] == "running":
                return {"error": f"That one is still running ('{prev['question'][:60]}'). Wait until it's done."}
            if prev.get("kind") == "build":
                return self.builds.follow_up(prev, instruction)
            return self.claude.follow_up_ask(prev, instruction)

        @self.tool("review_build", "Open a finished build's review (what it ran, what it changed, the diff) in "
                   "VS Code ('revisa lo de X', 'muéstrame qué hizo').", _obj({"job": S}))
        def review_build(job: str | None = None):
            j = pick_build(job)
            return j if "error" in j else self.builds.review(j)

        @self.tool("approve_build", "Merge a reviewed build into his current branch, locally only "
                   "('apruébalo', 'mérgealo'). Only when he clearly approves.", _obj({"job": S}))
        def approve_build(job: str | None = None):
            j = pick_build(job)
            return j if "error" in j else self.builds.approve(j)

        @self.tool("discard_build", "Throw away a build's branch and folder ('descártalo', 'bótalo').",
                   _obj({"job": S}))
        def discard_build(job: str | None = None):
            j = pick_build(job)
            return j if "error" in j else self.builds.discard(j)

        @self.tool("push_build", "Push approved work to GitHub ('súbelo'). First call without confirm and ask him; "
                   "call again with confirm=true only after he says yes.",
                   _obj({"job": S, "confirm": {"type": "boolean"}}))
        def push_build(job: str | None = None, confirm: bool = False):
            j = self.claude.find(job, kind="build") if job else next(
                (x for x in sorted(self.claude.jobs.values(), key=lambda x: -x.get("started", 0))
                 if x.get("review") == "merged"), None)
            return {"error": "Nothing approved to push."} if not j else self.builds.push(j, confirm)

        @self.tool("free_mode", "Modo libre: open Claude Code with NO permission checks in a visible window, on its "
                   "own branch of a project. Only works after he unlocked it by typing his passphrase in the "
                   "console; if it's locked, tell him to type `libre` there.",
                   _obj({"task": S, "project": S}, ["task", "project"]))
        def free_mode(task: str, project: str):
            folder, name = find_project(project)
            if folder is None:
                return name
            return self.builds.free(task, folder, name, self.clock())

        @self.tool("start_focus", "Start a focus block. Omit minutes to use the adaptive trainer's level. "
                   "lockin=true when he says lock in: distracting apps get closed until the break.",
                   _obj({"minutes": N, "label": S, "lockin": {"type": "boolean"}}))
        def start_focus(minutes: float | None = None, label: str | None = None, lockin: bool = False):
            out = self.focus.start(minutes=minutes, label=label, lockin=lockin)
            if lockin and "error" not in out:
                out["site_blocker"] = ("extension connected" if self.blocker.extension_connected else
                                       "extension NOT connected: Pakito will still close blocked tabs in front, but tell "
                                       "him to load browser-extension in opera://extensions (Developer mode → Load "
                                       "unpacked) for full blocking")
            return out

        @self.tool("stop_focus", "Stop the current focus block early, or skip the current break.")
        def stop_focus():
            return self.focus.stop()

        @self.tool("log_distraction", "Log that the user got distracted during the focus block.", _obj({"note": S}))
        def log_distraction(note: str | None = None):
            return self.focus.distracted(note)

        @self.tool("focus_status", "Current focus/break state and time remaining.")
        def focus_status():
            return self.focus.status()

        @self.tool("focus_stats", "Focus stats over the last N days (default 7).", _obj({"days": N}))
        def focus_stats(days: float = 7):
            return self.focus.stats(days=int(days))

        @self.tool("run_routine", "Run a saved routine by name.", _obj({"name": S}, ["name"]))
        def run_routine(name: str):
            r = next((x for x in self.routines.all() if x["name"].lower() == name.lower()), None) \
                or self.routines.match(name)
            if not r:
                return {"error": f"No routine called '{name}'."}
            done = []
            lockin = any(a["type"] == "lockin" and a["value"].lower() in ("on", "yes", "true", "1", "")
                         for a in r["actions"])
            for a in r["actions"]:
                typ, val = a["type"], a["value"]
                if typ == "open":
                    res = self.call("open_url", {"url": val}, source="routine")
                elif typ == "app":
                    res = self.call("open_app", {"name": val}, source="routine")
                elif typ == "music":
                    res = self.call("play_music", {"query": val}, source="routine")
                elif typ == "search":
                    res = self.call("web_search", {"query": val}, source="routine")
                elif typ == "book":
                    parts = val.rsplit(maxsplit=1)
                    page = float(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None
                    res = self.call("open_book", {"book": parts[0] if page else val, "page": page}, source="routine")
                elif typ == "focus":
                    parts = val.split(maxsplit=1)
                    try:
                        mins, label = float(parts[0]), (parts[1] if len(parts) > 1 else None)
                    except (ValueError, IndexError):
                        mins, label = None, val or None
                    res = self.call("start_focus", {"minutes": mins, "label": label, "lockin": lockin or None},
                                    source="routine")
                else:
                    continue
                done.append({"action": a, "result": res})
            return {"routine": r["name"], "ran": done}

        @self.tool("list_routines", "List saved routines.")
        def list_routines():
            return {"routines": [{"name": r["name"], "aliases": r["aliases"], "actions": r["actions"]}
                                 for r in self.routines.all()]}

        @self.tool("routine_suggestions", "Patterns the user repeats that could be saved as routines.")
        def routine_suggestions():
            sug = self.learner.suggestions(self.routines.all())
            return {"suggestions": [{k: v for k, v in s.items() if k != "signature"} for s in sug]}

        @self.tool("save_routine", "Save suggestion number `index` (1-based, from routine_suggestions) as a routine, optionally renamed.",
                   _obj({"index": N, "name": S}, ["index"]))
        def save_routine(index: float, name: str | None = None):
            sug = self.learner.suggestions(self.routines.all())
            i = int(index) - 1
            if not 0 <= i < len(sug):
                return {"error": "No suggestion with that number."}
            s = sug[i]
            final = name or s["name"]
            aliases = [a for a in ([s["name"]] if name else []) + s["aliases"] if a != final]
            r = self.routines.add(final, s["actions"], aliases)
            return {"saved": r}

        @self.tool("dismiss_suggestion", "Never suggest suggestion number `index` again.", _obj({"index": N}, ["index"]))
        def dismiss_suggestion(index: float):
            sug = self.learner.suggestions(self.routines.all())
            i = int(index) - 1
            if not 0 <= i < len(sug):
                return {"error": "No suggestion with that number."}
            self.learner.dismiss(sug[i]["signature"])
            return {"dismissed": sug[i]["name"]}
