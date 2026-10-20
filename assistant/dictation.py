"""Dictation, Wispr Flow-style: hold Ctrl+Win, talk, let go: clean text appears where your cursor is.

Works in any app (WhatsApp, Canvas, VS Code, email), without opening a Pakito conversation.
Gemini (the same free key) transcribes AND lightly cleans it: punctuation, filler words out, "no, mejor…"
self-corrections applied, "nueva línea" honored. Your Spanish/English mix stays exactly as you said it.
The window you're typing into tells it the tone (chat = casual, code editor = keep identifiers).

Privacy: the audio goes to Gemini's free tier and is dropped; Pakito logs only that you dictated and how
long, never the text. Off during calls, like the rest of Pakito's mic.
Settings ("voice" in config.local.json): "dictation_hotkey" (default "ctrl+windows"), "dictation": false to
turn it off, "dictation_model", "vocabulary": ["Taylor", "Lagrangian", ...] for words it should spell right.
"""
from __future__ import annotations

import array
import io
import sys
import threading
import time
import wave
from typing import Callable

RATE, CHUNK = 16000, 1024
MIN_SEC = 0.5                      # shorter than this = an accidental tap, ignored
SPEECH_PEAK = 0.04                 # a chunk this loud (≈ -28 dB) counts as voice
MIN_SPEECH_SEC = 0.3               # need this much voice, or nothing is sent at all
NONE = "<<none>>"
VOCAB = ["Pakito", "Pakibot", "Canvas", "Perusall", "Taylor", "Lagrangian", "Hamiltonian", "PHY 310", "PHY 302",
         "CSE 355", "CSE 365", "MAT 343", "NextLab", "Nexo", "RabbitHole", "altermagnetism", "Claude", "Gemini"]
CANDIDATES = ["gemini-3.1-flash", "gemini-3-flash", "gemini-2.5-flash", "gemini-flash-latest"]

PROMPT = """Transcribe this voice dictation and return ONLY the final text to type. No quotes, no notes.
He's typing into: {app}.
Rules:
- Keep the language exactly as spoken (Spanish, English, or mixed mid-sentence). Never translate.
- Light cleanup like a good dictation app: punctuation, capitalization, accents; drop filler (eh, em, um,
  "o sea"/"like" when they're filler); apply spoken self-corrections ("a las 5, no, mejor a las 6" -> "a las 6").
- Spoken commands: "nueva línea"/"new line" -> line break; "punto y aparte"/"new paragraph" -> blank line;
  "coma"/"punto"/"signo de pregunta" only when clearly said as punctuation commands.
- Don't add, answer, summarize or rephrase content. It's his words, cleaned.
- Tone: in chat apps (WhatsApp, Discord, Messenger, Slack) keep it casual and skip the final period on short
  messages; in code editors/terminals keep identifiers and symbols literal.
- Spell these as written when they come up: {vocab}.
- If there's no clear human speech (silence, background noise, music, a tone, a cough), output exactly <<none>>
  and nothing else. Never invent words that weren't spoken."""


def speech_seconds(wav: bytes) -> float:
    """How long the recording is actually loud enough to be someone talking (cheap, local, no API call)."""
    with wave.open(io.BytesIO(wav)) as w:
        rate, pcm = w.getframerate(), w.readframes(w.getnframes())
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    voiced = 0
    for i in range(0, len(samples), CHUNK):
        chunk = samples[i:i + CHUNK]
        if chunk and max(abs(min(chunk)), abs(max(chunk))) / 32768 >= SPEECH_PEAK:
            voiced += len(chunk)
    return voiced / rate


def to_wav(frames: list[bytes], rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(frames))
    return buf.getvalue()


class Recorder:
    """Records the mic (voice.mic in config, else the default) on its own thread between start() and stop()."""

    def __init__(self):
        self.frames: list[bytes] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.started_at = 0.0

    def start(self) -> None:
        from .voice import pick_mic, shared_pyaudio
        import pyaudio
        pa = shared_pyaudio()
        self.frames, self.started_at = [], time.time()
        self._stop.clear()
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True, frames_per_buffer=CHUNK,
                         input_device_index=pick_mic(pa)["index"])

        def run():
            try:
                while not self._stop.is_set():
                    self.frames.append(stream.read(CHUNK, exception_on_overflow=False))
            finally:
                stream.stop_stream()
                stream.close()
        self._t = threading.Thread(target=run, daemon=True)
        self._t.start()

    def stop(self) -> tuple[bytes, float]:
        """-> (wav bytes, seconds)"""
        self._stop.set()
        if self._t:
            self._t.join(timeout=1)
        secs = len(self.frames) * CHUNK / RATE
        return to_wav(self.frames), secs


# ---------- typing into the focused app ----------
def _clip_get() -> str | None:
    import ctypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalLock.restype = ctypes.c_void_p
    u.GetClipboardData.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u.OpenClipboard(None):
        return None
    try:
        h = u.GetClipboardData(13)                         # CF_UNICODETEXT
        if not h:
            return None
        p = k.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else None
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def _clip_set(text: str) -> bool:
    import ctypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _ in range(10):                                     # another app may be holding the clipboard
        if u.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return False
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(0x0002, len(data))               # GMEM_MOVEABLE
        p = k.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        return bool(u.SetClipboardData(13, h))
    finally:
        u.CloseClipboard()


def _modifiers_down() -> bool:
    import ctypes
    g = ctypes.windll.user32.GetAsyncKeyState
    return any(g(vk) & 0x8000 for vk in (0x11, 0x10, 0x12, 0x5B, 0x5C))   # ctrl shift alt win


