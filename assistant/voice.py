"""Voice layer: Gemini Live on a hotkey, with the hub's tools.

    run-pakito.bat setup                (once: Gemini key, mic, browser, Spotify...)
    run-pakito.bat                      (or: python -m assistant.voice)

Push-to-talk: HOLD Ctrl+Alt while you speak, release to send. The conversation stays open
for follow-ups (~60 s), so the next hold is instant. Hold again while it's talking to cut it
off. A quick tap hangs up; so does "that's all" / "gracias". While it runs you can still type
console commands (focus, d, stop, suggest, ...) in the same window.

Audio plumbing follows Google's official example
(github.com/google-gemini/gemini-live-api-examples, command-line/python/main.py).
"""
from __future__ import annotations

import array
import asyncio
import collections
import contextlib
import math
import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import gemini_key, load_config, user_line, user_name
from .hub import Hub

# Say any of these and the whole conversation is kept out of the logs (only a 'private' marker with its time is saved,
# so the daily dream also skips the actions from that window). Plain text match on what you said, no AI judging it.
OFF_RECORD = re.compile(r"off the record|off record|out of the record|no (lo |esto |eso )?(anotes|apuntes)|"
                        r"esto no (lo |se )?(anotes|apuntes|guardes)|fuera de(l)? registro", re.I)

SEND_RATE, RECEIVE_RATE, CHUNK = 16000, 24000, 1024

VOICE_DEFAULTS = {
    "model": "gemini-3.8-live",
    "mode": "ptt",               # "ptt" = hold the key to talk. "toggle" = press once, it listens hands-free
    "hotkey": "ctrl+alt",        # any key or combo: "ctrl+alt", "right ctrl", "f8", "ctrl+space"...
    "min_hold_sec": 0.3,         # shorter than this = a tap (hangs up) instead of talking
    "post_roll_sec": 0.3,        # keep listening this long after you let go, so the last word isn't cut
    "voice_name": None,          # e.g. "Puck", "Charon", "Kore". None = Gemini's default
    "idle_timeout_sec": 60,      # end the conversation after this much silence
    "barge_in": False,           # toggle mode only. True with headphones: interrupt it mid-sentence.
                                 # False: mic muted while it talks. (PTT never has echo: mic only sends while held)
    "location": None,            # e.g. "Tempe, AZ": for weather and "near me" (set by run-pakito.bat setup)
    "long_sessions": True,       # reconnect when Gemini drops a connection (~10 min); `check full` tests it
    "mic": None,                 # part of the mic's name (run-pakito.bat setup lists them). None = Windows default
    "google_search": True,       # False if your free tier blocks it -> uses the free `lookup` tool instead
    "pause_on_calls": True,      # another app using the mic (Zoom, Meet, WhatsApp...) -> Pakito hangs up and stays off it
    "call_ignore": [],           # apps that hold the mic but aren't calls, e.g. ["NVIDIA Broadcast.exe"] (run-pakito.bat mic)
    "dictation": True,           # hold the dictation hotkey anywhere: talk, let go, cleaned text gets typed
    "dictation_hotkey": "ctrl+windows",
    "dictation_model": None,     # None = first working Gemini flash model
    "vocabulary": [],            # extra words dictation should spell right, e.g. ["Botana", "RKKY"]
}

END_TOOL = {
    "name": "end_conversation",
    "description": "Hang up. ONLY when he clearly says goodbye or that he's done talking (bye, adiós, eso es todo, "
                   "that's all, ya no necesito nada). Never for short or unclear words like ¿qué?, ok, listo, or silence.",
    "parameters": {"type": "object", "properties": {}},
}


def log_error(hub: Hub, exc: BaseException) -> None:
    """Full traceback of a crashed conversation, so it can be fixed from the log instead of guessed."""
    import traceback
    try:
        p = hub.dir / "logs" / "errors.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


