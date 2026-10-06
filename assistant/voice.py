"""Voice layer: Gemini Live on a hotkey, with the hub's tools.

    setx GEMINI_API_KEY "your-key"      (once, then open a new terminal)
    pip install google-genai pyaudio keyboard winotify
    python -m assistant.voice

Push-to-talk: HOLD Ctrl+Alt while you speak, release to send. The conversation stays open
for follow-ups (~60 s), so the next hold is instant. Hold again while it's talking to cut it
off. A quick tap hangs up; so does "that's all" / "gracias". While it runs you can still type
console commands (focus, d, stop, suggest, ...) in the same window.

Audio plumbing follows Google's official example
(github.com/google-gemini/gemini-live-api-examples, command-line/python/main.py).
"""
from __future__ import annotations

import asyncio
import collections
import json
import sys
import threading
import time
from datetime import datetime

from .config import load_config
from .hub import Hub

SEND_RATE, RECEIVE_RATE, CHUNK = 16000, 24000, 1024

VOICE_DEFAULTS = {
    "model": "gemini-3.8-live",
    "mode": "ptt",               # "ptt" = hold the key to talk. "toggle" = press once, it listens hands-free
    "hotkey": "ctrl+alt",        # any key or combo: "ctrl+alt", "right ctrl", "f8", "ctrl+space"...
    "min_hold_sec": 0.3,         # shorter than this = a tap (hangs up) instead of talking
    "voice_name": None,          # e.g. "Puck", "Charon", "Kore". None = Gemini's default
    "idle_timeout_sec": 60,      # end the conversation after this much silence
    "barge_in": False,           # toggle mode only. True with headphones: interrupt it mid-sentence.
                                 # False: mic muted while it talks. (PTT never has echo: mic only sends while held)
    "location": "Tempe, AZ",
    "google_search": True,       # False if your free tier blocks it -> uses the free `lookup` tool instead
}

END_TOOL = {
    "name": "end_conversation",
    "description": "Call when the user is done (bye, thanks that's all, gracias, listo).",
    "parameters": {"type": "object", "properties": {}},
}


def system_prompt(hub: Hub, vcfg: dict) -> str:
    routines = hub.routines.all()
    rlines = "\n".join(
        f"- {r['name']}" + (f" (also: {', '.join(r['aliases'])})" if r["aliases"] else "") for r in routines
    ) or "- (none yet)"
    st = hub.focus.status()
    search_how = "use Google Search" if vcfg.get("google_search", True) else "call the lookup tool"
    recent = hub.recent()
    recent_txt = "\n".join(f"  {l}" for l in recent) if recent else "  (nothing yet)"
    waiting = hub.claude.undelivered()
    claude_txt = ""
    if waiting:
        claude_txt = "\nClaude answered these after the last conversation ended; mention them first, briefly:\n" + \
            "\n".join(f"  - {j['question']}: {j['spoken'][:300]}" for j in waiting[-3:])
    return f"""You are Pakito, Jose's voice assistant on his Windows PC. He's a CS + Physics student at ASU.
Now: {datetime.now():%A %B %d %Y, %I:%M %p}. He's in {vcfg['location']} unless he says otherwise.

How to talk: short and natural, one or two sentences. This is speech, so no lists or markdown.
Answer in whatever language he speaks (English or Spanish, he switches).

How to act:
- To do something on the PC, call the tool right away, then confirm in a few words ("Listo, Canvas is open").
- For facts about the world (weather, news, prices, hours, flights), {search_how} and answer out loud.
  Only use web_search when he wants results opened on screen.
- His routines (call run_routine when he says one of these or something close to it):
{rlines}
- Focus trainer: level {hub.focus.level} min, currently {st['phase']}. "Start focus" with no minutes means use the level.
  If he says he got distracted, call log_distraction. If he asks how he's doing, call focus_stats.
- If he says "save that as a routine" or asks what he repeats, use routine_suggestions / save_routine.
- For hard questions (explain a concept properly, plan something, compare options, research), call
  ask_claude, say "let me check with Claude", and keep chatting; the answer will be handed to you when it's ready.
- Don't redo things. If a tool says skipped/already opened, just say it's already open. Only pass force=true if he
  clearly asks to open it again.
- For school, you're an organizer, not a homework solver.
- When he's done, call end_conversation.

Done recently (oldest first):
{recent_txt}{claude_txt}"""


