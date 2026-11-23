"""Hands-free: say the wake word ("Hey Jarvis" for now, "Pakito" once trained) and Pakito starts listening, no
hotkey. Then it's a normal hands-free conversation (Gemini decides when you stopped talking) that ends with
"gracias" or after a few quiet seconds, and Pakito goes back to waiting for its name.

The wake word runs entirely on this computer (openWakeWord, ~2 ms of CPU per 80 ms of audio). Nothing goes to
Google until it hears the word. This module owns the mic while hands-free is on: the conversation it starts reads
from the same stream, so the words right after the wake word ("Hey Jarvis, pon música") aren't lost.

Install once: `run-pakito.bat pip install openwakeword` (or `<python> -m pip install openwakeword`).
"""
from __future__ import annotations

import asyncio
import collections
import os
import queue
import threading
import time
from typing import Callable

FRAME = 1280                         # 80 ms at 16 kHz: what openWakeWord wants per step
RATE = 16000
INSTALL = "Hands-free needs openWakeWord: run `run-pakito.bat pip install openwakeword`, then restart Pakito."
WAKE_DEFAULTS = {
    "enabled": False,
    "word": "hey_jarvis",            # a built-in model name, or the path to a trained .onnx ("pakito.onnx")
    "threshold": 0.5,                # higher = fewer false wake-ups, but it may miss you; `check wake` helps tune it
    "frames": 2,                     # this many 80 ms frames in a row above the threshold
    "idle_sec": 15,                  # a hands-free conversation hangs up after this much quiet
}


def load_model(word: str):
    """-> (model, name). Downloads the built-in models the first time (~6 MB from GitHub)."""
    from openwakeword.model import Model
    if word.endswith((".onnx", ".tflite")):
        path = os.path.expanduser(word)
        return Model(wakeword_models=[path], inference_framework="onnx"), os.path.splitext(os.path.basename(path))[0]
    import openwakeword
    from openwakeword.utils import download_models
    need = [m for m in openwakeword.MODELS if m == word]
    if need and not os.path.exists(openwakeword.MODELS[word]["model_path"].replace(".tflite", ".onnx")):
        download_models([os.path.basename(openwakeword.MODELS[word]["model_path"]).replace(".tflite", "")])
    return Model(wakeword_models=[word], inference_framework="onnx"), word


def open_mic():
    """A 16 kHz mono input stream on the mic Pakito uses (same choice as voice.py)."""
    import pyaudio
    from .voice import pick_mic, shared_pyaudio
    pa = shared_pyaudio()
    mic = pick_mic(pa)
    return pa.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True, input_device_index=mic["index"],
                   frames_per_buffer=FRAME)


class WakeWord:
    def __init__(self, cfg: dict | None, on_wake: Callable[[], None], mic_factory: Callable = open_mic,
                 model_loader: Callable = load_model, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print):
        self.cfg = {**WAKE_DEFAULTS, **(cfg or {})}
        self.on_wake, self.mic_factory, self.model_loader = on_wake, mic_factory, model_loader
        self.clock, self.log = clock, log
        self.model, self.name = None, None
        self.sink: queue.Queue | None = None          # set while a conversation is using the mic
        self.paused = threading.Event()               # on a call: let go of the mic
        self.stop_ev = threading.Event()
        self.thread: threading.Thread | None = None
        self.stream = None
        self.hits = 0
        self.last_wake = -99.0
        self.ring: collections.deque = collections.deque(maxlen=4)   # ~0.3 s: the start of what follows the word
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    @property
    def label(self) -> str:
        return (self.name or self.cfg["word"]).replace("_", " ").title().replace(".Onnx", "")

    # ---------- on / off ----------
    def start(self) -> dict:
        if self.running:
            return {"listening_for": self.label}
        if self.model is None:
            try:
                self.model, self.name = self.model_loader(self.cfg["word"])
            except ImportError:
                self.error = INSTALL
                return {"error": INSTALL}
            except Exception as e:
                self.error = f"The wake word model didn't load: {e}"
                return {"error": self.error}
        self.stop_ev.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="wake-word")
        self.thread.start()
        return {"listening_for": self.label}

    def stop(self) -> None:
        self.stop_ev.set()
        self.release()

    def pause(self) -> None:
        self.paused.set()

    def resume(self) -> None:
        self.paused.clear()

    # ---------- the mic loop ----------
    def _close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _run(self) -> None:
        import numpy as np
        while not self.stop_ev.is_set():
            if self.paused.is_set():
                self._close()
                time.sleep(0.2)
                continue
            if self.stream is None:
                try:
                    self.stream = self.mic_factory()
                except Exception as e:
                    self.error = f"Couldn't open the mic for hands-free: {e}"
                    self.log(f"\n⚠ {self.error}")
                    time.sleep(5)
                    continue
            try:
                frame = self.stream.read(FRAME, exception_on_overflow=False)
            except Exception:
                self._close()
                time.sleep(0.5)
                continue
            self.step(frame, np)
        self._close()

    def step(self, frame: bytes, np=None) -> bool:
        """One 80 ms frame. Returns True when it woke up."""
        sink = self.sink
        if sink is not None:                           # a conversation is listening: hand it the audio
            sink.put(frame)
            return False
        if np is None:
            import numpy as np
        self.ring.append(frame)
        score = float(self.model.predict(np.frombuffer(frame, dtype=np.int16)).get(self.name, 0.0))
        self.hits = self.hits + 1 if score >= self.cfg["threshold"] else 0
        if self.hits >= self.cfg["frames"] and self.clock() - self.last_wake > 2.0:
            self.hits, self.last_wake = 0, self.clock()
            self.sink = queue.Queue()
            for f in list(self.ring)[-2:]:             # the tail end of the word: the command may start right away
                self.sink.put(f)
            self.ring.clear()
            self.on_wake()
            return True
        return False

    def release(self) -> None:
        """The conversation ended: back to waiting for the word."""
        self.sink = None
        self.hits = 0
        if self.model is not None:
            try:
                self.model.reset()
            except Exception:
                pass


class TapIO:
    """The audio side of a hands-free conversation: mic frames from the wake-word stream, speaker output of its own."""

    def __init__(self, wake: WakeWord, out_factory: Callable | None = None):
        self.wake = wake
        self.q = wake.sink or queue.Queue()
        self.mic_name = "hands-free"
        self._out_factory = out_factory
        self.out = None

    async def read(self) -> bytes:
        def get():
            try:
                return self.q.get(timeout=1.0)
            except queue.Empty:
                return b"\x00\x00" * FRAME              # mic paused (a call): silence keeps the loop alive
        return await asyncio.to_thread(get)

    async def write(self, data: bytes) -> None:
        if self.out is None:
            self.out = (self._out_factory or _open_speaker)()
        await asyncio.to_thread(self.out.write, data)

    def close(self) -> None:
        if self.out is not None:
            try:
                self.out.stop_stream()
                self.out.close()
            except Exception:
                pass
        self.wake.release()


def _open_speaker():
    import pyaudio
    from .voice import RECEIVE_RATE, shared_pyaudio
    return shared_pyaudio().open(format=pyaudio.paInt16, channels=1, rate=RECEIVE_RATE, output=True)