def system_prompt(hub: Hub, vcfg: dict) -> str:
    routines = hub.routines.all()
    rlines = "\n".join(
        f"- {r['name']}" + (f" (also: {', '.join(r['aliases'])})" if r["aliases"] else "") for r in routines
    ) or "- (none yet)"
    st = hub.focus.status()
    search_how = "use Google Search" if vcfg.get("google_search", True) else "call the lookup tool"
    hub.calendar.maybe_refresh()
    coming = hub.calendar.coming_up(int((hub.cfg.get("calendar") or {}).get("lookahead_days", 10)))
    coming_txt = ("\nComing up (his calendar):\n" + "\n".join(f"  {l}" for l in coming)) if coming else ""
    recent = hub.recent()
    recent_txt = "\n".join(f"  {l}" for l in recent) if recent else "  (nothing yet)"
    waiting = hub.claude.undelivered()
    claude_txt = ""
    if waiting:
        claude_txt = "\nClaude finished these after the last conversation ended; mention them first, briefly:\n" + \
            "\n".join(f"  - {'build' if j.get('kind') == 'build' else 'question'} \"{j['question'][:80]}\": "
                      f"{j['spoken'][:300]}" for j in waiting[-3:])
    building = [j for j in hub.claude.running() if j.get("kind") == "build"]
    to_review = [j for j in hub.claude.jobs.values() if j.get("review") == "waiting"]
    if building:
        claude_txt += "\nClaude is building right now: " + "; ".join(
            f"{j['question'][:60]} ({j.get('project')})" for j in building)
    if to_review:
        claude_txt += "\nBuilds waiting for his review: " + "; ".join(
            f"{j['question'][:60]} ({j.get('project')})" for j in to_review[-3:])
    due, ask = hub.reminders.pending()
    if due:
        claude_txt += ("\nREMINDERS that are due (tell him first, in one sentence each; then call reminder_done, or "
                       "snooze_reminder if he wants it later): " + "; ".join(f"[{r['id']}] {r['what']}" for r in due[:4]))
    if ask:
        claude_txt += ("\nThings he asked you to check on later. Ask about ONE per conversation, briefly, after "
                       "handling what he asked; save his answer with reminder_done(item, answer): " +
                       "; ".join(f"[{r['id']}] {r['what']}" for r in ask[:3]))
    from .debrief import dream_line, offer_dream_question
    dq = None if st["phase"] == "focus" and st.get("lockin") else offer_dream_question(hub)
    if dq:
        claude_txt += ("\nHis Second Brain (the nightly dream) has a question for him. After handling what he asked, "
                       "if he isn't in a hurry, ask it once, in Spanish, in your own short words; if he answers, call "
                       f"dream_answer(id, answer) with his words. If he says later / not now, drop it: [{dq['id']}] "
                       f"{dq['q'][:300]}")
    dream = dream_line(hub)
    if dream:
        claude_txt += ("\nFrom last night's dream (Claude processed his logs while he slept). After handling what he "
                       "asked, tell him this once, in one short sentence, in Spanish: " + dream[:300])
    where = f" They're in {vcfg['location']} unless they say otherwise." if vcfg.get("location") else ""
    return f"""You are Pakito, the voice assistant on the Windows PC of {user_line(hub.cfg)}.
Now: {datetime.now():%A %B %d %Y, %I:%M %p}.{where}

How to talk: short and natural, one or two sentences. This is speech, so no lists or markdown.
Answer in whatever language he speaks (English or Spanish, he switches).

How to act:
- To do something on the PC, call the tool right away, then confirm in a few words ("Listo, Canvas is open").
- Answer general knowledge yourself, right away (science, history, how things work, definitions). Searching is
  slow: only {search_how} for things that change or that you can't know (weather, news, prices, schedules, scores,
  anything about today). Only use web_search when he wants results opened on screen.
- His routines (call run_routine when he says one of these or something close to it):
{rlines}
- Focus trainer: level {hub.focus.level} min, currently {st['phase']}. "Start focus" with no minutes means use the level.
  If he says he got distracted, call log_distraction. If he asks how he's doing, call focus_stats.
- "Lock in" while a block is already running: call start_focus(lockin=true); it upgrades the running block
  (never stop and restart it). If the result says the site blocker isn't connected, tell him in one sentence.
- If he says "save that as a routine" or asks what he repeats, use routine_suggestions / save_routine.
- For hard questions (explain a concept properly, plan something, compare options, research), call
  ask_claude, say "let me check with Claude", and keep chatting; the answer will be handed to you when it's ready.
- For coding work in one of his projects ("haz que Claude agregue X en RabbitHole"), call delegate_build. It runs
  for minutes on its own branch. When it's done he reviews (review_build), then approve_build or discard_build.
  Never approve, discard or push unless he clearly says so; push_build always asks him to confirm first.
- If he asks about something on his screen ("mira esto", "¿qué es esto?", "explain this error"), call
  look_at_screen and answer from the image. "Mira esta parte" = area region. "Explícamelo bien / paso a paso" =
  claude=true too: give the quick version yourself, Claude's full one comes later.
- "Toma nota / anota esta idea" -> take_note, called AFTER he has said the note: Pakito saves his exact words from
  the recording, so pass just title + kind (+ turns=2 if he said "toma nota" in one turn and the note in the next).
  If he only announced a note ("vamos a hacer una nota"), say "dale, te escucho" and wait. Never retype a long
  note. "Recuérdame…" -> remember (with due if he gives a time; without it, you'll ask him about it later).
  "Agrega a mis tareas" -> add_task.
- His calendar: call whats_coming for "¿qué tengo hoy?" / "¿qué viene?". If an exam or project is today or
  tomorrow and he hasn't heard about it in this conversation, mention it once, in one short sentence. You're his
  organizer: dates and what to study, not homework help.
- "Escribe por mí…" / "contéstale que…": call type_text with the finished text; it's typed where his cursor is,
  he presses send himself. (He can also dictate on his own by holding Ctrl+Win; that doesn't involve you.)
- Don't redo things. If a tool says skipped/already opened, just say it's already open. Only pass force=true if he
  clearly asks to open it again.
- For school, you're an organizer, not a homework solver.
- "Terminé" / done for today: call end_day, read its summary, ask what he got done, what's still open, and how his
  projects went (what moved, what got stuck), call log_day, then offer the examen (save_examen has the questions). This is a conversation: one question at a time.
- When he's done talking to you, call end_conversation.

Done recently (oldest first):
{recent_txt}{claude_txt}{coming_txt}"""


LONG_SESSIONS = {"ok": True}      # turned off for this run if the model rejects compression/resumption


def live_config(hub: Hub, vcfg: dict) -> dict:
    cfg = {
        "response_modalities": ["AUDIO"],
        "system_instruction": system_prompt(hub, vcfg),
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        "tools": [{"function_declarations": [*hub.declarations(), END_TOOL]}],
    }
    if LONG_SESSIONS["ok"] and vcfg.get("long_sessions", True):
        # long talks (debrief + examen, long notes) would hit the context limit and drop: let Gemini forget the
        # oldest part instead. And a connection lasts ~10 min: a resumption handle lets Pakito reconnect to the
        # same conversation.
        cfg["context_window_compression"] = {"sliding_window": {}}
        cfg["session_resumption"] = {}
    if vcfg.get("google_search", True):
        cfg["tools"].insert(0, {"google_search": {}})
    if vcfg.get("mode", "ptt") == "ptt":
        cfg["realtime_input_config"] = {"automatic_activity_detection": {"disabled": True}}
    if vcfg.get("voice_name"):
        cfg["speech_config"] = {"voice_config": {"prebuilt_voice_config": {"voice_name": vcfg["voice_name"]}}}
    return cfg


def jsonable(obj) -> dict:
    out = json.loads(json.dumps(obj, default=str))
    return out if isinstance(out, dict) else {"result": out}


