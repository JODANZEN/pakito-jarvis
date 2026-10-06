"""Voice logic tests with a fake Gemini session and fake audio (no mic, no network)."""
import asyncio
import copy
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("google.genai")

from google.genai import types  # noqa: E402

from assistant.config import DEFAULTS  # noqa: E402
from assistant.hub import Hub  # noqa: E402
from assistant.voice import VOICE_DEFAULTS, Conversation, HotkeyTracker, live_config  # noqa: E402


class FakeSession:
    def __init__(self, script=()):
        self.script = list(script)
        self.sent_audio, self.tool_responses = [], []

    async def send_realtime_input(self, audio):
        self.sent_audio.append(audio)

    async def send_tool_response(self, function_responses):
        self.tool_responses.append(function_responses)

    async def receive(self):
        while self.script:
            yield self.script.pop(0)
        await asyncio.sleep(0.05)


class FakeAudio:
    def __init__(self):
        self.played = []

    async def read(self):
        await asyncio.sleep(0.01)
        return b"\x00" * 2048

    async def write(self, data):
        self.played.append(data)


def heard(text):
    return NS(server_content=NS(input_transcription=NS(text=text), model_turn=None, output_transcription=None,
                                interrupted=None, turn_complete=None), tool_call=None)


def call(name, **args):
    return NS(server_content=None, tool_call=NS(function_calls=[NS(id="c1", name=name, args=args)]))


def spoke(audio=b"\x01\x02", text="ok", done=True):
    part = NS(inline_data=NS(data=audio))
    return NS(server_content=NS(input_transcription=None, model_turn=NS(parts=[part]),
                                output_transcription=NS(text=text), interrupted=None, turn_complete=done), tool_call=None)


@pytest.fixture
def hub(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    return Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)


def make(hub, session, **v):
    vcfg = {**VOICE_DEFAULTS, **v}
    return Conversation(hub, vcfg, session, FakeAudio(), log=lambda *a, **k: None)


def test_live_config_validates_with_sdk(hub):
    cfg = types.LiveConnectConfig.model_validate(live_config(hub, VOICE_DEFAULTS))
    names = [f.name for f in cfg.tools[1].function_declarations]
    assert "start_focus" in names and "end_conversation" in names
    assert cfg.tools[0].google_search is not None
    text = cfg.system_instruction.parts[0].text if hasattr(cfg.system_instruction, "parts") else cfg.system_instruction
    assert "lock in" in text and "physics reading" in text


def test_tool_call_runs_hub_and_replies(hub):
    s = FakeSession()
    c = make(hub, s)

    async def go():
        await c.handle(heard("start focus for cse 355"))
        await c.handle(call("start_focus", label="CSE 355"))

    asyncio.run(go())
    assert hub.focus.phase == "focus" and hub.focus.block["label"] == "CSE 355"
    (resp,), = s.tool_responses
    assert resp.id == "c1" and resp.name == "start_focus" and resp.response["started"] is True
    ev = hub.events.read()[-1]
    assert ev["source"] == "voice" and ev["episode"] is not None
    hub.focus.stop()


def test_voice_feeds_the_learner(hub):
    async def once():
        s = FakeSession()
        c = make(hub, s)
        await c.handle(heard("ethics post"))
        await c.handle(call("open_url", url="https://canvas.asu.edu/courses/1"))
        await c.handle(spoke())
        hub.end_request()

    for _ in range(3):
        asyncio.run(once())
    sug = hub.call("routine_suggestions")["suggestions"]
    assert sug and sug[0]["name"] == "ethics post"


def test_end_conversation_hangs_up_after_audio(hub):
    s = FakeSession([heard("thanks that's all"), call("end_conversation"), spoke(b"\x05" * 10, "bye")])
    c = make(hub, s, idle_timeout_sec=5)
    asyncio.run(asyncio.wait_for(c.run(), timeout=3))
    assert c.ending and c.done.is_set()
    assert c.audio.played == [b"\x05" * 10]


