"""Voice logic tests with a fake Gemini session and fake audio (no mic, no network)."""
import array
import asyncio
import copy
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("google.genai")

from google.genai import types  # noqa: E402

from assistant.config import DEFAULTS  # noqa: E402
from assistant.hub import Hub  # noqa: E402
from assistant.voice import VOICE_DEFAULTS, Conversation, HotkeyTracker, KeyPoller, live_config  # noqa: E402


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
    def __init__(self, sample=0):
        self.played = []
        self.sample = sample

    async def read(self):
        await asyncio.sleep(0.01)
        return array.array("h", [self.sample] * 1024).tobytes()

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
        n = s.log.count("audio")
        await asyncio.sleep(0.45)                     # post-roll tail keeps sending, then ends
        assert s.log.count("audio") > n

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
    # a stale key from before (e.g. a key-up Windows never sent) must NOT block talking
    t = HotkeyTracker("ctrl+alt")
    t.feed("tab", True)
    t.feed("ctrl", True)
    assert t.feed("alt", True) == "press"


def test_hotkey_release_reported_under_another_name():
    # the bug from Oct 5: Ctrl+Alt acts like AltGr, key-up arrives as "right alt"/"alt gr"
    t = HotkeyTracker("ctrl+alt")
    t.feed("ctrl", True)
    assert t.feed("alt", True) == "press"
    assert t.feed("alt gr", False) == "release"           # any alt variant releasing ends the hold
    t.feed("ctrl", False)
    t.feed("ctrl", True)
    assert t.feed("alt", True) == "press"                 # and it works again right away


def test_hotkey_sync_ends_a_stuck_hold():
    t = HotkeyTracker("ctrl+alt")
    t.feed("ctrl", True)
    assert t.feed("alt", True) == "press"
    # key-ups never arrive... but the real keyboard says alt is up
    assert t.sync(lambda p: p != "alt") == "release"
    assert t.sync(lambda p: False) is None                 # nothing left to end
    assert not t.active and not t.down
    t.feed("ctrl", True)
    assert t.feed("alt", True) == "press"                 # usable again
    assert t.sync(lambda p: True) is None                  # still physically held: keep going
    assert t.sync(lambda p: None) is None                  # can't tell (non-Windows): trust events



def test_latency_printed_after_release(hub):
    logs = []
    c = Conversation(hub, VOICE_DEFAULTS, RecSession(), FakeAudio(), log=lambda *a, **k: logs.append(a[0] if a else ""))
    c.press()
    c.press_t -= 1                                          # held 1 s
    c.release()

    async def go():
        await c.handle(spoke(b"x", done=False))
        await c.handle(spoke(b"y", done=False))

    asyncio.run(go())
    assert sum(1 for l in logs if "⏱" in str(l)) == 1      # once per turn, on the first audio



def test_press_again_during_tail_continues_same_turn(hub):
    s = RecSession()
    c = make(hub, s)

    async def script():
        c.press()
        await asyncio.sleep(0.45)
        c.release()
        await asyncio.sleep(0.1)                     # still in the 0.3 s tail
        c.press()
        await asyncio.sleep(0.4)
        c.release()
        await asyncio.sleep(0.45)

    run_mic(c, script)
    assert s.log.count("start") == 1 and s.log.count("end") == 1 and s.log[-1] == "end"


def test_level_meter_and_quiet_mic_warning(hub):
    from assistant.voice import level_bar, peak_level
    assert peak_level(array.array("h", [0, 16384, -32768]).tobytes()) == 1.0
    assert "good" in level_bar(0.3) and "too quiet" in level_bar(0.01) and level_bar(0) == "(no audio)"
    logs = []
    c = Conversation(hub, VOICE_DEFAULTS, RecSession(), FakeAudio(sample=300), log=lambda *a, **k: logs.append(a[0]))

    async def script():
        c.press()
        await asyncio.sleep(0.4)
        c.release()

    run_mic(c, script)
    assert any("mic" in l and "too quiet" in l for l in logs) and any("⚠ mic very quiet" in l for l in logs)



# ---------------- key poller (Windows physical-key polling) ----------------