class Conversation:
    """One hotkey-to-hangup conversation. Audio I/O is injected so the logic is testable."""

    def __init__(self, hub: Hub, vcfg: dict, session, audio, clock=time.monotonic, log=print):
        self.hub, self.vcfg, self.session, self.audio = hub, vcfg, session, audio
        self.clock, self.log = clock, log
        self.out_q: asyncio.Queue[bytes] = asyncio.Queue()
        self.done = asyncio.Event()
        self.ending = False                  # end_conversation called: hang up once audio drains
        self.last_activity = clock()
        self.last_play = 0.0
        self.heard = ""                      # what he said this turn (for the learner)
        self.fresh_turn = True
        self.episode_for = None
        self.said: list[str] = []
        self.private = False                 # he said 'off the record' this conversation
        self.loop = None
        # push-to-talk state
        self.ptt = vcfg.get("mode", "ptt") == "ptt"
        self.ring = collections.deque(maxlen=7)   # ~0.45 s of audio from just before the press
        self.holding = False
        self.press_t = 0.0
        self.pre: list[bytes] = []               # audio captured this hold, not sent yet
        self.started = False                     # activity_start sent for this hold
        self.pending_turn: list[bytes] | None = None  # a whole turn spoken before the session connected
        self.mic_running = False
        self.sent_at: float | None = None        # when you let go: used to print response latency
        self.slow_warned = False
        self.turn_tools: list[str] = []
        self.release_t = 0.0
        self.closing_turn = False                # let go, still sending the post-roll tail
        self.peak = 0.0                          # loudest sample this hold (0..1), for the level readout
        # his own audio, per turn: take_note saves from this, and a long turn that never got an answer is
        # saved on its own when the conversation ends (voice_notes.py). Nothing is sent anywhere from here.
        self.turns: collections.deque = collections.deque(maxlen=8)
        self.cur_turn: list[bytes] | None = None
        self.failed: BaseException | None = None   # the connection died mid-conversation
        self.resume_handle: str | None = None      # from Gemini: lets a dropped connection pick up where it was
        self.going_away = False                    # Gemini warned the connection is about to close
        self.reconnect = None                      # async (handle) -> new session; set by converse()
        self.reconnects = 0

    # ---- push-to-talk ----
    def press(self) -> None:
        self.holding = True
        self.press_t = self.clock()
        self.peak = 0.0
        self.hub.pet.set("listen", level=0)
        if self.started and self.closing_turn:   # pressed again during the tail: same turn continues
            self.closing_turn = False
            self.last_activity = self.clock()
            return
        self._close_turn()
        self.pre = list(self.ring)
        self.cur_turn = list(self.ring)
        self.started = False
        self.last_activity = self.clock()
        while not self.out_q.empty():            # holding the key cuts it off mid-sentence
            self.out_q.get_nowait()
        self.last_play = 0.0

    def release(self) -> bool:
        """Returns True if this was a tap (too short to be speech)."""
        self.holding = False
        self.last_activity = self.clock()
        held = self.clock() - self.press_t
        tap = held < self.vcfg.get("min_hold_sec", 0.3)
        self.release_t = self.clock()
        if not tap:
            self.sent_at = self.clock()
            self.hub.pet.set("think", level=0)
            self.log(f"  → {held:.1f}s · mic {level_bar(self.peak)}")
            if 0 < self.peak < 0.03:
                self.log("  ⚠ mic very quiet: check Windows input volume, or that the right mic is the default")
        elif self.hub.pet.activity == "listen":
            self.hub.pet.set("idle", level=0)
        if self.started:
            self.closing_turn = True             # mic_loop sends the tail, then activity_end
        elif not tap and self.pre:
            self.pending_turn = self.pre         # spoke before the connection was ready
            self._close_turn()
        elif tap:
            self.cur_turn = None                 # a tap isn't a turn
        self.pre = []
        return tap

    def _close_turn(self) -> None:
        if self.cur_turn:
            self.turns.append({"pcm": b"".join(self.cur_turn), "t": self.clock(), "replied": False, "saved": False})
        self.cur_turn = None

    def _replied(self) -> None:
        if self.turns and not self.turns[-1]["replied"]:
            self.turns[-1]["replied"] = True

    async def _send(self, data: bytes) -> None:
        await self._rt(audio={"data": data, "mime_type": f"audio/pcm;rate={SEND_RATE}"})

    async def _rt(self, **kw) -> None:
        """send_realtime_input that survives a dead connection: his audio is kept locally (self.turns) anyway,
        and the receive side notices the drop and reconnects or ends the conversation."""
        try:
            await self.session.send_realtime_input(**kw)
        except Exception:
            pass

    # ---- server messages ----
    async def handle(self, msg) -> None:
        sru = getattr(msg, "session_resumption_update", None)
        if sru is not None and getattr(sru, "resumable", False) and getattr(sru, "new_handle", None):
            self.resume_handle = sru.new_handle
        if getattr(msg, "go_away", None) is not None:
            self.going_away = True                # the server closes soon: reconnect quietly when it does
        sc = getattr(msg, "server_content", None)
        if sc:
            if sc.input_transcription and sc.input_transcription.text:
                if self.fresh_turn:
                    self.heard, self.fresh_turn = "", False
                self.heard += sc.input_transcription.text
                self.hub.pet.say("you", sc.input_transcription.text)
                self.last_activity = self.clock()
            if sc.model_turn:
                for part in sc.model_turn.parts or []:
                    data = getattr(part.inline_data, "data", None) if part.inline_data else None
                    if isinstance(data, bytes):
                        self._replied()
                        if self.sent_at is not None:
                            dt = self.clock() - self.sent_at
                            self.log(f"  ⏱ {dt:.1f}s")
                            self.sent_at, self.slow_warned = None, False
                            try:
                                self.hub.latency.append({"time": datetime.now().isoformat(timespec="seconds"),
                                                         "model": self.vcfg.get("model"), "s": round(dt, 2),
                                                         "tools": self.turn_tools})
                            except Exception:
                                pass
                            self.turn_tools = []
                        self.out_q.put_nowait(data)
                        if self.hub.pet.activity != "talk":
                            self.hub.pet.set("talk", note="")
                        self.last_activity = self.clock()
            if sc.output_transcription and sc.output_transcription.text:
                self.log(sc.output_transcription.text, end="")
                self.hub.pet.say("pakito", sc.output_transcription.text)
            if sc.interrupted:
                while not self.out_q.empty():
                    self.out_q.get_nowait()
            if sc.turn_complete:
                if self.heard.strip() and (not self.said or self.said[-1] != self.heard.strip()):
                    self.log(f"\n  you: {self.heard.strip()}")
                    self.said.append(self.heard.strip())
                    if OFF_RECORD.search(self.heard):
                        self.private = True
                self.log("")
                self.fresh_turn = True
        tc = getattr(msg, "tool_call", None)
        if tc and tc.function_calls:
            self._replied()
            await self._run_tools(tc.function_calls)

    async def _run_tools(self, calls) -> None:
        from google.genai import types

        said = self.heard.strip()
        if said and said != self.episode_for:          # tie these actions to what he asked
            self.hub.learner.begin(said, source="voice")
            self.episode_for = said
        responses = []
        for fc in calls:
            args = dict(fc.args or {})
            if fc.name == "end_conversation":
                self.ending = True
                result = {"ok": True}
            else:
                self.hub.pet.set("think", note=f"⚙ {fc.name.replace('_', ' ')}")
                used = self._note_audio(args) if fc.name == "take_note" else []
                result = await asyncio.to_thread(self.hub.call, fc.name, args, "voice")
                if isinstance(result, dict) and result.get("saving"):
                    for t in used:
                        t["saved"] = True
                if fc.name == "look_at_screen" and self.hub.pending_image:
                    img, self.hub.pending_image = self.hub.pending_image, None
                    await self.session.send_realtime_input(video=types.Blob(data=img, mime_type="image/jpeg"))
                    await asyncio.sleep(0.3)            # let the frame land before the tool result
            self.turn_tools.append(fc.name)
            self.log(f"\n  ⚙ {fc.name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
            responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=jsonable(result)))
        self.last_activity = self.clock()
        await self.session.send_tool_response(function_responses=responses)

    def _note_audio(self, args: dict) -> list[dict]:
        """Hand his last `turns` turns (his own audio) to take_note, so Gemini never has to retype the note."""
        self.hub.pending_note_audio = None
        if not self.turns or self.hub.note_taker is None:
            return []
        try:
            n = max(1, min(4, int(float(args.get("turns") or 1))))
        except (TypeError, ValueError):
            n = 1
        picked = [t for t in list(self.turns)[-n:] if not t["saved"]]
        if picked:
            self.hub.pending_note_audio = b"".join(t["pcm"] for t in picked)
        return picked

    def save_unanswered(self) -> int:
        """End of conversation: long turns Gemini never answered get saved to the inbox from the audio."""
        self._close_turn()
        if self.private or self.hub.note_taker is None:
            return 0
        from .voice_notes import RECOVER_SPEECH_SEC, speech_seconds_pcm
        n = 0
        for t in self.turns:
            if not t["replied"] and not t["saved"] and speech_seconds_pcm(t["pcm"]) >= RECOVER_SPEECH_SEC:
                try:
                    self.hub.note_taker.submit(t["pcm"], None, "note", recovered=True)
                    t["saved"] = True
                    n += 1
                except Exception as e:
                    log_error(self.hub, e)
        if n:
            self.log(f"\n  📝 Pakito no alcanzó a responder {'un turno largo' if n == 1 else f'{n} turnos largos'}: "
                     "lo guardo en tu inbox desde el audio.")
        return n

    async def _guard(self, loop_fn) -> None:
        """A loop that dies (the Gemini connection dropped) ends the conversation out loud, not in silence."""
        try:
            await loop_fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self.done.is_set():
                self.failed = e
                if not (self.going_away or (self.resume_handle and self.reconnect)):
                    log_error(self.hub, e)
                self.done.set()

    # ---- loops ----
    async def mic_loop(self) -> None:
        self.mic_running = True
        while not self.done.is_set():
            data = await self.audio.read()
            if not self.ptt:
                talking = self.clock() - self.last_play < 0.35
                if talking and not self.vcfg["barge_in"]:
                    continue                     # half-duplex: don't let it hear itself
                await self._send(data)
                continue
            # push-to-talk
            if self.session is not None and self.pending_turn:
                turn, self.pending_turn = self.pending_turn, None
                await self._rt(activity_start={})
                for chunk in turn:
                    await self._send(chunk)
                await self._rt(activity_end={})
            if self.holding:
                lvl = peak_level(data)
                self.peak = max(self.peak, lvl)
                self.hub.pet.set(level=lvl)
                if self.cur_turn is not None:
                    self.cur_turn.append(data)
            if not self.holding:
                if self.started and self.closing_turn:
                    if self.cur_turn is not None:
                        self.cur_turn.append(data)
                    await self._send(data)                       # post-roll tail
                    if self.clock() - self.release_t >= self.vcfg.get("post_roll_sec", 0.3):
                        self.started = self.closing_turn = False
                        self._close_turn()
                        await self._rt(activity_end={})
                    continue
                self.ring.append(data)
                continue
            held = self.clock() - self.press_t
            if self.started:
                await self._send(data)
            else:
                self.pre.append(data)
                if self.session is not None and held >= self.vcfg.get("min_hold_sec", 0.3):
                    self.started = True
                    await self._rt(activity_start={})
                    chunks, self.pre = self.pre, []
                    for chunk in chunks:
                        await self._send(chunk)

    async def receive_loop(self) -> None:
        empty = 0
        while not self.done.is_set():
            got = False
            async for msg in self.session.receive():
                got = True
                await self.handle(msg)
                if self.done.is_set():
                    return
            empty = 0 if got else empty + 1
            if empty >= 20:                       # receive() keeps returning nothing: the socket closed quietly
                raise ConnectionError("the Gemini connection closed")
            if not got:
                await asyncio.sleep(0.05)

    async def play_loop(self) -> None:
        while not self.done.is_set():
            try:
                chunk = await asyncio.wait_for(self.out_q.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            self.hub.pet.set(level=peak_level(chunk))
            await self.audio.write(chunk)
            self.last_play = self.clock()

    async def watchdog(self) -> None:
        while not self.done.is_set():
            await asyncio.sleep(0.25)
            playing = not self.out_q.empty() or self.clock() - self.last_play < 0.5
            if not playing and self.hub.pet.activity == "talk":
                self.hub.pet.set("idle", level=0)
            waiting_on_claude = self.hub.claude.pending > 0 and not self.ending
            if self.sent_at is not None and not self.slow_warned and self.clock() - self.sent_at > 12:
                self.slow_warned = True
                self.log("  ⏳ Gemini is slow to answer… (tap Ctrl+Alt to hang up and ask again)")
            if self.ending and not playing:
                self.done.set()
            elif not playing and not waiting_on_claude and not self.holding and \
                    self.clock() - self.last_activity > self.vcfg["idle_timeout_sec"]:
                self.done.set()

    async def _try_reconnect(self) -> bool:
        """The connection dropped (Gemini closes each one after ~10 min): pick the same conversation back up."""
        if self.failed is None or self.ending:
            return False
        e, quiet = self.failed, self.going_away
        if self.reconnect and self.resume_handle and self.reconnects < 3:
            try:
                session = await self.reconnect(self.resume_handle)
            except Exception as err:
                log_error(self.hub, err)
                session = None
            if session is not None:
                self.session, self.failed, self.going_away = session, None, False
                self.reconnects += 1
                self.started = False                  # a turn in progress starts over on the new connection
                self.last_activity = self.clock()
                self.done.clear()
                if not quiet:
                    self.log("\n  ↻ (se cortó la conexión con Gemini; reconectado, seguimos)")
                return True
        log_error(self.hub, e)
        self.log(f"\n⚠ Se cayó la conexión con Gemini ({type(e).__name__}). Lo que dijiste no se pierde: si era "
                 "largo, va a tu inbox. Presiona otra vez para seguir.")
        return False

    # ---- Claude answers arriving mid-conversation ----
    def on_claude(self, job: dict) -> bool:
        """Called from the Claude thread. Returns True if this conversation will say it."""
        if self.done.is_set() or self.loop is None:
            return False
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._say_claude(job)))
        return True

    async def _say_claude(self, job: dict) -> None:
        self.last_activity = self.clock()
        self.log(f"\n  🟠 Claude ({job.get('seconds', '?')} s): {job['spoken'][:200]}")
        await self.session.send_realtime_input(
            text=(f'(Claude finished the build "{job["question"][:80]}". Tell him briefly, then ask if he wants '
                  f'to review it: {job["spoken"]})' if job.get("kind") == "build" else
                  f'(Claude\'s answer to "{job["question"]}" just arrived. Tell him in your own words, briefly: '
                  f'{job["spoken"]})'))

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.hub.claude.listeners.append(self.on_claude)
        started = time.time()
        mic_task = None if self.mic_running else asyncio.create_task(self._guard(self.mic_loop))
        try:
            while True:
                loops = [self.receive_loop, self.play_loop, self.watchdog]
                tasks = [asyncio.create_task(self._guard(c)) for c in loops]
                try:
                    await self.done.wait()
                finally:
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                if not await self._try_reconnect():
                    break
        finally:
            if mic_task:
                mic_task.cancel()
                await asyncio.gather(mic_task, return_exceptions=True)
            if self.on_claude in self.hub.claude.listeners:
                self.hub.claude.listeners.remove(self.on_claude)
            self.hub.end_request()
            try:
                self.save_unanswered()
            except Exception as e:
                log_error(self.hub, e)
            # The conversation can end (cut off, crash, hang-up) before Gemini sends turn_complete for his last
            # turn; without this, what he said last never reaches the log.
            pending = self.heard.strip()
            if not self.fresh_turn and pending and (not self.said or self.said[-1] != pending):
                self.said.append(pending)
                if OFF_RECORD.search(pending):
                    self.private = True
            if self.said:
                rec = {"start": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
                       "seconds": round(time.time() - started)}
                self.hub.conversations.append({**rec, "private": True, "said": []} if self.private
                                              else {**rec, "said": self.said})


