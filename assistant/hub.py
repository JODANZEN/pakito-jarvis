"""The hub: every action Pakito can take is a registered tool.

* Voice (Gemini Live) and the typed console both go through `Hub.call`, so every
  action is logged to <vault>/Assistant/logs/events.jsonl — that log is what the
  learner and the stats feed on.
* `Hub.declarations()` returns the tools in the function-declaration shape the
  Gemini Live API expects, so the voice layer doesn't need its own list.
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from .claude_bridge import ClaudeBridge
from .config import data_dir, load_config
from .focus import FocusTrainer
from .blocker import Blocker
from .learner import Learner
from .routines import Routines
from .util import JsonlLog, file_uri, launch_app_quietly, find_browser, make_launcher, make_notifier, now_iso

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
- open: https://canvas.asu.edu
- open: https://chatgpt.com
- music: deep focus
- lockin: on
- focus: study

## homework
aliases: homework mode, hw, tarea
- open: https://canvas.asu.edu
- open: https://chatgpt.com
- music: chill study

## phy 310
aliases: mechanics, classical mechanics, mecanica, taylor, phy 310 study
- book: taylor
- focus: PHY 310

## physics reading
aliases: phy 302, phy 302 reading, perusall, reading, lectura
- open: https://app.perusall.com
- open: https://chatgpt.com
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
DEDUPE = {"open_url": "url", "open_app": "name", "play_music": "query", "web_search": "query", "search_flights": "to",
          "open_book": "book"}
VERB = {"open_url": "opened", "open_app": "opened", "play_music": "music:", "web_search": "searched",
        "start_focus": "focus started", "stop_focus": "focus stopped", "run_routine": "routine",
        "ask_claude": "asked Claude:", "save_routine": "saved routine", "search_flights": "flights to", "open_book": "opened book", "play_podcast": "podcast:"}


def _target(name: str, args: dict) -> str:
    v = str(args.get(DEDUPE.get(name, ""), "")).strip().lower()
    if name == "open_url":
        v = v.split("://", 1)[-1].removeprefix("www.").rstrip("/")
    return v


class Hub:
    def __init__(self, cfg: dict | None = None, launcher: Callable[[str], None] | None = None,
                 notifier: Callable[[str, str], None] | None = None,
                 clock: Callable[[], float] = time.time, timer_factory=None):
        self.cfg = cfg or load_config()
        self.dir = data_dir(self.cfg)
        exe, self.browser_name = find_browser(self.cfg.get("browser", "auto"))
        self.launch = launcher or make_launcher(exe)
        self.notify = notifier or make_notifier()
        self.clock = clock
        self.events = JsonlLog(self.dir / "logs" / "events.jsonl")
        focus_kwargs = {"timer_factory": timer_factory} if timer_factory else {}
        self.focus = FocusTrainer(self.cfg["focus"], self.dir / "state" / "focus_state.json",
                                  JsonlLog(self.dir / "logs" / "focus.jsonl"), self.notify,
                                  clock=clock, **focus_kwargs)
        self.routines = Routines(self.dir / "Routines.md", seed=SEED_ROUTINES)
        self.learner = Learner(self.cfg["learner"], JsonlLog(self.dir / "logs" / "episodes.jsonl"),
                               JsonlLog(self.dir / "state" / "dismissed.jsonl"), clock=clock)
        self.mem = self.cfg["memory"]
        self.recent_opens: dict[tuple, float] = {}
        cutoff = clock() - self.mem["dedupe_min"] * 60
        for e in self.events.read()[-300:]:
            if e.get("tool") in DEDUPE and e.get("ok") and not (e.get("result") or {}).get("skipped") \
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
                               relaunch=(lambda: launch_app_quietly(relaunch_target)) if relaunch_target else None)
        from .podcasts import Podcasts
        heard = {(e.get("result") or {}).get("id") for e in self.events.read()
                 if e.get("tool") in ("play_podcast", "podcast_heard") and e.get("ok")}
        self.podcasts = Podcasts(self.cfg.get("podcasts", []), {h for h in heard if h}, clock=clock,
                                 api_key=self.cfg.get("youtube_api_key"),
                                 spotify=self.spotify.sp if self.spotify else None)
        self.claude = ClaudeBridge(self.cfg["claude"], self.dir, self.notify, clock=clock)
        self.conversations = JsonlLog(self.dir / "logs" / "conversations.jsonl")
        self.tools: dict[str, Tool] = {}
        self._register_builtins()

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
        self.events.append({
            "time": now_iso(t0), "ts": t0, "source": source, "tool": name, "args": args, "ok": ok,
            "episode": self.learner.current["id"] if self.learner.current else None,
            "result": result if len(str(result)) < 500 else str(result)[:500],
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
            if (e.get("result") or {}).get("skipped"):
                continue
            a = e.get("args", {})
            what = a.get("url") or a.get("name") or a.get("query") or a.get("label") or a.get("question") \
                or a.get("to") or a.get("book") or ""
            if e["tool"] == "play_podcast":
                what = (e.get("result") or {}).get("title", "")
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

        @self.tool("open_app", "Open an app or site by name (vscode, spotify, canvas, matlab, pwn college, perusall, gmail, whatsapp...).",
                   _obj({"name": S, "force": B}, ["name"]))
        def open_app(name: str):
            target = apps.get(name.lower().strip(), name)
            self.launch(target)
            return {"opened": name, "target": target}

        @self.tool("play_music", "Play music or a podcast on Spotify (pon música). Omit query for his default. "
                   "kind: playlist (default; his own playlists match first), track, artist, album, podcast.",
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

        @self.tool("podcast_options", "Pon un podcast: newest episodes he hasn't played from the creators he follows "
                   "(Chris Williamson / Modern Wisdom, ZZEN Talks). Read the 2-3 titles and let him pick.",
                   _obj({"creator": S}))
        def podcast_options(creator: str | None = None):
            return self.podcasts.options(creator)

        @self.tool("play_podcast", "Play the episode he picked from podcast_options: a number (1-3) or words from the title. "
                   "where = youtube or spotify (default from his settings).",
                   _obj({"choice": S, "where": S}, ["choice"]))
        def play_podcast(choice: str, where: str | None = None):
            ep = self.podcasts.pick(choice)
            if not ep:
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

        @self.tool("lookup", "Search the web and get result snippets back to answer from (weather, news, facts).",
                   _obj({"query": S}, ["query"]))
        def lookup(query: str):
            try:
                from ddgs import DDGS
            except ImportError:
                return {"error": "ddgs not installed (pip install ddgs)"}
            hits = DDGS().text(query, max_results=5)
            return {"results": [{"title": h.get("title"), "snippet": h.get("body"), "url": h.get("href")} for h in hits]}

        @self.tool("recent_actions", "What Pakito did recently (opened, played, started), to avoid repeating things.",
                   _obj({"hours": N}))
        def recent_actions(hours: float = 2):
            return {"recent": self.recent(hours=hours, limit=20)}

        @self.tool("ask_claude", "Hand a hard question to Claude: deep explanations, planning, comparisons, research. "
                   "Takes 10-60 s and the answer comes back on its own; tell Jose you're checking with Claude. "
                   "Not for quick facts.", _obj({"question": S, "context": S}, ["question"]))
        def ask_claude(question: str, context: str | None = None):
            return self.claude.ask(question, context)

        @self.tool("start_focus", "Start a focus block. Omit minutes to use the adaptive trainer's level. "
                   "lockin=true when he says lock in: distracting apps get closed until the break.",
                   _obj({"minutes": N, "label": S, "lockin": {"type": "boolean"}}))
        def start_focus(minutes: float | None = None, label: str | None = None, lockin: bool = False):
            return self.focus.start(minutes=minutes, label=label, lockin=lockin)

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
