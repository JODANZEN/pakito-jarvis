"""Voice notes: his own audio -> word-for-word inbox note, never lost (no mic, no network)."""
import array
import asyncio
import copy
import json
import math
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from assistant import voice_notes as vn
from assistant.config import DEFAULTS
from assistant.hub import Hub

RATE = 16000


def tone(sec, amp=8000):
    n = int(sec * RATE)
    return array.array("h", (int(amp * math.sin(i / 3)) for i in range(n))).tobytes()


def silence(sec):
    return b"\x00\x00" * int(sec * RATE)


@pytest.fixture
def hub(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    return Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)


def inbox(hub):
    d = Path(hub.cfg["vault_path"]) / "inbox"
    return {p.name: p.read_text(encoding="utf-8") for p in d.glob("*.md")} if d.exists() else {}


def test_split_cuts_long_audio_at_pauses():
    # 5 minutes of "speech" with a 1 s pause every 10 s
    pcm = b"".join(tone(9) + silence(1) for _ in range(30))
    pieces = vn.split_pcm(pcm)
    assert len(pieces) == 3 and sum(map(len, pieces)) == len(pcm)
    assert all(len(p) / 2 / RATE <= 120 for p in pieces)
    for p in pieces[:-1]:                                    # each cut lands in a pause
        tail = array.array("h", p[-400:])
        assert max(map(abs, tail)) == 0


def test_small_pieces_stay_whole():
    """check notes on 30 s at 12 s pieces gave 73 slivers: the search window was the whole piece."""
    import random
    rnd = random.Random(0)
    pcm = b"".join(tone(rnd.uniform(0.5, 3)) + silence(rnd.uniform(0.1, 0.6)) for _ in range(20))
    pieces = vn.split_pcm(pcm, piece_sec=12)
    secs = [len(p) / 2 / RATE for p in pieces]
    assert sum(map(len, pieces)) == len(pcm)
    assert all(s <= 12 for s in secs) and all(s >= 9 for s in secs[:-1])
    assert len(pieces) <= math.ceil(len(pcm) / 2 / RATE / 9)


def test_note_from_audio_goes_word_for_word_to_inbox(hub):
    calls = []

    async def transcribe(wav, prompt):
        calls.append(len(wav))
        assert "word for word" in prompt and "summarize" in prompt
        return f"parte {len(calls)}"

    hub.note_taker = vn.NoteTaker(hub, transcribe)
    r = hub.note_taker.submit(tone(150), "Identidad y el Second Brain", "thought")
    assert r["saving"] and r["seconds"] == 150.0
    notes = inbox(hub)
    (name, body), = notes.items()
    assert "Identidad y el Second Brain" in name and "parte 1 parte 2" in body
    assert len(calls) == 2                                   # 150 s -> two pieces
    assert not list((hub.dir / "state" / "note_audio").glob("*"))   # audio deleted once it's safe


def test_failed_transcription_keeps_audio_and_retries(hub):
    async def broken(wav, prompt):
        raise ConnectionError("offline")

    hub.note_taker = vn.NoteTaker(hub, broken)
    hub.note_taker.submit(tone(10), "Idea larga", "idea", fallback="la idea de la mansión")
    (body,) = inbox(hub).values()
    assert "la idea de la mansión" in body and "note_audio" in body       # gist + where the audio is
    saved = list((hub.dir / "state" / "note_audio").glob("*.wav"))
    assert len(saved) == 1

    async def works(wav, prompt):
        return "todo lo que dije"

    hub.note_taker.transcribe = works
    out = asyncio.run(hub.note_taker.retry_pending())
    assert out[0]["words"] == 4
    assert any("todo lo que dije" in b for b in inbox(hub).values())
    assert not list((hub.dir / "state" / "note_audio").glob("*"))