def peak_level(data: bytes) -> float:
    """Loudest sample in a 16-bit PCM chunk, 0..1."""
    a = array.array("h")
    a.frombytes(data[: len(data) - len(data) % 2])
    return (max(abs(min(a, default=0)), abs(max(a, default=0))) / 32768.0) if a else 0.0


def level_bar(peak: float) -> str:
    if peak <= 0:
        return "(no audio)"
    db = 20 * math.log10(max(peak, 1e-6))                # 0 dB = max
    bars = "▁▂▃▄▅▆▇█"
    i = max(0, min(7, int((db + 48) / 6)))              # -48 dB .. 0 dB
    word = "too quiet" if db < -30 else "low" if db < -20 else "good" if db < -2 else "clipping"
    return f"{bars[:i + 1]} {db:.0f} dB ({word})"


_PA = None


def shared_pyaudio():
    """Initializing PortAudio takes a few hundred ms; do it once, not per conversation."""
    global _PA
    if _PA is None:
        import pyaudio
        _PA = pyaudio.PyAudio()
    return _PA


MIC = {"name": None}          # voice.mic from config: part of the mic's name; None = Windows default


def list_mics(pa) -> list[dict]:
    """Input devices, MME only (each mic shows up once per Windows audio API otherwise)."""
    out, seen = [], set()
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d.get("maxInputChannels", 0) < 1:
            continue
        try:
            api = pa.get_host_api_info_by_index(d["hostApi"]).get("name", "")
        except Exception:
            api = ""
        if api and "MME" not in api:
            continue
        if d["name"] in seen:
            continue
        seen.add(d["name"])
        out.append(d)
    return out


