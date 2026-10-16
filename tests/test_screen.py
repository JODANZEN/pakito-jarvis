import asyncio
import copy
import io
import json

import pytest
from PIL import Image

from assistant import claude_bridge, screen
from assistant.config import DEFAULTS
from assistant.hub import Hub
from test_voice import Conversation, FakeAudio, FakeSession, VOICE_DEFAULTS, call, heard


def fake_img(w=3000, h=2000):
    return Image.new("RGB", (w, h), (30, 60, 90))


@pytest.fixture
def hub(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    h = Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)
    grabbed = []
    h.screen.fg = lambda: ("PHY 310 - Taylor ch 7.pdf - Opera", "opera.exe", (0, 0, 1200, 800))
    h.screen.grabber = lambda box: grabbed.append(box) or fake_img()
    h.screen.selector = lambda: (100, 100, 500, 300)
    h.screen.sleep = lambda s: None
    h.grabbed = grabbed
    return h


def test_screenshot_is_downsized_jpeg_of_the_front_window(hub):
    r = hub.call("look_at_screen", {})
    assert r["looking_at"].startswith("PHY 310") and hub.grabbed == [(0, 0, 1200, 800)]
    img = Image.open(io.BytesIO(hub.pending_image))
    assert img.format == "JPEG" and max(img.size) == screen.MAX_SIDE
    ev = hub.events.read()[-1]
    assert ev["tool"] == "look_at_screen" and "jpeg" not in json.dumps(ev)      # the image is never logged


def test_region_and_cancel(hub):
    hub.call("look_at_screen", {"area": "region"})
    assert hub.grabbed[-1] == (100, 100, 500, 300)
    hub.screen.selector = lambda: None
    assert "Cancelled" in hub.call("look_at_screen", {"area": "region"})["error"]


def test_refuses_on_calls_and_password_windows(hub):
    hub.pet.on_call = True
    assert "call" in hub.call("look_at_screen", {})["error"] and not hub.grabbed
    hub.pet.on_call = False
    hub.screen.fg = lambda: ("Bitwarden", "Bitwarden.exe", (0, 0, 800, 600))
    assert "password" in hub.call("look_at_screen", {})["error"] and not hub.grabbed
    assert hub.pending_image is None


def test_pet_gets_out_of_the_way(hub):
    hub.pet.set_mode("stay")
    seen = []
    hub.screen.grabber = lambda box: seen.append(hub.pet_snapshot()) or fake_img()
    hub.call("look_at_screen", {})
    assert seen[0]["show"] is False and seen[0]["hide_now"] is True


def test_voice_sends_the_image_before_the_tool_result(hub):
    order = []

    class S(FakeSession):
        async def send_realtime_input(self, audio=None, video=None, **k):
            if video is not None:
                order.append(("image", video.mime_type, len(video.data)))

        async def send_tool_response(self, function_responses):
            order.append(("tool", function_responses[0].name))

    c = Conversation(hub, VOICE_DEFAULTS, S(), FakeAudio(), log=lambda *a, **k: None)

    async def go():
        await c.handle(heard("mira esto, ¿qué es esta ecuación?"))
        await c.handle(call("look_at_screen"))
    asyncio.run(go())
    assert order[0][:2] == ("image", "image/jpeg") and order[1] == ("tool", "look_at_screen")
    assert hub.pending_image is None                                          # dropped after sending


def test_claude_gets_a_temporary_copy_that_is_deleted(hub, monkeypatch):
    monkeypatch.setattr(claude_bridge, "find_claude", lambda p=None: "claude.exe")
    seen = {}

    def runner(cmd, cwd, timeout):
        prompt = cmd[cmd.index("-p") + 1]
        name = prompt.split("the file ")[1].split()[0]
        seen["exists_during"] = (cwd / name).exists()
        seen["allowed"] = cmd[cmd.index("--allowedTools") + 1]
        return 0, json.dumps({"result": "It's the Lagrangian for a pendulum.\n---\nSteps...", "session_id": "s"}), ""

    hub.claude.runner, hub.claude.threaded = runner, False
    r = hub.call("look_at_screen", {"question": "explain this step by step", "claude": True})
    assert r["claude"] and seen["exists_during"] and "Read" in seen["allowed"]
    assert not list(hub.claude.work.glob("screen-*.jpg"))                     # gone after Claude finished


def test_agents_cant_take_screenshots():
    from assistant import server
    assert "look_at_screen" in server.AGENT_HIDDEN


def test_sensitive_titles():
    assert screen.is_sensitive("Vault - 1Password", "1Password.exe")
    assert screen.is_sensitive("Windows Security", "CredentialUIBroker.exe")
    assert not screen.is_sensitive("Taylor - Classical Mechanics.pdf", "opera.exe")