def poller(keys: set, hotkey="ctrl+alt"):
    out = []
    kp = KeyPoller(hotkey, out.append, pressed=lambda p: p in keys)
    kp.vk_down = lambda vk: vk in keys.get_vks() if hasattr(keys, "get_vks") else False
    return kp, out


def run(kp, n=3):
    for _ in range(n):
        w = kp.step()
        if w:
            kp.emit(w)


def test_poller_press_release_needs_two_stable_reads():
    keys = set()
    kp, out = poller(keys)
    keys |= {"ctrl", "alt"}
    assert kp.step() is None                       # first read: not trusted yet (debounce)
    run(kp)
    assert out == ["press"]
    keys.clear()
    run(kp)
    assert out == ["press", "release"]
    run(kp, 5)
    assert out == ["press", "release"]             # nothing repeats while idle


def test_poller_only_ctrl_is_not_a_press():
    keys = {"ctrl"}
    kp, out = poller(keys)
    run(kp, 5)
    assert out == []


def test_poller_third_key_cancels_and_waits_for_full_release():
    class Keys(set):
        vks = set()
        def get_vks(self):
            return self.vks
    keys = Keys({"ctrl", "alt"})
    kp, out = poller(keys)
    run(kp)
    assert out == ["press"]
    keys.vks = {0x2E}                               # Delete -> Ctrl+Alt+Del
    run(kp)
    assert out == ["press", "cancel"]
    keys.vks = set()
    run(kp)
    assert out == ["press", "cancel"]              # still holding ctrl+alt: stays blocked
    keys.clear()
    run(kp)
    keys |= {"ctrl", "alt"}
    run(kp)
    assert out == ["press", "cancel", "press"]     # works again after letting go


def test_poller_bounce_is_ignored():
    keys = set()
    kp, out = poller(keys)
    keys |= {"ctrl", "alt"}
    kp.step()
    keys.clear()                                   # 10 ms glitch
    run(kp, 4)
    assert out == []


def test_latency_logged_and_slow_warning(hub):
    logs = []
    t = [100.0]
    c = Conversation(hub, VOICE_DEFAULTS, RecSession(), FakeAudio(), clock=lambda: t[0],
                     log=lambda *a, **k: logs.append(a[0] if a else ""))
    c.press()
    t[0] += 1
    c.release()
    t[0] += 13

    async def go():
        wd = asyncio.create_task(c.watchdog())
        await asyncio.sleep(0.35)
        await c.handle(call("lookup", query="x"))
        await c.handle(spoke(b"a", done=False))
        c.done.set()
        await wd

    asyncio.run(go())
    assert any("⏳ Gemini is slow" in str(l) for l in logs)
    rec = hub.latency.read()[-1]
    assert rec["s"] == 13.0 and rec["tools"] == ["lookup"]


def test_off_the_record_phrases():
    from assistant.voice import OFF_RECORD
    for s in ["Esto es off the record", "Paquito, no lo anotes", "esto no lo apuntes porfa", "fuera de registro"]:
        assert OFF_RECORD.search(s), s
    for s in ["Anota esto en mis tareas", "pon música", "no guardes la canción en favoritos"]:
        assert not OFF_RECORD.search(s), s


def _convos(hub):
    import json
    p = hub.dir / "logs" / "conversations.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def test_cut_off_conversation_still_logs_last_turn(hub):
    # he answers, then the conversation dies before Gemini sends turn_complete
    s = FakeSession([heard("Gracias por mi familia,"), heard(" mis amigos y mis estudios.")])
    c = make(hub, s, idle_timeout_sec=0.3)
    asyncio.run(asyncio.wait_for(c.run(), timeout=3))
    assert _convos(hub)[-1]["said"] == ["Gracias por mi familia, mis amigos y mis estudios."]


def test_off_the_record_conversation_keeps_only_a_marker(hub):
    s = FakeSession([heard("Esto es off the record, me siento mal"), spoke()])
    c = make(hub, s, idle_timeout_sec=0.3)
    asyncio.run(asyncio.wait_for(c.run(), timeout=3))
    last = _convos(hub)[-1]
    assert last["private"] is True and last["said"] == []
