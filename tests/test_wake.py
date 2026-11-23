"""Hands-free: the wake word wakes Pakito, and what he says right after it reaches Gemini."""
import asyncio
import copy

import numpy as np
import pytest

from assistant.config import DEFAULTS
from assistant.hub import Hub
from assistant.wake import FRAME, INSTALL, TapIO, WakeWord


class FakeModel:
    def __init__(self, scores):
        self.scores = list(scores)
        self.resets = 0

    def predict(self, x):
        assert x.dtype == np.int16 and len(x) == FRAME
        return {"hey_jarvis": self.scores.pop(0) if self.scores else 0.0}

    def reset(self):
        self.resets += 1


def frame(n):
    return bytes([n % 256, 0]) * FRAME


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def make(scores, **cfg):
    woke = []
    w = WakeWord(cfg, lambda: woke.append(True), model_loader=lambda word: (FakeModel(scores), "hey_jarvis"),
                 clock=Clock())
    w.model, w.name = w.model_loader("hey_jarvis")
    return w, woke


def test_two_frames_in_a_row_wake_it():
    w, woke = make([0.9, 0.1, 0.7, 0.8])
    assert [w.step(frame(i)) for i in range(4)] == [False, False, False, True]
    assert woke == [True] and w.sink is not None


def test_after_waking_the_audio_goes_to_the_conversation_not_the_model():
    w, woke = make([0.9, 0.9])
    w.step(frame(1)), w.step(frame(2))
    w.step(frame(3))                                    # what he says next: "pon música"
    got = [w.sink.get_nowait() for _ in range(w.sink.qsize())]
    assert got[-1] == frame(3) and frame(2) in got      # plus the tail of the word, in case he didn't pause
    w.release()
    assert w.sink is None and w.model.resets == 1


def test_not_twice_for_one_word():
    w, woke = make([0.9, 0.9, 0.9, 0.9, 0.9])
    for i in range(2):
        w.step(frame(i))
    w.release()
    for i in range(3):
        w.step(frame(i))                                # still the same "hey jarvis" (within 2 s)
    assert woke == [True]


def test_threshold_is_configurable():
    w, woke = make([0.4, 0.4], threshold=0.35)
    w.step(frame(0)), w.step(frame(1))
    assert woke == [True]


def test_without_openwakeword_it_says_how_to_install():
    def missing(word):
        raise ImportError("No module named 'openwakeword'")
    w = WakeWord({}, lambda: None, model_loader=missing)
    assert w.start() == {"error": INSTALL} and not w.running


def test_tap_reads_what_the_wake_word_heard():
    w, _ = make([0.9, 0.9])
    w.step(frame(1)), w.step(frame(2)), w.step(frame(7))
    io = TapIO(w, out_factory=lambda: None)

    async def go():
        return [await io.read() for _ in range(3)]
    assert asyncio.run(go())[-1] == frame(7)
    io.close()
    assert w.sink is None                               # back to waiting for the word


@pytest.fixture
def hub(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    return Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)


def test_hands_free_tool_turns_it_on_and_off(hub):
    saved = {}
    hub.save_local = lambda u: saved.update(u)
    assert "only works" in hub.call("hands_free", {"on": True}, "voice")["error"]
    started = []

    class W:
        label = "Hey Jarvis"

        def start(self):
            started.append(True)
            return {"listening_for": self.label}

        def stop(self):
            started.append(False)
    hub.wake = W()
    r = hub.call("hands_free", {"on": True}, "voice")
    assert r["hands_free"] and r["wake_word"] == "Hey Jarvis" and "end_conversation" in r["note"]
    assert saved == {"voice": {"wake": {"enabled": True}}}
    hub.call("hands_free", {"on": False}, "voice")
    assert started == [True, False] and saved["voice"]["wake"]["enabled"] is False


def test_conversation_keeps_what_he_said_while_connecting(hub):
    """Hands-free mode: audio arriving before the Gemini session exists is sent once it connects."""
    from assistant.voice import Conversation, VOICE_DEFAULTS

    class Audio:
        def __init__(self):
            self.n = 0

        async def read(self):
            self.n += 1
            await asyncio.sleep(0)
            return frame(self.n)

    class Session:
        def __init__(self):
            self.sent = []

        async def send_realtime_input(self, **kw):
            self.sent.append(kw)

    async def go():
        conv = Conversation(hub, {**VOICE_DEFAULTS, "mode": "toggle"}, None, Audio())
        task = asyncio.create_task(conv.mic_loop())
        for _ in range(5):
            await asyncio.sleep(0)
        conv.session = Session()
        for _ in range(5):
            await asyncio.sleep(0)
        conv.done.set()
        await asyncio.wait_for(task, 2)
        return conv.session.sent
    sent = asyncio.run(go())
    assert sent[0]["audio"]["data"] == frame(1)         # the very first frame wasn't lost


def test_agents_cant_switch_hands_free():
    from assistant.server import AGENT_HIDDEN
    assert "hands_free" in AGENT_HIDDEN