def test_take_note_uses_his_audio_not_geminis_text(hub):
    async def transcribe(wav, prompt):
        return "mis palabras exactas, todas"

    hub.note_taker = vn.NoteTaker(hub, transcribe)
    hub.pending_note_audio = tone(5)
    r = hub.call("take_note", {"title": "Prueba", "text": "resumen corto"}, "voice")
    assert r["saving"]
    assert "mis palabras exactas, todas" in next(iter(inbox(hub).values()))


def test_take_note_before_he_says_it_waits(hub):
    hub.note_taker = vn.NoteTaker(hub, lambda w, p: None)
    hub.pending_note_audio = tone(1.0)                    # just "vamos a hacer una nota"
    r = hub.call("take_note", {"title": "Nueva nota"}, "voice")
    assert "error" in r and "listening" in r["error"]
    assert inbox(hub) == {}
    r = hub.call("take_note", {"text": ""}, "voice")         # Oct 19: empty text, no audio
    assert "error" in r


def test_typed_note_still_works(hub):
    r = hub.call("take_note", {"text": "comprar café", "title": "café"})
    assert r["noted"].endswith("café.md")


# ---------- the voice loop ----------
pytest.importorskip("google.genai")
from assistant.voice import VOICE_DEFAULTS, Conversation  # noqa: E402


class LoudAudio:
    async def read(self):
        await asyncio.sleep(0.01)
        return tone(1024 / RATE)

    async def write(self, data):
        pass


class RecSession:
    def __init__(self):
        self.tool_responses = []

    async def send_realtime_input(self, **kw):
        pass

    async def send_tool_response(self, function_responses):
        self.tool_responses.append(function_responses)


def conv(hub):
    return Conversation(hub, {**VOICE_DEFAULTS}, RecSession(), LoudAudio(), log=lambda *a, **k: None)


def hold(c, seconds):
    async def go():
        t = asyncio.create_task(c.mic_loop())
        c.press()
        await asyncio.sleep(seconds)
        c.release()
        await asyncio.sleep(0.45)                              # post-roll tail, then the turn closes
        c.done.set()
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(go())


def test_each_hold_is_kept_as_a_turn(hub):
    c = conv(hub)
    hold(c, 0.6)
    assert len(c.turns) == 1 and len(c.turns[0]["pcm"]) / 2 / RATE >= 0.6


def test_take_note_call_gets_the_turn_audio(hub):
    got = []

    async def transcribe(wav, prompt):
        got.append(len(wav))
        return "lo que dije"

    hub.note_taker = vn.NoteTaker(hub, transcribe)
    c = conv(hub)
    c.turns.append({"pcm": tone(3), "t": 0, "replied": False, "saved": False})
    c.turns.append({"pcm": tone(4), "t": 1, "replied": False, "saved": False})
    fc = NS(id="1", name="take_note", args={"title": "Idea", "turns": 2})
    asyncio.run(c._run_tools([fc]))
    assert got and got[0] > 2 * RATE * 6                     # both turns' audio (7 s)
    assert all(t["saved"] for t in c.turns)
    assert c.session.tool_responses[0][0].response["saving"] is True


def test_long_unanswered_turn_is_saved_when_conversation_ends(hub, monkeypatch):
    """Oct 19: 4.5 minutes, no reply, no transcript: all of it was lost. Now it goes to the inbox."""
    async def transcribe(wav, prompt):
        return "todo el monólogo"

    hub.note_taker = vn.NoteTaker(hub, transcribe)
    monkeypatch.setattr(vn, "RECOVER_SPEECH_SEC", 0.5)
    c = conv(hub)
    hold(c, 0.8)
    assert c.save_unanswered() == 1
    (body,) = inbox(hub).values()
    assert "todo el monólogo" in body and "no alcanzó a responder" in body
    assert c.save_unanswered() == 0                          # never twice


def test_answered_or_private_turns_are_not_saved(hub, monkeypatch):
    hub.note_taker = vn.NoteTaker(hub, lambda w, p: None)
    monkeypatch.setattr(vn, "RECOVER_SPEECH_SEC", 0.5)
    c = conv(hub)
    c.turns.append({"pcm": tone(2), "t": 0, "replied": True, "saved": False})
    assert c.save_unanswered() == 0
    c.turns.append({"pcm": tone(2), "t": 0, "replied": False, "saved": False})
    c.private = True
    assert c.save_unanswered() == 0


