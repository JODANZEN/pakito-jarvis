import asyncio
import copy
import json
import wave
import io

import pytest

from assistant import dictation as D
from assistant.config import DEFAULTS
from assistant.hub import Hub


@pytest.fixture
def hub(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    return Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)


class FakeRec:
    def __init__(self, secs=2.0):
        self.secs, self.started = secs, False

    def start(self):
        self.started = True

    def stop(self):
        import array, math
        loud = array.array("h", [int(9000 * math.sin(i / 7)) for i in range(int(16000 * self.secs))])
        return D.to_wav([loud.tobytes()]), self.secs


def make(hub, text="Nos vemos a las 6 en la biblioteca", secs=2.0):
    typed, beeps, seen = [], [], {}

    async def transcribe(wav, app):
        seen["wav"], seen["app"] = wav, app
        return text

    d = D.Dictation(hub, transcribe, recorder_factory=lambda: FakeRec(secs), typer=lambda t: typed.append(t) or True,
                    app=lambda: "WhatsApp (Mario)", beep=beeps.append, log=lambda *a: None)
    return d, typed, beeps, seen


def test_hold_talk_release_types_clean_text(hub):
    d, typed, beeps, seen = make(hub)
    d.press()
    assert hub.pet.snapshot()["state"] == "idle" or hub.pet.activity == "listen"
    out = asyncio.run(d.release())
    assert out == typed[0] == "Nos vemos a las 6 en la biblioteca"
    assert seen["app"] == "WhatsApp (Mario)"
    with wave.open(io.BytesIO(seen["wav"])) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
    ev = hub.events.read()[-1]
    assert ev["tool"] == "dictation" and ev["args"] == {"seconds": 2.0, "chars": len(out)}
    assert "biblioteca" not in json.dumps(ev)                     # what he said never hits the logs


def test_taps_shortcuts_silence_and_calls_type_nothing(hub):
    d, typed, beeps, _ = make(hub, secs=0.2)
    d.press()
    assert asyncio.run(d.release()) is None                        # too short: a tap
    d, typed2, _, _ = make(hub)
    d.press()
    d.cancel()                                                     # Ctrl+Win+D etc.
    assert asyncio.run(d.release()) is None
    d, typed3, _, _ = make(hub, text="")
    d.press()
    assert asyncio.run(d.release()) is None                        # no speech
    hub.pet.on_call = True
    d, typed4, beeps4, _ = make(hub)
    d.press()
    assert asyncio.run(d.release()) is None and beeps4 == ["busy"]
    assert not (typed or typed2 or typed3 or typed4)


def test_transcriber_falls_back_to_a_model_that_works():
    calls = []

    class Models:
        def list(self):
            from types import SimpleNamespace as NS
            return [NS(name="models/gemini-2.5-flash", supported_actions=["generateContent"]),
                    NS(name="models/gemini-3.1-flash-live-preview", supported_actions=["bidiGenerateContent"])]

    class AioModels:
        async def generate_content(self, model, contents, config):
            calls.append(model)
            if model != "gemini-2.5-flash":
                raise RuntimeError("404 not found")
            from types import SimpleNamespace as NS
            return NS(text='"Hola, ¿cómo vas?"')

    from types import SimpleNamespace as NS
    client = NS(models=Models(), aio=NS(models=AioModels()))
    t = D.Transcriber(client, model="gemini-9-flash")
    assert asyncio.run(t(b"wav", "WhatsApp")) == "Hola, ¿cómo vas?"
    assert t.model == "gemini-2.5-flash" and "gemini-9-flash" in calls
    assert asyncio.run(t(b"wav", "x")) and calls[-1] == "gemini-2.5-flash"   # remembered


def test_prompt_keeps_language_and_knows_his_words():
    p = D.PROMPT.format(app="Code (physics.py)", vocab=", ".join(D.VOCAB + ["Botana"]))
    assert "Never translate" in p and "Lagrangian" in p and "Botana" in p and "Code (physics.py)" in p


def test_type_text_tool_types_and_redacts(hub):
    typed = []
    hub.typer = lambda t: typed.append(t) or True
    r = hub.call("type_text", {"text": "Dale, nos vemos mañana a las 10"})
    assert r == {"typed": True, "chars": 31} and typed == ["Dale, nos vemos mañana a las 10"]
    ev = hub.events.read()[-1]
    assert ev["args"] == {"text": "<31 chars>"} and "mañana" not in json.dumps(ev)


def test_agents_cant_type_into_his_apps():
    from assistant import server
    assert "type_text" in server.AGENT_HIDDEN