def live_config(hub: Hub, vcfg: dict) -> dict:
    cfg = {
        "response_modalities": ["AUDIO"],
        "system_instruction": system_prompt(hub, vcfg),
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        "tools": [{"function_declarations": [*hub.declarations(), END_TOOL]}],
    }
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

    # ---- push-to-talk ----
    def press(self) -> None:
        self.holding = True
        self.press_t = self.clock()
        self.pre = list(self.ring)
        self.started = False
        self.last_activity = self.clock()
        while not self.out_q.empty():            # holding the key cuts it off mid-sentence
            self.out_q.get_nowait()
        self.last_play = 0.0

    def release(self) -> bool:
        """Returns True if this was a tap (too short to be speech)."""
        self.holding = False
        self.last_activity = self.clock()
        tap = self.clock() - self.press_t < self.vcfg.get("min_hold_sec", 0.3)
        if self.started:
            self.started = False
            if self.session is not None:
                asyncio.ensure_future(self.session.send_realtime_input(activity_end={}))
        elif not tap and self.pre:
            self.pending_turn = self.pre         # spoke before the connection was ready
        self.pre = []
        return tap

    async def _send(self, data: bytes) -> None:
        await self.session.send_realtime_input(audio={"data": data, "mime_type": f"audio/pcm;rate={SEND_RATE}"})

    # ---- server messages ----
    async def handle(self, msg) -> None:
        sc = getattr(msg, "server_content", None)
        if sc:
            if sc.input_transcription and sc.input_transcription.text:
                if self.fresh_turn:
                    self.heard, self.fresh_turn = "", False
                self.heard += sc.input_transcription.text
                self.last_activity = self.clock()
            if sc.model_turn:
                for part in sc.model_turn.parts or []:
                    data = getattr(part.inline_data, "data", None) if part.inline_data else None
                    if isinstance(data, bytes):
                        self.out_q.put_nowait(data)
                        self.last_activity = self.clock()
            if sc.output_transcription and sc.output_transcription.text:
                self.log(sc.output_transcription.text, end="")
            if sc.interrupted:
                while not self.out_q.empty():
                    self.out_q.get_nowait()
            if sc.turn_complete:
                if self.heard.strip():
                    self.log(f"\n  you: {self.heard.strip()}")
                    if not self.said or self.said[-1] != self.heard.strip():
                        self.said.append(self.heard.strip())
                self.log("")
                self.fresh_turn = True
        tc = getattr(msg, "tool_call", None)
        if tc and tc.function_calls:
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
                result = await asyncio.to_thread(self.hub.call, fc.name, args, "voice")
            self.log(f"\n  ⚙ {fc.name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
            responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=jsonable(result)))
        self.last_activity = self.clock()
        await self.session.send_tool_response(function_responses=responses)

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
                await self.session.send_realtime_input(activity_start={})
                for chunk in turn:
                    await self._send(chunk)
                await self.session.send_realtime_input(activity_end={})
            if not self.holding:
                self.ring.append(data)
                continue
            held = self.clock() - self.press_t
            if self.started:
                await self._send(data)
            else:
                self.pre.append(data)
                if self.session is not None and held >= self.vcfg.get("min_hold_sec", 0.3):
                    self.started = True
                    await self.session.send_realtime_input(activity_start={})
                    chunks, self.pre = self.pre, []
                    for chunk in chunks:
                        await self._send(chunk)

    async def receive_loop(self) -> None:
        while not self.done.is_set():
            async for msg in self.session.receive():
                await self.handle(msg)
                if self.done.is_set():
                    return

    async def play_loop(self) -> None:
        while not self.done.is_set():
            try:
                chunk = await asyncio.wait_for(self.out_q.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            await self.audio.write(chunk)
            self.last_play = self.clock()

    async def watchdog(self) -> None:
        while not self.done.is_set():
            await asyncio.sleep(0.25)
            playing = not self.out_q.empty() or self.clock() - self.last_play < 0.5
            waiting_on_claude = self.hub.claude.pending > 0 and not self.ending
            if self.ending and not playing:
                self.done.set()
            elif not playing and not waiting_on_claude and not self.holding and \
                    self.clock() - self.last_activity > self.vcfg["idle_timeout_sec"]:
                self.done.set()

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
            text=f'(Claude\'s answer to "{job["question"]}" just arrived. Tell Jose in your own words, briefly: '
                 f'{job["spoken"]})')

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.hub.claude.listeners.append(self.on_claude)
        started = time.time()
        loops = [self.receive_loop, self.play_loop, self.watchdog] + ([] if self.mic_running else [self.mic_loop])
        tasks = [asyncio.create_task(c()) for c in loops]
        try:
            await self.done.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.on_claude in self.hub.claude.listeners:
                self.hub.claude.listeners.remove(self.on_claude)
            self.hub.end_request()
            if self.said:
                self.hub.conversations.append({"start": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
                                               "seconds": round(time.time() - started), "said": self.said})