def test_dropped_connection_ends_conversation_out_loud(hub):
    logs = []
    c = Conversation(hub, {**VOICE_DEFAULTS}, RecSession(), LoudAudio(), log=lambda *a, **k: logs.append(a[0]))

    async def dies():
        raise ConnectionError("1011 internal error")

    async def go():
        await asyncio.wait_for(c._guard(dies), 1)
        return await c._try_reconnect()
    assert asyncio.run(go()) is False                        # no resumption handle: end, but say so
    assert c.done.is_set() and isinstance(c.failed, ConnectionError)
    assert any("Se cayó la conexión" in l for l in logs)
    assert "1011" in (hub.dir / "logs" / "errors.log").read_text(encoding="utf-8")


def test_dropped_connection_resumes_the_same_conversation(hub):
    logs, new = [], RecSession()
    c = Conversation(hub, {**VOICE_DEFAULTS}, RecSession(), LoudAudio(), log=lambda *a, **k: logs.append(a[0]))
    handles = []

    async def reconnect(handle):
        handles.append(handle)
        return new

    c.reconnect = reconnect

    async def go():
        await c.handle(NS(session_resumption_update=NS(resumable=True, new_handle="h-42"), go_away=None,
                          server_content=None, tool_call=None))
        await c.handle(NS(session_resumption_update=None, go_away=NS(time_left="10s"), server_content=None,
                          tool_call=None))

        async def dies():
            raise ConnectionError("closed after go_away")
        await c._guard(dies)
        return await c._try_reconnect()
    assert asyncio.run(go()) is True
    assert handles == ["h-42"] and c.session is new and not c.done.is_set() and c.failed is None
    assert not any("Se cayó" in l for l in logs)              # announced drop: reconnects quietly


def test_converse_reconnects_with_the_handle(hub, monkeypatch):
    from assistant import voice

    class FakeIO(LoudAudio):
        mic_name = "fake"

        def close(self):
            pass

    monkeypatch.setattr(voice, "PyAudioIO", FakeIO)
    monkeypatch.setattr(voice, "beep", lambda k: None)
    configs = []

    class Sess(RecSession):
        def __init__(self, n):
            super().__init__()
            self.n = n

        async def receive(self):
            if self.n == 1:
                yield NS(session_resumption_update=NS(resumable=True, new_handle="h-1"), go_away=None,
                         server_content=None, tool_call=None)
                raise ConnectionError("1011 deadline")
            await asyncio.sleep(0.05)
            if False:
                yield None

    class CM:
        def __init__(self, s):
            self.s = s

        async def __aenter__(self):
            return self.s

        async def __aexit__(self, *a):
            return False

    class Live:
        def connect(self, model, config):
            configs.append(config)
            return CM(Sess(len(configs)))

    client = NS(aio=NS(live=Live()))
    vcfg = {**VOICE_DEFAULTS, "idle_timeout_sec": 0.3}
    asyncio.run(asyncio.wait_for(voice.converse(hub, vcfg, client, {"key_down": False}, first_press=False), 5))
    assert len(configs) == 2 and configs[1]["session_resumption"] == {"handle": "h-1"}
    assert configs[0]["session_resumption"] == {}