def test_silence_never_reaches_gemini(hub):
    asked = []

    class Quiet(FakeRec):
        def stop(self):
            return D.to_wav([b"\x00\x00" * 32000]), 2.0

    async def transcribe(wav, app):
        asked.append(1)
        return "Hola, ¿cómo estás? Te escribo para…"          # what it invented from a tone
    typed = []
    d = D.Dictation(hub, transcribe, recorder_factory=Quiet, typer=typed.append, app=lambda: "x",
                    log=lambda *a: None)
    d.press()
    assert asyncio.run(d.release()) is None and not asked and not typed


def test_model_saying_none_types_nothing():
    from types import SimpleNamespace as NS

    class AioModels:
        async def generate_content(self, model, contents, config):
            assert config.thinking_config.thinking_budget == 0    # no thinking: faster
            return NS(text="<<none>>")
    t = D.Transcriber(NS(models=None, aio=NS(models=AioModels())), model="gemini-2.5-flash")
    assert asyncio.run(t(b"wav", "x")) == ""


def test_speech_seconds():
    import array, math
    talk = array.array("h", [int(8000 * math.sin(i / 5)) for i in range(16000)])
    assert D.speech_seconds(D.to_wav([talk.tobytes()])) > 0.9
    assert D.speech_seconds(D.to_wav([b"\x00\x00" * 16000])) == 0


# ---------- Oct 29: long essay dictation got slow, pasted out of order, and failures were invisible ----------
def make2(hub, transcribe, **kw):
    typed, beeps, notes, saved, copied = [], [], [], [], []
    d = D.Dictation(hub, transcribe, recorder_factory=lambda: FakeRec(3.0), typer=lambda t: typed.append(t) or True,
                    app=lambda: "Obsidian", beep=beeps.append, log=lambda *a: None,
                    notify=lambda t, m: notes.append((t, m)), fallback=lambda pcm, secs: saved.append((len(pcm), secs)),
                    copier=lambda t: copied.append(t) or True, **kw)
    return d, typed, beeps, notes, saved, copied


def test_dictations_paste_in_the_order_spoken_even_if_one_is_slow(hub):
    calls = []

    async def transcribe(wav, app):
        calls.append(len(calls))
        if len(calls) == 1:
            await asyncio.sleep(0.3)                           # the first one is slow
            return "primero"
        return "segundo"

    d, typed, *_ = make2(hub, transcribe)

    async def go():
        d.press()
        t1 = asyncio.create_task(d.release())
        await asyncio.sleep(0.01)
        d.press()
        t2 = asyncio.create_task(d.release())
        await asyncio.sleep(0.05)
        st = d.status()
        assert st["waiting"] == 2 and st["transcribing"]["seconds_of_audio"] == 3.0
        await asyncio.gather(t1, t2)
    asyncio.run(go())
    assert typed == ["primero", "segundo"]
    assert d.status()["waiting"] == 0 and d.status()["last"]["ok"]


def test_slow_gemini_gets_one_retry(hub):
    tries = []

    async def transcribe(wav, app):
        tries.append(1)
        if len(tries) == 1:
            await asyncio.sleep(5)                             # stuck
        return "salió al segundo intento"

    d, typed, beeps, *_ = make2(hub, transcribe, timeout=lambda secs: 0.2, retry_wait=0)
    d.press()
    assert asyncio.run(d.release()) == "salió al segundo intento"
    assert len(tries) == 2 and typed == ["salió al segundo intento"] and beeps[-1] == "done"


def test_failed_dictation_is_saved_and_says_so(hub):
    async def transcribe(wav, app):
        raise ConnectionError("503 model overloaded")

    d, typed, beeps, notes, saved, _ = make2(hub, transcribe, retry_wait=0)
    d.press()
    assert asyncio.run(d.release()) is None
    assert not typed and beeps[-1] == "busy"
    assert saved and saved[0][1] == 3.0                                    # the audio went to the inbox pipeline
    assert notes and "falló" in notes[0][0] and "inbox" in notes[0][1]
    ev = hub.events.read()[-1]
    assert ev["tool"] == "dictation" and ev["ok"] is False and "503" in ev["error"]   # failures are logged now
    assert d.status()["last"] == {"ok": False, "error": "ConnectionError: 503 model overloaded", "saved_to_inbox": True}


def test_switched_windows_meanwhile_goes_to_clipboard(hub):
    async def transcribe(wav, app):
        return "texto del ensayo"

    windows = iter([111, 222])                                             # at release, then at paste time
    d, typed, beeps, notes, saved, copied = make2(hub, transcribe, window=lambda: next(windows))
    d.press()
    asyncio.run(d.release())
    assert copied == ["texto del ensayo"] and not typed
    assert any("portapapeles" in m for _, m in notes)


def test_pakito_can_check_if_its_still_processing(hub):
    async def transcribe(wav, app):
        return "x"

    hub.dictation, *_ = make2(hub, transcribe)
    r = hub.call("dictation_status", {}, "voice")
    assert r["waiting"] == 0 and "Nothing pending" in r["note"]