def pick_mic(pa, name: str | None = None) -> dict:
    """The configured mic (first input whose name contains `name`), else the Windows default."""
    name = (name if name is not None else MIC["name"]) or ""
    if name.strip():
        for d in list_mics(pa):
            if name.lower() in d["name"].lower():
                return d
    return pa.get_default_input_device_info()


class PyAudioIO:
    def __init__(self):
        import pyaudio

        self.pa = shared_pyaudio()
        mic = pick_mic(self.pa)
        self.mic_name = mic.get("name", "?")
        self.inp = self.pa.open(format=pyaudio.paInt16, channels=1, rate=SEND_RATE, input=True,
                                input_device_index=mic["index"], frames_per_buffer=CHUNK)
        self.out = self.pa.open(format=pyaudio.paInt16, channels=1, rate=RECEIVE_RATE, output=True)

    async def read(self) -> bytes:
        return await asyncio.to_thread(self.inp.read, CHUNK, exception_on_overflow=False)

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self.out.write, data)

    def close(self) -> None:
        for s in (self.inp, self.out):
            try:
                s.stop_stream()
                s.close()
            except Exception:
                pass


SOUNDS = {"on": [(1050, 45)], "off": [(760, 45)], "close": [(880, 70), (620, 90)],
          "busy": [(420, 50), (0, 40), (420, 50)]}       # pressed during a call: Pakito stays off the mic
_SOUND_FILES: dict[str, str] = {}