def test_model_that_rejects_long_sessions_still_connects(hub, monkeypatch):
    from assistant import voice

    class FakeIO(LoudAudio):
        mic_name = "fake"

        def close(self):
            pass

    monkeypatch.setattr(voice, "PyAudioIO", FakeIO)
    monkeypatch.setattr(voice, "beep", lambda k: None)
    monkeypatch.setitem(voice.LONG_SESSIONS, "ok", True)
    configs = []

    class Sess(RecSession):
        async def receive(self):
            await asyncio.sleep(0.05)
            if False:
                yield None

    class CM:
        def __init__(self, cfg):
            self.cfg = cfg

        async def __aenter__(self):
            if "session_resumption" in self.cfg:
                raise RuntimeError("1007 invalid argument: session_resumption")
            return Sess()

        async def __aexit__(self, *a):
            return False

    class Live:
        def connect(self, model, config):
            configs.append(config)
            return CM(config)

    vcfg = {**VOICE_DEFAULTS, "idle_timeout_sec": 0.2}
    asyncio.run(asyncio.wait_for(voice.converse(hub, vcfg, NS(aio=NS(live=Live())), {}, first_press=False), 5))
    assert len(configs) == 2 and "session_resumption" not in configs[1]
    assert voice.LONG_SESSIONS["ok"] is False
    assert "session_resumption" not in voice.live_config(hub, vcfg)       # remembered for the next conversation


def test_pieces_cut_mid_sentence_are_joined_back():
    parts = ["Entonces, pues a veces sí me", "pongo a elaborar mucho. Y otra idea.", "Nuevo tema", ""]
    assert vn.join_pieces(parts) == "Entonces, pues a veces sí me pongo a elaborar mucho. Y otra idea.\n\nNuevo tema"


def test_quota_error_doesnt_turn_off_long_sessions(hub, monkeypatch):
    """Mac, Oct 22: a quota error got blamed on long sessions and switched them off."""
    from assistant import voice

    class FakeIO(LoudAudio):
        mic_name = "fake"

        def close(self):
            pass

    monkeypatch.setattr(voice, "PyAudioIO", FakeIO)
    monkeypatch.setattr(voice, "beep", lambda k: None)
    monkeypatch.setitem(voice.LONG_SESSIONS, "ok", True)
    tries = []

    class CM:
        async def __aenter__(self):
            raise RuntimeError("1011 None. You exceeded your current quota, please check your plan")

        async def __aexit__(self, *a):
            return False

    class Live:
        def connect(self, model, config):
            tries.append(config)
            return CM()

    with pytest.raises(RuntimeError, match="quota"):
        asyncio.run(voice.converse(hub, {**VOICE_DEFAULTS, "google_search": False}, NS(aio=NS(live=Live())), {},
                                   first_press=False))
    assert len(tries) == 1 and voice.LONG_SESSIONS["ok"] is True


def test_search_quota_falls_back_to_lookup(hub, monkeypatch, tmp_path):
    """Mac, Oct 22: every conversation died on '1011 ... exceeded your current quota' = Google Search's free limit."""
    from assistant import voice

    class FakeIO(LoudAudio):
        mic_name = "fake"

        def close(self):
            pass

    monkeypatch.setattr(voice, "PyAudioIO", FakeIO)
    monkeypatch.setattr(voice, "beep", lambda k: None)
    saved = {}
    monkeypatch.setattr(voice, "_save_voice_setting", lambda k, v: saved.update({k: v}))
    tries = []

    class Sess(RecSession):
        async def receive(self):
            await asyncio.sleep(0.05)
            if False:
                yield None

    class CM:
        def __init__(self, cfg):
            self.cfg = cfg

        async def __aenter__(self):
            if any("google_search" in t for t in self.cfg["tools"]):
                raise RuntimeError("1011 None. You exceeded your current quota, please check your plan")
            return Sess()

        async def __aexit__(self, *a):
            return False

    class Live:
        def connect(self, model, config):
            tries.append(config)
            return CM(config)

    vcfg = {**VOICE_DEFAULTS, "google_search": True, "idle_timeout_sec": 0.2}
    asyncio.run(asyncio.wait_for(voice.converse(hub, vcfg, NS(aio=NS(live=Live())), {}, first_press=False), 5))
    assert len(tries) == 2 and not any("google_search" in t for t in tries[1]["tools"])
    names = [f["name"] for f in tries[1]["tools"][0]["function_declarations"]]
    assert "lookup" in names                                   # the free search tool is offered instead
    assert vcfg["google_search"] is False and saved == {"google_search": False}