def paste(text: str) -> bool:
    """Type text where the cursor is: through the clipboard (fast, any language), then put the old one back."""
    if sys.platform != "win32":
        print(text)
        return True
    import ctypes
    for _ in range(50):                                     # wait until you've let go of Ctrl/Win
        if not _modifiers_down():
            break
        time.sleep(0.02)
    old = _clip_get()
    if not _clip_set(text):
        return False
    time.sleep(0.03)
    u = ctypes.windll.user32
    u.keybd_event(0x11, 0, 0, 0)
    u.keybd_event(0x56, 0, 0, 0)
    u.keybd_event(0x56, 0, 2, 0)
    u.keybd_event(0x11, 0, 2, 0)
    time.sleep(0.35)                                        # let the app read it before restoring
    if old is not None:
        _clip_set(old)
    return True


def target_app() -> str:
    try:
        from .screen import foreground
        title, proc, _ = foreground()
        return f"{proc.removesuffix('.exe') or 'an app'} ({title[:80]})" if (title or proc) else "an app"
    except Exception:
        return "an app"


class Transcriber:
    """Gemini: audio in, cleaned text out. Picks a working flash model on first use and remembers it."""

    def __init__(self, client, model: str | None = None, vocab: list[str] | None = None):
        self.client = client
        self.model = model
        self.vocab = VOCAB + list(vocab or [])

    def _candidates(self) -> list[str]:
        out = [self.model] if self.model else []
        out += [c for c in CANDIDATES if c not in out]
        try:
            names = [m.name.removeprefix("models/") for m in self.client.models.list()
                     if "generateContent" in (m.supported_actions or [])]
            flash = [n for n in names if "flash" in n and not any(x in n for x in ("live", "tts", "image", "lite"))]
            out = [c for c in out if c in names] + [n for n in flash if n not in out]
        except Exception:
            pass
        return out

    async def __call__(self, wav: bytes, app: str) -> str:
        from google.genai import types
        prompt = PROMPT.format(app=app, vocab=", ".join(self.vocab))
        parts = [types.Part.from_bytes(data=wav, mime_type="audio/wav"), prompt]
        quick = getattr(self, "_quick", True)
        cfg = types.GenerateContentConfig(
            temperature=0,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            **({"thinking_config": types.ThinkingConfig(thinking_budget=0)} if quick else {}))   # no "thinking": faster
        models = [self.model] if self.model else self._candidates()
        err = None
        for m in models:
            try:
                r = await self.client.aio.models.generate_content(model=m, contents=parts, config=cfg)
                self.model = m
                text = (r.text or "").strip().strip('"').strip()
                return "" if NONE in text or not text else text
            except Exception as e:                          # model missing/renamed: try the next one
                err = e
                if quick and "thinking" in str(e).lower():  # this model can't turn thinking off: retry with it on
                    self._quick = False
                    return await self(wav, app)
                if self.model:
                    self.model = None
                    return await self(wav, app)
        raise RuntimeError(f"no transcription model worked: {err}")


class Dictation:
    """The hold-to-dictate flow. Everything with side effects is injectable for tests."""

    def __init__(self, hub, transcribe, recorder_factory=Recorder, typer=paste, app=target_app,
                 beep: Callable[[str], None] = lambda k: None, log: Callable[..., None] = print):
        self.hub, self.transcribe = hub, transcribe
        self.recorder_factory, self.typer, self.app = recorder_factory, typer, app
        self.beep, self.log = beep, log
        self.rec = None
        self.cancelled = False

    def press(self) -> None:
        if getattr(self.hub.pet, "on_call", False):
            self.beep("busy")
            self.log("✍ Not dictating during a call.")
            return
        self.cancelled = False
        try:
            self.rec = self.recorder_factory()
            self.rec.start()
            self.hub.pet.set("listen", level=0, note="✍ dictando")
        except Exception as e:
            self.rec = None
            self.log(f"✍ Mic error: {e}")

    def cancel(self) -> None:                              # Ctrl+Win+<key> was a shortcut, not dictation
        self.cancelled = True
        if self.rec:
            self.rec.stop()
            self.rec = None
        self.hub.pet.set("idle", note="")

    async def release(self) -> str | None:
        if not self.rec:
            return None
        rec, self.rec = self.rec, None
        wav, secs = rec.stop()
        if self.cancelled or secs < MIN_SEC:
            self.hub.pet.set("idle", note="")
            return None
        if speech_seconds(wav) < MIN_SPEECH_SEC:            # nothing said: don't even ask Gemini (it can invent text)
            self.hub.pet.set("idle", note="")
            self.log("✍ Didn't hear anything: check the mic level.")
            return None
        self.beep("off")
        app = self.app()
        self.hub.pet.set("think", note="✍ escribiendo…")
        t0 = time.time()
        try:
            text = await self.transcribe(wav, app)
        except Exception as e:
            self.log(f"✍ Couldn't transcribe: {e}")
            self.hub.pet.set("idle", note="")
            return None
        self.hub.pet.set("idle", note="")
        if not text:
            return None
        ok = self.typer(text)
        self.hub.events.append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "ts": time.time(), "source": "voice",
                                "tool": "dictation", "args": {"seconds": round(secs, 1), "chars": len(text)},
                                "ok": bool(ok), "ms": round((time.time() - t0) * 1000)})
        self.log(f"✍ {len(text)} chars in {time.time() - t0:.1f}s")
        return text