def test_idle_timeout_ends(hub):
    c = make(hub, FakeSession(), idle_timeout_sec=0.3)
    asyncio.run(asyncio.wait_for(c.run(), timeout=3))
    assert c.done.is_set() and not c.ending


def test_half_duplex_mutes_mic_while_talking(hub):
    s = FakeSession()
    c = make(hub, s, barge_in=False, mode="toggle")
    c.last_play = c.clock()          # it's "talking" right now

    async def go():
        t = asyncio.create_task(c.mic_loop())
        await asyncio.sleep(0.1)
        c.done.set()
        await t

    asyncio.run(go())
    assert s.sent_audio == []
    c2 = make(hub, s, barge_in=True, mode="toggle")
    c2.last_play = c2.clock()

    async def go2():
        t = asyncio.create_task(c2.mic_loop())
        await asyncio.sleep(0.1)
        c2.done.set()
        await t

    asyncio.run(go2())
    assert s.sent_audio and s.sent_audio[0]["mime_type"] == "audio/pcm;rate=16000"


def test_interrupt_flushes_queued_audio(hub):
    c = make(hub, FakeSession())

    async def go():
        await c.handle(spoke(b"a", done=False))
        await c.handle(spoke(b"b", done=False))
        await c.handle(NS(server_content=NS(input_transcription=None, model_turn=None, output_transcription=None,
                                            interrupted=True, turn_complete=None), tool_call=None))

    asyncio.run(go())
    assert c.out_q.empty()


def test_search_off_uses_lookup_tool(hub):
    cfg = live_config(hub, {**VOICE_DEFAULTS, "google_search": False})
    assert all("google_search" not in t for t in cfg["tools"])
    names = [d["name"] for d in cfg["tools"][0]["function_declarations"]]
    assert "lookup" in names
    assert "call the lookup tool" in cfg["system_instruction"]
    types.LiveConnectConfig.model_validate(cfg)


def test_prompt_has_recent_actions_and_waiting_claude(hub):
    hub.call("open_url", {"url": "https://canvas.asu.edu"})
    hub.claude.jobs["j1"] = {"id": "j1", "question": "best flights", "status": "done", "spoken": "Tuesday is cheapest.",
                             "delivered": False}
    p = live_config(hub, VOICE_DEFAULTS)["system_instruction"]
    assert "opened canvas.asu.edu" in p and "Tuesday is cheapest" in p and "ask_claude" in p


def test_claude_answer_is_spoken_mid_conversation(hub):
    sent = []

    class S(FakeSession):
        async def send_realtime_input(self, audio=None, text=None):
            if text:
                sent.append(text)

    c = make(hub, S(), idle_timeout_sec=0.3)
    job = {"id": "j", "question": "q?", "status": "running", "started": 0, "delivered": False}
    hub.claude.jobs["j"] = job                                      # pending -> no idle hangup

    async def go():
        t = asyncio.create_task(c.run())
        await asyncio.sleep(0.8)
        assert not c.done.is_set()                                  # held open while Claude thinks
        job.update(status="done", spoken="Answer!")
        assert c.on_claude(job) is True
        await asyncio.sleep(0.1)
        await asyncio.wait_for(t, 2)                                # then idles out normally

    asyncio.run(go())
    assert sent and "Answer!" in sent[0]
    assert c.on_claude(job) is False                                # conversation over -> toast instead


# ---------------- push-to-talk ----------------

class RecSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.log = []

    async def send_realtime_input(self, audio=None, text=None, activity_start=None, activity_end=None):
        if activity_start is not None:
            self.log.append("start")
        elif activity_end is not None:
            self.log.append("end")
        elif audio is not None:
            self.log.append("audio")
        elif text is not None:
            self.log.append("text")


def run_mic(c, script):
    async def go():
        t = asyncio.create_task(c.mic_loop())
        await script()
        await asyncio.sleep(0.05)
        c.done.set()
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(go())