def _render_sounds() -> None:
    """Write tiny WAVs once; PlaySound(ASYNC) plays them instantly (winsound.Beep lags on many PCs)."""
    import tempfile
    import wave
    folder = Path(tempfile.gettempdir()) / "pakito-sounds"
    folder.mkdir(exist_ok=True)
    rate = 22050
    for kind, notes in SOUNDS.items():
        frames = array.array("h")
        for freq, ms in notes:
            n = int(rate * ms / 1000)
            for i in range(n):
                env = min(1.0, i / 80, (n - i) / 80)                      # tiny fade: no clicks
                frames.append(int(9000 * env * math.sin(2 * math.pi * freq * i / rate)))
        f = folder / f"{kind}.wav"
        with wave.open(str(f), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(frames.tobytes())
        _SOUND_FILES[kind] = str(f)


def beep(kind: str) -> None:
    """on = mic open (you pressed), off = sent (you let go), close = conversation over."""
    if sys.platform != "win32":
        return
    import winsound
    try:
        if not _SOUND_FILES:
            _render_sounds()
        winsound.PlaySound(_SOUND_FILES[kind], winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        threading.Thread(target=lambda: [winsound.Beep(f, d) for f, d in SOUNDS[kind]], daemon=True).start()


class KeyPoller:
    """Reads the physical keyboard ~100x/s on its own thread (Windows GetAsyncKeyState).

    Replaces the keyboard hook: hooks get delayed or silently dropped when the process is
    busy or throttled in the background; polling the real key state can't be.
    Emits 'press', 'release', 'cancel' (a third key joined -> it's a shortcut, not talking)."""

    # keys that turn Ctrl+Alt+<key> into a shortcut instead of talking
    OTHER_KEYS = ([0x08, 0x09, 0x0D, 0x1B, 0x20, 0x2E, 0x25, 0x26, 0x27, 0x28]
                  + list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) + list(range(0x70, 0x7C)))

    def __init__(self, hotkey: str, emit, pressed=None, interval: float = 0.01):
        self.parts = [HotkeyTracker.ALIASES.get(p.strip().lower(), p.strip().lower())
                      for p in hotkey.split("+") if p.strip()]
        self.part_vks = set()
        for p in self.parts:
            if p == "windows":
                self.part_vks |= {0x5B, 0x5C}
            elif p in VK:
                self.part_vks.add(VK[p])
            elif len(p) == 1 and p.isalnum():
                self.part_vks.add(ord(p.upper()))
        self.emit = emit
        self.pressed = pressed or physically_pressed
        self.interval = interval
        self.active = False
        self.blocked = False
        self._stable = 0
        self._last = False
        self._stop = threading.Event()

    def _combo_down(self) -> bool:
        return all(self.pressed(p) is not False for p in self.parts)

    def _other_down(self) -> bool:
        vk_down = getattr(self, "vk_down", None)
        if vk_down is None:
            return False
        return any(vk_down(vk) for vk in self.OTHER_KEYS if vk not in self.part_vks)

    def step(self) -> str | None:
        """One poll. Needs the same reading twice in a row (~10-20 ms) so a bounce can't fire."""
        now = self._combo_down()
        if now == self._last:
            self._stable += 1
        else:
            self._stable, self._last = 1, now
        if self._stable < 2:
            return None
        if self.blocked:
            if not now:
                self.blocked = False
            return None
        if self.active and self._other_down():
            self.active, self.blocked = False, True
            return "cancel"
        if now != self.active:
            if now and self._other_down():            # Ctrl+Alt pressed around another key: shortcut
                self.blocked = True
                return None
            self.active = now
            return "press" if now else "release"
        return None

    def run(self) -> None:
        if sys.platform == "win32":
            import ctypes
            gaks = ctypes.windll.user32.GetAsyncKeyState
            self.vk_down = lambda vk: bool(gaks(vk) & 0x8000)
            try:
                ctypes.windll.winmm.timeBeginPeriod(1)    # 1 ms sleep resolution instead of ~15 ms
            except Exception:
                pass
        while not self._stop.is_set():
            what = self.step()
            if what:
                self.emit(what)
            time.sleep(self.interval)

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True, name="pakito-keys").start()

    def stop(self) -> None:
        self._stop.set()


def windows_tweaks() -> list[str]:
    """Keep Pakito responsive: no console freeze on click, no background throttling."""
    done = []
    if sys.platform != "win32":
        return done
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    try:   # QuickEdit: clicking the console would pause the whole program until Esc
        h = k32.GetStdHandle(-10)
        mode = wintypes.DWORD()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)
            done.append("console-click-freeze off")
    except Exception:
        pass
    try:   # above-normal priority
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00008000)
        done.append("priority up")
    except Exception:
        pass
    try:   # opt out of Windows 11 efficiency-mode throttling
        class PPT(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]
        st = PPT(1, 0x1, 0x0)   # PROCESS_POWER_THROTTLING_EXECUTION_SPEED, state off
        if k32.SetProcessInformation(k32.GetCurrentProcess(), 4, ctypes.byref(st), ctypes.sizeof(st)):
            done.append("no throttling")
    except Exception:
        pass
    return done


class HotkeyTracker:
    """Turns raw key events into press/release of the hotkey (a key or combo like 'ctrl+alt').
    'ctrl'/'alt'/'shift' in a combo match either side; 'right ctrl' means only the right one.

    Windows sometimes reports a key-up under a different name than its key-down (Ctrl+Alt acts
    like AltGr on Spanish layouts), or drops it. So: releasing ANY variant of a modifier clears
    the whole family, and `sync()` checks the physical keyboard so a hold can never get stuck."""

    SIDES = ("ctrl", "alt", "shift", "windows")
    ALIASES = {"alt gr": "right alt", "altgr": "right alt", "control": "ctrl", "menu": "alt",
               "left ctrl": "ctrl", "left alt": "alt", "left shift": "shift", "left windows": "windows"}

    def __init__(self, hotkey: str):
        self.parts = [self.ALIASES.get(p.strip().lower(), p.strip().lower()) for p in hotkey.split("+") if p.strip()]
        self.down: set[str] = set()
        self.active = False
        self.blocked = False
        self._lock = threading.Lock()

    @staticmethod
    def _family(name: str) -> str | None:
        for side in HotkeyTracker.SIDES:
            if name in (side, f"left {side}", f"right {side}"):
                return side
        return None

    def _matches(self, part: str) -> bool:
        if part in self.SIDES:
            return any(self._family(k) == part for k in self.down)
        return part in self.down

    def _is_part(self, name: str) -> bool:
        return any(name == p or (p in self.SIDES and self._family(name) == p) for p in self.parts)

    def feed(self, name: str, is_down: bool) -> str | None:
        """Returns 'press', 'release', 'cancel' (another key joined: it's a shortcut like
        Ctrl+Alt+Del, not talking) or None."""
        name = (name or "").lower()
        name = self.ALIASES.get(name, name)
        with self._lock:
            if is_down:
                self.down.add(name)
                if self.active and not self._is_part(name):
                    self.active, self.blocked = False, True
                    return "cancel"
            else:
                fam = self._family(name)
                self.down = {k for k in self.down if k != name and (fam is None or self._family(k) != fam)}
            if self.blocked:
                if not any(self._matches(p) for p in self.parts):
                    self.blocked = False         # all let go: ready again
                return None
            now = all(self._matches(p) for p in self.parts)
            if now != self.active:
                self.active = now
                return "press" if now else "release"
            return None

    def sync(self, pressed) -> str | None:
        """pressed(part) -> is it physically down right now? Ends holds whose key-up got lost."""
        with self._lock:
            if not (self.active or self.blocked or self.down):
                return None
            physically = [pressed(p) for p in self.parts]
            if all(x is not False for x in physically):
                return None
            # at least one hotkey key is definitely up
            self.down = {k for k in self.down if not self._is_part(k)}
            if self.blocked:
                self.blocked = False
                return None
            if self.active:
                self.active = False
                return "release"
            return None


VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "left ctrl": 0xA2, "right ctrl": 0xA3, "left alt": 0xA4,
      "right alt": 0xA5, "left shift": 0xA0, "right shift": 0xA1, "space": 0x20, "tab": 0x09, "enter": 0x0D,
      "esc": 0x1B, "caps lock": 0x14, **{f"f{i}": 0x6F + i for i in range(1, 25)}}


def physically_pressed(part: str):
    """True/False from the real keyboard (Windows), or None if we can't tell."""
    if sys.platform != "win32":
        return None
    import ctypes
    gaks = ctypes.windll.user32.GetAsyncKeyState
    if part == "windows":
        return bool(gaks(0x5B) & 0x8000 or gaks(0x5C) & 0x8000)
    vk = VK.get(part) or (ord(part.upper()) if len(part) == 1 and part.isalnum() else None)
    return None if vk is None else bool(gaks(vk) & 0x8000)


async def converse(hub: Hub, vcfg: dict, client, conv_box: dict, first_press: bool = True) -> None:
    """One conversation: opens audio + the Live session, runs until it idles out or is hung up.
    conv_box['conv'] exposes the Conversation to the hotkey handler."""
    audio = await asyncio.to_thread(PyAudioIO)
    conv = Conversation(hub, vcfg, None, audio)
    conv_box["conv"] = conv
    mic = None
    connected = False
    try:
        if conv.ptt and first_press:
            if not conv_box.get("key_down"):
                return                           # just a tap: don't open a conversation
            conv.press()                         # start capturing while we connect
            conv.loop = asyncio.get_running_loop()
            mic = asyncio.create_task(conv.mic_loop())
            conv.mic_running = True
        waiting = hub.claude.undelivered()
        config = live_config(hub, vcfg)
        stack = contextlib.AsyncExitStack()

        async def reconnect(handle: str):
            cfg2 = {**config, "session_resumption": {"handle": handle}}   # same prompt; no second dream line
            return await stack.enter_async_context(client.aio.live.connect(model=vcfg["model"], config=cfg2))

        conv.reconnect = reconnect
        async with stack:
            try:
                session = await stack.enter_async_context(client.aio.live.connect(model=vcfg["model"],
                                                                                  config=config))
            except Exception as e:
                if "session_resumption" not in config:
                    raise
                LONG_SESSIONS["ok"] = False             # this model won't take them: connect without, from now on
                log_error(hub, e)
                print("  (this Gemini model doesn't take long-session support; continuing without it)")
                config = {k: v for k, v in config.items()
                          if k not in ("session_resumption", "context_window_compression")}
                session = await stack.enter_async_context(client.aio.live.connect(model=vcfg["model"],
                                                                                  config=config))
            hub.claude.mark_delivered(waiting)
            conv.session = session
            connected = True
            hub.pet.conversation(True)
            if conv.holding:
                hub.pet.set("listen")
            if first_press:
                print(f"  (mic: {getattr(audio, 'mic_name', '?')})")
            print("\n🎙  " + ("hold " + vcfg["hotkey"] + " and talk" if conv.ptt else "listening…"))
            if conv.done.is_set():               # tapped and let go before we even connected
                return
            await conv.run()
    finally:
        conv.done.set()
        if mic:
            mic.cancel()
            await asyncio.gather(mic, return_exceptions=True)
        conv_box["conv"] = None
        hub.pet.conversation(False)
        audio.close()
        if connected and not conv_box.get("on_call"):    # no beep in your ear mid-call
            beep("close")
            print("🔇 done\n› ", end="", flush=True)


