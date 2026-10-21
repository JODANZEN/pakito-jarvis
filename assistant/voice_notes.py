"""Voice notes that can't get lost.

Gemini used to retype everything he said into take_note. Long notes came out summarized, and a turn that got
no answer was gone. Now:
  * the voice loop keeps HIS OWN recording of each turn (Conversation.turns);
  * take_note hands that recording here. It's written to disk first (state/note_audio/), transcribed in ~2-minute
    pieces in the background by the same Gemini flash model dictation uses, in his words, nothing summarized.
    Then it goes to the inbox;
  * if transcription fails, the inbox still gets a note (Gemini's text if there was any) and the audio stays on
    disk. Pakito retries it the next time it starts;
  * a long turn that never got an answer (connection dropped, Gemini stuck) is saved the same way when the
    conversation ends, as "Nota recuperada". Off-the-record conversations are never saved.
"""
from __future__ import annotations

import array
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from .dictation import RATE, SPEECH_PEAK, to_wav

FRAME = 1024                        # samples per analysis frame (64 ms at 16 kHz)
PIECE_SEC = 120                     # transcribe at most this much audio per request
MIN_NOTE_SPEECH_SEC = 2.0           # less voice than this in the turn = he hasn't said the note yet
RECOVER_SPEECH_SEC = 15.0           # unanswered turns with at least this much voice get saved on their own

NOTE_PROMPT = """Transcribe this voice note word for word. He's dictating a note for his Second Brain.
Rules:
- Keep EVERYTHING he said, in order, in the language he used (Spanish, English, or mixed). Never translate,
  summarize, shorten, reorder or add anything. It's a transcript, not a summary.
- Light cleanup only: punctuation, accents, capitalization; drop filler (eh, em, um, "o sea"/"like" as filler) and
  false starts; apply spoken self-corrections ("el martes, no, el miércoles" -> "el miércoles").
- Drop a command at the very start that's addressed to the assistant ("Pakito, toma nota", "anota esto",
  "take a note"). Keep everything after it.
- Start a new paragraph when he moves to a new idea.
- Spell these as written: {vocab}.
- If there's no clear human speech, output exactly <<none>>."""


