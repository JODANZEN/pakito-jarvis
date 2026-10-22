"""Two computers (PC + Mac) sharing one folder, like OneDrive: shared memory, no clobbering, one in charge."""
import copy
import json

import pytest

from assistant.config import DEFAULTS, device_name, device_path
from assistant.devices import STALE_SEC
from assistant.hub import Hub


class Clock:
    def __init__(self):
        self.t = 1_790_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def pair(tmp_path):
    clock = Clock()

    def make(name, primary):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["vault_path"] = str(tmp_path)
        cfg["device"] = {"name": name, "primary": primary}
        return Hub(cfg, launcher=lambda x: None, notifier=lambda t, m: fired.append((name, m)), clock=clock)
    fired = []
    pc, mac = make("joweedpc", True), make("Jose's MacBook", False)
    return pc, mac, clock, fired


def test_names_and_paths(tmp_path):
    assert device_name({"device": {"name": "Jose's MacBook.local"}}) == "jose-s-macbook"
    p = tmp_path / "logs" / "events.jsonl"
    assert device_path({"device": {"primary": True}}, p) == p                 # one computer: nothing changes
    assert device_path({"device": {"name": "mac", "primary": False}}, p).name == "events.mac.jsonl"


def test_each_writes_its_own_log_and_both_read_all(pair):
    pc, mac, clock, _ = pair
    pc.call("open_url", {"url": "canvas.asu.edu"}, "voice")
    clock.t += 60
    mac.call("open_url", {"url": "chatgpt.com"}, "voice")
    logs = pc.dir / "logs"
    assert (logs / "events.jsonl").exists() and (logs / "events.jose-s-macbook.jsonl").exists()
    for hub in (pc, mac):
        urls = [e["args"]["url"] for e in hub.events.read() if e["tool"] == "open_url"]
        assert urls == ["canvas.asu.edu", "chatgpt.com"]                     # merged, in time order
    assert any("chatgpt" in l for l in pc.recent())                          # the PC knows what you did on the Mac


def test_shared_tasks_and_notes(pair):
    pc, mac, _, _ = pair
    mac.call("add_task", {"task": "comprar café"}, "voice")
    assert "comprar café" in pc.call("tasks")["open"]
    mac.call("take_note", {"text": "idea desde el mac", "title": "mac"}, "typed")
    assert (pc.dir.parent / "inbox").exists()


def test_reminders_merge_instead_of_overwriting(pair):
    pc, mac, clock, _ = pair
    pc.call("remember", {"what": "llamar a mamá", "due": "2026-09-30T09:00"}, "voice")
    mac.call("remember", {"what": "pagar renta", "due": "2026-09-30T10:00"}, "voice")   # Mac had loaded before
    pc.call("remember", {"what": "sacar la basura", "due": "2026-09-30T11:00"}, "voice")
    whats = sorted(r["what"] for r in json.loads((pc.dir / "state" / "reminders.json").read_text()))
    assert whats == ["llamar a mamá", "pagar renta", "sacar la basura"]          # nobody's got lost
    assert len(mac.call("reminders")["reminders"]) == 3


def test_only_the_computer_you_used_last_fires_reminders(pair):
    pc, mac, clock, fired = pair
    pc.call("remember", {"what": "estirar", "due": "2026-09-22T07:00"}, "voice")
    pc.presence.beat(used=True)
    clock.t += 60
    mac.presence.beat(used=True)                                      # now you're on the Mac
    pc.presence.beat()
    clock.t = pc.reminders.items[0]["at"] + 5
    pc.presence.beat()
    mac.presence.beat()
    assert pc.reminders.check() == []                                 # PC stays quiet
    assert [r["what"] for r in mac.reminders.check()] == ["estirar"]  # the Mac fires it
    assert pc.reminders.check() == [] and mac.reminders.check() == []  # once, ever (fired flag is shared)
    assert [n for n, _ in fired if n in ("joweedpc", "Jose's MacBook")] == ["Jose's MacBook"]


def test_sleeping_mac_hands_back_to_the_pc(pair):
    pc, mac, clock, fired = pair
    mac.presence.beat(used=True)
    pc.presence.beat()
    assert not pc.presence.in_charge() and pc.presence.who() == "jose-s-macbook"
    clock.t += STALE_SEC + 1                                          # the Mac went to sleep: no more heartbeats
    pc.presence.beat()
    assert pc.presence.in_charge()
    mac.presence.beat()                                               # it wakes up but you're not using it
    pc.presence.beat(used=True)                                       # you talk to the PC
    assert pc.presence.in_charge() and not mac.presence.in_charge()


def test_restart_remembers_when_this_computer_was_used(pair, tmp_path):
    pc, mac, clock, _ = pair
    pc.presence.beat(used=True)
    clock.t += 10
    again = Hub(copy.deepcopy(pc.cfg), launcher=lambda x: None, notifier=lambda *a: None, clock=clock)
    assert again.presence.used == pc.presence.used


def test_machine_only_files_stay_separate(pair):
    pc, mac, _, _ = pair
    assert pc.pet.path.name == "pet.json" and mac.pet.path.name == "pet.jose-s-macbook.json"
    assert pc.claude.ledger.name == "jobs.json" and mac.claude.ledger.name == "jobs.jose-s-macbook.json"
    assert pc.calendar.cache.name == "calendar_cache.json" and mac.calendar.cache.name != pc.calendar.cache.name


def test_focus_level_is_shared_not_clobbered(pair):
    pc, mac, clock, _ = pair
    pc.call("start_focus", {}, "voice")
    clock.t += 21 * 60                                                # PC block done -> level 22
    pc.focus.status()
    assert pc.focus.level == 22
    mac.call("start_focus", {}, "voice")                              # Mac loaded level 20 before
    clock.t += 21 * 60
    mac.focus.status()
    assert mac.focus.level == 24                                      # built on the PC's 22, not its stale 20
