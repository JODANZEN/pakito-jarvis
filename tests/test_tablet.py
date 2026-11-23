"""Nov 23: no Miracast on his PC, so the tablet's screen comes over adb (USB-C cable) on demand."""
import copy
import io
import subprocess

import pytest
from PIL import Image

from assistant import tablet as tab
from assistant.config import DEFAULTS
from assistant.hub import Hub
from assistant.tablet import Tablet


def png(w=1200, h=1800):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def runner(devices="R5CX123\tdevice\n", shot=None, code=0):
    def run(cmd, **kw):
        if "devices" in cmd:
            return subprocess.CompletedProcess(cmd, 0, f"List of devices attached\n{devices}\n", "")
        return subprocess.CompletedProcess(cmd, code, shot if shot is not None else png(), b"")
    return run


def t(**kw):
    return Tablet({}, which=lambda x: "adb", **kw)


def test_no_adb_says_how_to_install():
    tb = Tablet({}, which=lambda x: None, exists=lambda p: False)
    assert "winget install Google.PlatformTools" in tb.status()["error"]
    assert "winget" in tb.screenshot()["error"]


def test_each_missing_setup_step_is_named():
    assert "Plug in" in t(run=runner(devices="")).status()["error"]
    assert "tap Allow" in t(run=runner(devices="R5CX123\tunauthorized\n")).status()["error"]
    assert t(run=runner()).status() == {"ok": True, "devices": ["R5CX123"]}


def test_screenshot_and_bad_output():
    assert t(run=runner()).screenshot()["png"].startswith(tab.PNG)
    r = t(run=runner(devices="R5CX123\tunauthorized\n", shot=b"error: unauthorized", code=1)).screenshot()
    assert "tap Allow" in r["error"]


def test_prepare_shrinks_for_gemini():
    pic = tab.prepare(png(2800, 1752))
    assert pic["size"] == (2800, 1752)
    assert max(Image.open(io.BytesIO(pic["jpeg"])).size) <= 1600


@pytest.fixture
def hub(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    h = Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)
    h.tablet = t(run=runner())
    return h


def test_pakito_looks_at_the_tablet_as_a_tutor(hub):
    r = hub.call("look_at_tablet", {}, "voice")
    assert r["looking_at"] == "his tablet" and "hint" in r["note"] and hub.pending_image


def test_for_chatgpt_it_is_saved_and_copied_not_sent(hub, monkeypatch, tmp_path):
    copied = []
    monkeypatch.setattr(tab, "clipboard_image", lambda img: copied.append(img.size) or True)
    hub.cfg["tablet"] = {"folder": str(tmp_path / "shots")}
    r = hub.call("look_at_tablet", {"send_to": "clipboard"}, "voice")
    assert r["copied"] and copied == [(1200, 1800)] and hub.pending_image is None
    assert (tmp_path / "shots").is_dir() and r["saved"].endswith(".png")


def test_paste_into_the_window_in_front(hub, monkeypatch, tmp_path):
    keys = []
    monkeypatch.setattr(tab, "clipboard_image", lambda img: True)
    monkeypatch.setattr(hub, "_paste_keys", lambda: keys.append("ctrl+v"))
    monkeypatch.setattr("assistant.hub.time.sleep", lambda s: None)
    hub.cfg["tablet"] = {"folder": str(tmp_path)}
    r = hub.call("look_at_tablet", {"send_to": "clipboard", "paste": True}, "voice")
    assert keys == ["ctrl+v"] and "Enter" in r["note"]


def test_tablet_problems_are_reported(hub):
    hub.tablet = t(run=runner(devices="", shot=b"error: no devices/emulators found", code=1))
    r = hub.call("look_at_tablet", {}, "voice")
    assert "Plug in" in r["error"] and hub.pending_image is None


def test_agents_cant_look_at_the_tablet():
    from assistant.server import AGENT_HIDDEN
    assert "look_at_tablet" in AGENT_HIDDEN