def speech_seconds_pcm(pcm: bytes, rate: int = RATE) -> float:
    a = array.array("h", pcm[: len(pcm) // 2 * 2])
    voiced = 0
    for i in range(0, len(a), FRAME):
        f = a[i:i + FRAME]
        if f and max(abs(min(f)), abs(max(f))) / 32768 >= SPEECH_PEAK:
            voiced += len(f)
    return voiced / rate


def split_pcm(pcm: bytes, rate: int = RATE, piece_sec: float = PIECE_SEC, search_sec: float = 12) -> list[bytes]:
    """Cut long audio into pieces of at most piece_sec, each cut at the quietest moment of its last search_sec
    (a pause between sentences), so no word is split in half."""
    bps = 2
    a = array.array("h", pcm[: len(pcm) // 2 * 2])
    n, limit = len(a), int(piece_sec * rate)
    look = min(int(search_sec * rate), limit // 4)      # only look near the end: pieces stay >= 3/4 of piece_sec
    pieces, start = [], 0
    while n - start > limit:
        lo, hi = start + limit - look, start + limit
        best, best_peak = hi, None
        for i in range(lo, hi - FRAME, FRAME):
            f = a[i:i + FRAME]
            p = max(abs(min(f)), abs(max(f)))
            if best_peak is None or p < best_peak:
                best, best_peak = i + FRAME // 2, p
        pieces.append(pcm[start * bps:best * bps])
        start = best
    pieces.append(pcm[start * bps:n * bps])
    return [p for p in pieces if p]


def join_pieces(parts: list[str]) -> str:
    """Pieces are cut at pauses, often mid-sentence ("sí me" | "pongo a elaborar"): continue with a space,
    and only start a new paragraph where a sentence really ended."""
    out = ""
    for p in (x.strip() for x in parts):
        if not p:
            continue
        if not out:
            out = p
        elif out.rstrip()[-1:] in ".?!…:;\"»)":
            out += "\n\n" + p
        else:
            out += " " + p
    return out


class NoteTaker:
    """transcribe(wav, prompt) -> text. loop: the voice event loop (work is scheduled onto it from tool threads)."""

    def __init__(self, hub, transcribe: Callable[[bytes, str], Awaitable[str]], vocab: list[str] | None = None,
                 loop: asyncio.AbstractEventLoop | None = None, log: Callable[..., None] = print):
        self.hub, self.transcribe, self.loop, self.log = hub, transcribe, loop, log
        self.dir = Path(hub.dir) / "state" / "note_audio"
        self.prompt = NOTE_PROMPT.format(vocab=", ".join(vocab or []))
        self.running: list = []

    # ---- called from the tool thread (hub.call) ----
    def submit(self, pcm: bytes, title: str | None = None, kind: str = "note", fallback: str = "",
               recovered: bool = False) -> dict:
        """Write the audio to disk now (so it can't be lost), transcribe in the background."""
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(self.hub.clock()).strftime("%Y-%m-%d_%H%M%S")
        base = self.dir / stamp
        n = 1
        while base.with_suffix(".wav").exists():
            n += 1
            base = self.dir / f"{stamp}-{n}"
        base.with_suffix(".wav").write_bytes(to_wav([pcm]))
        job = {"wav": str(base.with_suffix(".wav")), "title": title, "kind": kind, "fallback": fallback or "",
               "recovered": recovered, "created": self.hub.clock(), "seconds": round(len(pcm) / 2 / RATE, 1),
               "tries": 0}
        base.with_suffix(".json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        self._schedule(job)
        return {"saving": True, "seconds": job["seconds"]}

    def _schedule(self, job: dict) -> None:
        coro = self.process(job)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self.loop is not None and running is not self.loop:
            self.running.append(asyncio.run_coroutine_threadsafe(coro, self.loop))
        elif running is not None:
            self.running.append(running.create_task(coro))
        else:
            asyncio.run(coro)                      # no loop at all (tests, text mode): do it now

    # ---- the background work ----
    async def process(self, job: dict) -> dict:
        wav_path = Path(job["wav"])
        meta_path = wav_path.with_suffix(".json")
        pcm = _read_pcm(wav_path)
        title = job.get("title") or ("Nota recuperada" if job.get("recovered") else None)
        self.hub.pet.set(note="📝 guardando nota…")
        parts, error = [], None
        try:
            for piece in split_pcm(pcm):
                if speech_seconds_pcm(piece) < 0.3:
                    continue
                t = (await self.transcribe(to_wav([piece]), self.prompt)).strip()
                if t:
                    parts.append(t)
        except Exception as e:                     # network, quota, model: keep the audio, retry later
            error = f"{type(e).__name__}: {str(e)[:120]}"
        text = join_pieces(parts)
        self.hub.pet.set(note="")
        if text and not error:
            if job.get("recovered"):
                text = ("*(Pakito no alcanzó a responder este turno, así que lo guardó del audio.)*\n\n" + text)
            res = self.hub.inbox.add(text, title, job.get("kind", "note"))
            for p in (wav_path, meta_path):
                try:
                    p.unlink()
                except OSError:
                    pass
            self.hub.notify("📝 Nota guardada", f"{res.get('noted', '')} ({len(text.split())} palabras)")
            self.hub.pet.say("pakito", f"📝 Nota guardada: {len(text.split())} palabras")
            self._log(job, True, words=len(text.split()))
            return {"saved": res.get("noted"), "words": len(text.split())}
        # failed (or no speech found): never lose it
        job["tries"] = job.get("tries", 0) + 1
        job["error"] = error or "no speech found"
        meta_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        if not job.get("placeholder_written"):
            body = (job.get("fallback") or "").strip()
            body = (body + "\n\n" if body else "") + (
                f"🎙 *Pakito no pudo transcribir el audio todavía ({job['error']}). Está guardado en "
                f"`Assistant/state/note_audio/{wav_path.name}` y lo reintenta cuando arranque.*")
            self.hub.inbox.add(body, title, job.get("kind", "note"))
            job["placeholder_written"] = True
            meta_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        self.hub.notify("📝 Nota guardada a medias", "El audio quedó guardado; Pakito lo reintenta al arrancar.")
        self._log(job, False, error=job["error"])
        return {"error": job["error"], "audio": str(wav_path)}

    def _log(self, job: dict, ok: bool, **extra) -> None:
        try:
            self.hub.events.append({"time": datetime.fromtimestamp(self.hub.clock()).isoformat(timespec="seconds"),
                                    "ts": self.hub.clock(), "source": "voice", "tool": "note_audio",
                                    "args": {"seconds": job.get("seconds"), "recovered": job.get("recovered", False),
                                             "title": job.get("title")}, "ok": ok, "result": extra})
        except Exception:
            pass

    async def retry_pending(self, max_tries: int = 5) -> list[dict]:
        """Audio from notes that failed to transcribe (or Pakito closed mid-way): try again."""
        out = []
        for meta in sorted(self.dir.glob("*.json")) if self.dir.exists() else []:
            try:
                job = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("tries", 0) >= max_tries or not Path(job.get("wav", "")).exists():
                continue
            if job.get("placeholder_written"):
                job["title"] = (job.get("title") or "Nota") + " (transcrita)"
            out.append(await self.process(job))
        return out


def _read_pcm(path: Path) -> bytes:
    import wave
    with wave.open(str(path)) as w:
        return w.readframes(w.getnframes())