def test_ptt_config_disables_auto_vad(hub):
    cfg = types.LiveConnectConfig.model_validate(live_config(hub, VOICE_DEFAULTS))
    assert cfg.realtime_input_config.automatic_activity_detection.disabled is True
    toggle = live_config(hub, {**VOICE_DEFAULTS, "mode": "toggle"})
    assert "realtime_input_config" not in toggle


def test_ptt_hold_sends_one_turn_with_preroll(hub):
    s = RecSession()
    c = make(hub, s)

    async def script():
        await asyncio.sleep(0.15)          # mic running, key up: fills the pre-roll ring
        assert len(c.ring) > 0 and s.log == []
        c.press()
        assert len(c.pre) > 0              # pre-roll captured so the first syllable isn't cut
        await asyncio.sleep(0.5)
        assert c.release() is False

    run_mic(c, script)
    assert s.log[0] == "start" and s.log[-1] == "end"
    assert s.log.count("start") == 1 and s.log.count("audio") >= 10


def test_ptt_tap_sends_nothing(hub):
    s = RecSession()
    c = make(hub, s)

    async def script():
        c.press()
        await asyncio.sleep(0.1)
        assert c.release() is True

    run_mic(c, script)
    assert s.log == []


def test_ptt_speech_before_connect_is_sent_after(hub):
    s = RecSession()
    c = make(hub, None)

    async def script():
        c.press()
        await asyncio.sleep(0.45)
        c.release()                        # done talking before the session connected
        assert c.pending_turn
        c.session = s                      # now it connects
        await asyncio.sleep(0.1)

    run_mic(c, script)
    assert s.log[0] == "start" and s.log[-1] == "end" and "audio" in s.log
    assert c.pending_turn is None


def test_ptt_press_cuts_off_playback(hub):
    c = make(hub, RecSession())
    c.out_q.put_nowait(b"a")
    c.out_q.put_nowait(b"b")
    c.press()
    assert c.out_q.empty()


def test_ptt_holding_prevents_idle_hangup(hub):
    c = make(hub, FakeSession(), idle_timeout_sec=0.2)

    async def go():
        c.holding = True
        t = asyncio.create_task(c.watchdog())
        await asyncio.sleep(0.5)
        assert not c.done.is_set()
        c.holding = False
        await asyncio.sleep(0.5)
        assert c.done.is_set()
        await t

    asyncio.run(go())


def test_hotkey_tracker():
    t = HotkeyTracker("right ctrl")
    assert t.feed("ctrl", True) is None            # left ctrl doesn't count
    t.feed("ctrl", False)
    assert t.feed("right ctrl", True) == "press"
    assert t.feed("right ctrl", True) is None      # auto-repeat
    assert t.feed("right ctrl", False) == "release"
    t = HotkeyTracker("ctrl+space")
    assert t.feed("space", True) is None
    assert t.feed("right ctrl", True) == "press"   # either ctrl works in a combo
    assert t.feed("space", False) == "release"
    t = HotkeyTracker("F8")
    assert t.feed("f8", True) == "press" and t.feed("f8", False) == "release"


def test_hotkey_ctrl_alt_ignores_shortcuts():
    t = HotkeyTracker("ctrl+alt")
    assert t.feed("ctrl", True) is None
    assert t.feed("alt", True) == "press"
    assert t.feed("alt", False) == "release"
    assert t.feed("ctrl", False) is None
    # Ctrl+Alt+Del: becomes a cancel, and doesn't re-trigger until both are let go
    t.feed("ctrl", True)
    assert t.feed("alt", True) == "press"
    assert t.feed("delete", True) == "cancel"
    assert t.feed("delete", False) is None
    assert t.feed("alt", False) is None
    assert t.feed("ctrl", False) is None
    assert t.feed("ctrl", True) is None and t.feed("alt", True) == "press"
    # holding another key first (typing with ctrl+alt chord) never triggers
    t = HotkeyTracker("ctrl+alt")
    t.feed("shift", True)
    t.feed("ctrl", True)
    assert t.feed("alt", True) is None
