"""Mac pieces, simulated on any OS (no Mac needed): commands are captured, not run."""
import json
import sys
from types import SimpleNamespace as NS

import pytest

from assistant import macos, util
from assistant.voice import HotkeyTracker


@pytest.fixture
def popen(monkeypatch):
    calls = []
    monkeypatch.setattr(macos.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(util.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    return calls


def test_launch_uses_open(popen, monkeypatch):
    monkeypatch.setattr(util.sys, "platform", "darwin")
    util.launch("https://canvas.asu.edu")
    util.launch("spotify:search:lofi")
    util.launch("code")
    assert popen == [["open", "https://canvas.asu.edu"], ["open", "spotify:search:lofi"], "code"]


def test_browser_launcher_opens_with_app(popen, monkeypatch):
    monkeypatch.setattr(util.sys, "platform", "darwin")
    go = util.make_launcher("/Applications/Opera GX.app")
    go("https://x.com")
    assert popen == [["open", "-a", "/Applications/Opera GX.app", "https://x.com"]]


def test_app_list_and_open(tmp_path, popen, monkeypatch):
    (tmp_path / "Discord.app").mkdir()
    (tmp_path / "Obsidian.app").mkdir()
    monkeypatch.setattr(macos, "APP_DIRS", [tmp_path])
    apps = macos.apps()
    assert [a["Name"] for a in apps] == ["Discord", "Obsidian"]
    monkeypatch.setattr(util.sys, "platform", "darwin")
    sa = util.StartApps(loader=macos.apps)
    assert sa.open("discord") == "Discord"
    assert popen[-1] == ["open", "-a", str(tmp_path / "Discord.app")]


def test_notify_and_paste_go_through_osascript(monkeypatch):
    scripts, clip = [], {"v": "old"}
    monkeypatch.setattr(macos, "_osa", lambda s, timeout=5: scripts.append(s) or "")
    monkeypatch.setattr(macos, "clipboard_get", lambda: clip["v"])
    monkeypatch.setattr(macos, "clipboard_set", lambda t: clip.update(v=t) or True)
    monkeypatch.setattr(macos.time, "sleep", lambda s: None)
    monkeypatch.setattr(macos.threading, "Thread", lambda target, args, daemon: NS(start=lambda: target(*args)))
    macos.notify('Nota "guardada"', "listo")
    assert 'display notification "listo" with title "Nota \\"guardada\\""' in scripts[0]
    assert macos.paste("hola mundo")
    assert "keystroke \"v\" using command down" in scripts[1] and clip["v"] == "old"   # old clipboard back


def test_hotkeys_ctrl_option_and_ctrl_cmd(monkeypatch):
    names = {"ctrl_l": "left ctrl", "alt_l": "left alt", "cmd": "windows", "a": "a"}
    monkeypatch.setattr(macos, "key_name", lambda k: names.get(k))
    talk, dic = [], []
    lst = macos.HotkeyListener([(HotkeyTracker("ctrl+alt"), talk.append),
                                (HotkeyTracker("ctrl+windows"), dic.append)])
    for k, down in [("ctrl_l", True), ("alt_l", True), ("alt_l", False), ("ctrl_l", False)]:
        lst._feed(k, down)
    assert talk == ["press", "release"] and dic == []
    for k, down in [("ctrl_l", True), ("cmd", True), ("cmd", False), ("ctrl_l", False)]:
        lst._feed(k, down)
    assert dic == ["press", "release"]
    for k, down in [("ctrl_l", True), ("alt_l", True), ("a", True)]:         # Ctrl+Option+A is a shortcut
        lst._feed(k, down)
    assert talk[-1] == "cancel"


def test_terminal_command_is_quoted(monkeypatch):
    got = []
    monkeypatch.setattr(macos, "_osa", lambda s, timeout=5: got.append(s) or "")
    macos.open_terminal(["/usr/local/bin/claude", "--continue"], "/Users/jose/my repo")
    assert "cd '/Users/jose/my repo' && /usr/local/bin/claude --continue" in got[0]


def test_setup_finds_the_pcs_memory_in_onedrive(tmp_path):
    from assistant.setup_wizard import shared_memory
    sb = tmp_path / "Library" / "CloudStorage" / "OneDrive-Personal" / "Second Brain"
    (sb / "Assistant").mkdir(parents=True)
    (sb / "Assistant" / "Routines.md").write_text("# Routines")
    (tmp_path / "Library" / "CloudStorage" / "OneDrive-Personal" / "Other").mkdir()
    assert shared_memory(tmp_path) == [str(sb)]


def test_mic_list_isnt_filtered_to_windows_mme_on_mac(monkeypatch):
    from assistant import voice
    devs = [{"name": "MacBook Air Microphone", "maxInputChannels": 1, "hostApi": 0, "index": 0},
            {"name": "AirPods", "maxInputChannels": 1, "hostApi": 0, "index": 1}]
    pa = NS(get_device_count=lambda: 2, get_device_info_by_index=lambda i: devs[i],
            get_host_api_info_by_index=lambda i: {"name": "Core Audio"})
    monkeypatch.setattr(voice.sys, "platform", "darwin")
    assert [d["name"] for d in voice.list_mics(pa)] == ["MacBook Air Microphone", "AirPods"]


def test_wizard_on_a_mac_offers_the_shared_memory_and_mac_browsers(tmp_path, monkeypatch):
    from assistant import setup_wizard as sw
    sb = tmp_path / "Library" / "CloudStorage" / "OneDrive-Personal" / "Second Brain"
    (sb / "Assistant" / "logs").mkdir(parents=True)
    (sb / "Assistant" / "Routines.md").write_text("# Routines")
    (sb / "Assistant" / "logs" / "events.jsonl").write_text("{}\n")
    (sb / "Assistant" / "state" / "devices").mkdir(parents=True)
    (sb / "Assistant" / "state" / "devices" / "joweedpc.json").write_text("{}")
    monkeypatch.setattr(sw.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sw, "obsidian_vaults", lambda: [])
    monkeypatch.setattr(sw.sys, "platform", "darwin")
    monkeypatch.setattr(macos, "browsers", lambda: [{"name": "Opera GX", "path": "/Applications/Opera GX.app",
                                                     "chromium": True}])
    monkeypatch.setattr("assistant.config.device_name", lambda cfg: "josesmac")
    p = tmp_path / "config.local.json"
    said = []
    w = sw.Wizard(ask=lambda q: (said.append(q), "")[1], say=said.append, local=p)
    w.notes()                                      # Enter = default = the shared folder, then join it
    w.browser()
    cfg = json.loads(p.read_text())
    assert cfg["vault_path"] == str(sb) and cfg["device"] == {"name": "josesmac", "primary": False}
    assert cfg["browser"] == "default" and any("System default" in s for s in said)