class PyAudioIO:
    def __init__(self):
        import pyaudio

        self.pa = pyaudio.PyAudio()
        mic = self.pa.get_default_input_device_info()
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
        self.pa.terminate()


def beep(up: bool) -> None:
    if sys.platform == "win32":
        import winsound
        threading.Thread(target=lambda: (winsound.Beep(660 if up else 880, 90), winsound.Beep(880 if up else 660, 90)),
                         daemon=True).start()


class HotkeyTracker:
    """Turns raw key events into press/release of the hotkey (a key or combo like 'ctrl+space').
    'ctrl'/'alt'/'shift' in a combo match either side; 'right ctrl' means only the right one."""

    SIDES = ("ctrl", "alt", "shift", "windows")

    def __init__(self, hotkey: str):
        self.parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
        self.down: set[str] = set()
        self.active = False

    def _matches(self, part: str) -> bool:
        if part in self.SIDES:
            return any(k in (part, f"left {part}", f"right {part}") for k in self.down)
        return part in self.down

    def _is_part(self, name: str) -> bool:
        return any(name == p or (p in self.SIDES and name in (f"left {p}", f"right {p}")) for p in self.parts)

    def feed(self, name: str, is_down: bool) -> str | None:
        """Returns 'press', 'release', 'cancel' (another key joined: it's a shortcut like
        Ctrl+Alt+Del, not talking) or None."""
        name = (name or "").lower()
        if is_down:
            self.down.add(name)
            if self.active and not self._is_part(name):
                self.active = False
                self.blocked = True
                return "cancel"
        else:
            self.down.discard(name)
        if getattr(self, "blocked", False):
            if not any(self._matches(p) for p in self.parts):
                self.blocked = False         # all let go: ready again
            return None
        now = all(self._matches(p) for p in self.parts) and not any(not self._is_part(k) for k in self.down)
        if now != self.active:
            self.active = now
            return "press" if now else "release"
        return None


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
        async with client.aio.live.connect(model=vcfg["model"], config=config) as session:
            hub.claude.mark_delivered(waiting)
            conv.session = session
            connected = True
            beep(True)
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
        audio.close()
        if connected:
            beep(False)
            print("🔇 done\n› ", end="", flush=True)


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
    import keyboard  # global key hooks on Windows, no admin needed
    from google import genai

    cfg = load_config()
    vcfg = {**VOICE_DEFAULTS, **cfg.get("voice", {})}
    hub = Hub(cfg)
    hub.blocker.start()
    from . import server
    server.start(hub)
    client = genai.Client()  # reads GEMINI_API_KEY
    loop = asyncio.get_running_loop()
    events: asyncio.Queue[str] = asyncio.Queue()
    tracker = HotkeyTracker(vcfg["hotkey"])

    def on_key(e):
        what = tracker.feed(e.name, e.event_type == "down")
        if what:
            loop.call_soon_threadsafe(events.put_nowait, what)

    keyboard.hook(on_key)
    how = f"hold {vcfg['hotkey']} to talk, tap to hang up" if vcfg["mode"] == "ptt" else f"{vcfg['hotkey']} to start/stop"
    print(f"Pakito voice · {how} · model {vcfg['model']} · browser {hub.browser_name} · focus level {hub.focus.level} min")
    start_console(hub)

    box: dict = {"conv": None, "key_down": False}
    task: asyncio.Task | None = None
    created_by_this_press = False
    while True:
        ev = await events.get()
        box["key_down"] = ev == "press"
        if ev == "cancel" and vcfg["mode"] != "ptt":
            continue
        conv = box["conv"]
        if task and task.done():
            if task.exception():
                print(f"\n⚠ voice error: {task.exception()!r}")
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
            if tap:
                conv.ending = True               # tap = hang up (or never mind, if it just opened)
                if created_by_this_press or not conv.out_q.qsize():
                    conv.done.set()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