def start_pet(cfg: dict) -> None:
    """Pakibot runs as its own process: if it crashes, voice keeps going; if Pakito quits, it closes itself."""
    if not (cfg.get("pet") or {}).get("enabled", True):
        return
    try:
        import subprocess
        kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        subprocess.Popen([sys.executable, "-m", "assistant.pet"], cwd=str(Path(__file__).resolve().parent.parent),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        print("  (Pakibot is up: right-click it for pop-in / stay / hide)")
    except Exception as e:
        print(f"  (pet didn't start: {e})")


def start_console(hub: Hub) -> None:
    from .__main__ import handle

    def loop():
        while True:
            try:
                line = input("› ")
            except EOFError:
                return
            if line.strip().lower() in ("quit", "exit", "q"):
                print("Close the window or Ctrl+C to quit voice mode.")
                continue
            handle(hub, line)

    threading.Thread(target=loop, daemon=True).start()


async def main_async() -> None:
    from google import genai

    cfg = load_config()
    if not gemini_key(cfg):
        print("No Gemini key yet. Run setup-pakito.bat (or run-pakito.bat setup): it walks you through getting a "
              "free one.")
        return
    vcfg = {**VOICE_DEFAULTS, **cfg.get("voice", {})}
    MIC["name"] = vcfg.get("mic")
    hub = Hub(cfg)
    from . import server
    if server.start(hub) is None:
        # port 8765 taken = another Pakito is already running; two of them means double voice + double pets
        print("Pakito ya está corriendo en otra ventana. Cierro esta para no duplicarlo.")
        return
    hub.blocker.start()
    client = genai.Client()  # reads GEMINI_API_KEY (gemini_key() exported it from config if needed)
    loop = asyncio.get_running_loop()
    events: asyncio.Queue[str] = asyncio.Queue()
    tweaks = windows_tweaks()
    if sys.platform == "win32":                  # poll the real keyboard: can't be delayed or dropped
        KeyPoller(vcfg["hotkey"], lambda what: loop.call_soon_threadsafe(events.put_nowait, what)).start()
        _render_sounds()
        await asyncio.to_thread(shared_pyaudio)  # warm up audio now, not on your first press
    else:
        import keyboard
        tracker = HotkeyTracker(vcfg["hotkey"])

        def on_key(e):
            what = tracker.feed(e.name, e.event_type == "down")
            if what:
                loop.call_soon_threadsafe(events.put_nowait, what)

        keyboard.hook(on_key)
    watcher = None
    if sys.platform == "win32" and vcfg.get("pause_on_calls", True):
        from .calls import CallWatcher
        watcher = CallWatcher(lambda on, apps: loop.call_soon_threadsafe(events.put_nowait, "call_on" if on else "call_off"),
                              ignore=vcfg.get("call_ignore", []))
        if watcher.reader():                     # registry readable: watch for calls
            watcher.start()
            tweaks.append("pauses during calls")
    if tweaks:
        print("  (" + ", ".join(tweaks) + ")")
    how = f"hold {vcfg['hotkey']} to talk, tap to hang up" if vcfg["mode"] == "ptt" else f"{vcfg['hotkey']} to start/stop"
    print(f"Pakito voice · {how} · model {vcfg['model']} · browser {hub.browser_name} · focus level {hub.focus.level} min")
    start_console(hub)
    start_pet(cfg)

    from .dictation import Transcriber
    from .voice_notes import NoteTaker
    transcriber = Transcriber(client, vcfg.get("dictation_model"), vcfg.get("vocabulary"))
    hub.note_taker = NoteTaker(hub, lambda wav, prompt: transcriber(wav, prompt=prompt), vocab=transcriber.vocab,
                               loop=loop, log=print)
    loop.create_task(hub.note_taker.retry_pending())      # notes whose audio didn't get transcribed last time
    dictation = None
    if sys.platform == "win32" and vcfg.get("dictation", True):
        from .dictation import Dictation
        dictation = Dictation(hub, transcriber,
                              beep=beep, log=lambda *a: print("\n" + " ".join(map(str, a)) + "\n› ", end="", flush=True))
        KeyPoller(vcfg["dictation_hotkey"],
                  lambda what: loop.call_soon_threadsafe(events.put_nowait, "dict_" + what)).start()
        print(f"  (dictation: hold {vcfg['dictation_hotkey']}, talk, let go → text appears where your cursor is)")

    box: dict = {"conv": None, "key_down": False, "on_call": bool(watcher and watcher.on_call)}
    task: asyncio.Task | None = None
    created_by_this_press = False
    while True:
        ev = await events.get()
        if ev.startswith("dict_"):
            if dictation is None:
                continue
            if ev == "dict_press":
                dictation.press()
                # beep only if it's still a hold a moment later (Ctrl+Win+arrow etc. is a shortcut, stays silent)
                loop.call_later(0.25, lambda: dictation.rec is not None and not dictation.cancelled and beep("on"))
            elif ev == "dict_cancel":
                dictation.cancel()
            else:
                asyncio.create_task(dictation.release())
            continue
        if ev in ("call_on", "call_off"):
            box["on_call"] = ev == "call_on"
            hub.pet.on_call = box["on_call"]
            if box["on_call"]:
                print(f"\n📞 {', '.join(watcher.apps)} is using the mic: Pakito let go of it until the call ends.")
                if box["conv"]:
                    box["conv"].done.set()
            else:
                print("\n📞 Call over, Pakito's back.\n› ", end="", flush=True)
            continue
        box["key_down"] = ev == "press"
        if ev == "press" and box["on_call"]:
            beep("busy")
            print(f"\n📞 On a call ({', '.join(watcher.apps)}): Pakito stays off the mic. "
                  "(\"pause_on_calls\": false to change that)")
            box["key_down"] = False
            continue
        if ev == "cancel" and vcfg["mode"] != "ptt":
            continue
        conv = box["conv"]
        if task and task.done():
            if task.exception():
                print(f"\n⚠ voice error: {task.exception()!r}  (details in logs/errors.log)")
                log_error(hub, task.exception())
            task, conv = None, None
        if vcfg["mode"] != "ptt":                # toggle: press starts, next press hangs up
            if ev != "press":
                continue
            if task is None:
                task = asyncio.create_task(converse(hub, vcfg, client, box, first_press=False))
            elif conv:
                conv.done.set()
            continue
        if ev == "press":
            beep("on")
            if task is None:
                created_by_this_press = True
                task = asyncio.create_task(converse(hub, vcfg, client, box))
            else:
                created_by_this_press = False
                if conv:
                    conv.press()
        elif ev == "cancel":                     # it was a shortcut (Ctrl+Alt+something), not talking
            if conv:
                conv.release()
                conv.pre = []
                if created_by_this_press:
                    conv.done.set()
        else:
            if conv is None:                     # released before audio even opened
                continue
            tap = conv.release()
            if not tap:
                beep("off")
            if tap:
                conv.ending = True               # tap = hang up (or never mind, if it just opened)
                if created_by_this_press or not conv.out_q.qsize():
                    conv.done.set()


def keytest() -> None:
    """run-pakito.bat keys: shows exactly when Pakito sees your hotkey go down/up."""
    cfg = load_config()
    hotkey = {**VOICE_DEFAULTS, **cfg.get("voice", {})}["hotkey"]
    print(windows_tweaks())
    _render_sounds()
    t0 = time.perf_counter()
    marks = {"press": "🟢 DOWN", "release": "⚪ UP  ", "cancel": "✖ shortcut (ignored)"}

    def show(what):
        if what == "press":
            beep("on")
        elif what == "release":
            beep("off")
        print(f"  {time.perf_counter() - t0:8.3f}s  {marks[what]}", flush=True)

    print(f"Hold {hotkey} a few times. Each line should appear the instant you press/let go. Ctrl+C to quit.")
    poller = KeyPoller(hotkey, show)
    poller.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        poller.stop()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "keytest":
        return keytest()
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
