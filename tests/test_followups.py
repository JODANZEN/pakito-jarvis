"""Nov 22: he asked Pakito to have Claude plan his finals, Claude got blocked, and it would have been forgotten."""
import copy
import json

import pytest

from assistant import claude_bridge
from assistant.config import DEFAULTS
from assistant.followups import FollowUps, similar
from assistant.hub import Hub


class Clock:
    def __init__(self):
        self.t = 1795400000.0

    def __call__(self):
        return self.t


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_bridge, "find_claude", lambda p=None: "claude")
    monkeypatch.setattr("assistant.setup_wizard.save_local", lambda u: None)
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    h = Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None, clock=Clock())
    h.claude.threaded = False
    return h


def blocked_then_ok(calls):
    def runner(cmd, cwd, timeout):
        calls.append(cmd)
        if len(calls) == 1:
            return 0, json.dumps({"result": "No pude leer el calendario.", "session_id": "s1", "permission_denials": [
                {"tool_name": "mcp__claude_ai_Google_Calendar__list_events", "tool_input": {}}]}), ""
        return 0, json.dumps({"result": "Listo: plan de finales en tu nota de PHY 302.", "session_id": "s1"}), ""
    return runner


def test_add_dedupes_and_caps(tmp_path):
    f = FollowUps(tmp_path / "f.json", Clock())
    a = f.add("pasar los syllabus al calendario")
    b = f.add("Pasar los horarios de los syllabus al calendario", plan="Claude: leer syllabus; Jose: revisar")
    assert "added" in a and b["updated"] == a["added"] and len(f.load()) == 1
    assert f.load()[0]["plan"] == ["Claude: leer syllabus", "Jose: revisar"]
    for w in ("maleta Denver", "cursos AWS", "Ercilia Works páginas", "Bessel videos", "rosario nuevo",
              "llamar abuelo", "lavar ropa", "comprar café", "pagar renta", "arreglar bici"):
        f.add(w)
    assert len(f.open()) == 8                                   # oldest dropped, not deleted
    assert any(i["status"] == "dropped" for i in f.load())


def test_similar():
    assert similar("planear bloques de estudio para finales", "bloques de estudio finales")
    assert not similar("maleta para Denver", "bloques de estudio finales")


def test_blocked_claude_question_becomes_a_follow_up_and_closes_after_allowing(hub):
    calls = []
    hub.claude.runner = blocked_then_ok(calls)
    hub.call("ask_claude", {"question": "Organiza mis finales y pon mis clases en el calendario"}, "voice")
    live = hub.call("followups", {}, "voice")["followups"]
    assert len(live) == 1 and "calendario" in live[0]["what"] and "dale permiso" in live[0]["plan"][0]
    r = hub.call("claude_allow", {}, "voice")
    assert r["allowed"] == ["mcp__claude_ai_Google_Calendar"]
    assert hub.call("followups", {}, "voice")["followups"] == []          # Claude finished it: closed
    assert hub.followups.load()[0]["status"] == "done"


def test_do_hands_it_to_claude_with_the_plan(hub):
    calls = []
    hub.claude.runner = lambda cmd, cwd, t: (calls.append(cmd), (0, json.dumps({"result": "Hecho.", "session_id": "s"}), ""))[1]
    hub.call("followups", {"action": "add", "what": "que Claude me arme bloques de estudio para finales",
                           "plan": "Claude: leer syllabus; Jose: aprobar horario"}, "voice")
    r = hub.call("followups", {"action": "do", "item": "bloques"}, "voice")
    prompt = calls[0][calls[0].index("-p") + 1]
    assert r["asked"] and "bloques de estudio" in prompt and "- Claude: leer syllabus" in prompt
    assert hub.followups.open(hub.claude.jobs) == []


def test_offered_once_every_few_hours_and_not_during_lockin(hub):
    hub.followups.add("pasar los syllabus al calendario")
    assert hub.followups.offer(hub.claude.jobs)["what"].startswith("pasar")
    assert hub.followups.offer(hub.claude.jobs) is None                 # cooldown
    hub.clock.t += 5 * 3600
    assert hub.followups.offer(hub.claude.jobs) is not None


def test_done_and_drop(hub):
    hub.followups.add("pasar los syllabus al calendario")
    hub.followups.add("revisar el proyecto de Ercilia Works")
    assert hub.call("followups", {"action": "done", "item": "syllabus"}, "voice") == {"done": "pasar los syllabus al calendario"}
    assert hub.call("followups", {"action": "drop", "item": "ercilia"}, "voice")["dropped"]
    assert hub.call("followups", {}, "voice")["followups"] == []


def test_the_dreams_items_are_read_fresh(hub, tmp_path):
    p = hub.dir / "state" / "followups.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([{"id": "f-2026-11-22-1", "what": "Plan de estudio para finales", "source": "dream",
                              "status": "open", "said": "necesito como trackear mi estudio",
                              "plan": ["Claude: sacar fechas de los syllabus", "Jose: decir cuántas horas al día"]}]),
                 encoding="utf-8")
    live = hub.call("followups", {}, "voice")["followups"]
    assert live[0]["said"] == "necesito como trackear mi estudio" and len(live[0]["plan"]) == 2


def test_agents_cant_grant_permissions():
    from assistant.server import AGENT_HIDDEN
    assert "claude_allow" in AGENT_HIDDEN
