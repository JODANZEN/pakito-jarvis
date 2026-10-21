"""The dream's questions reach him in any conversation, and his answers land where the dream reads them."""
import copy
import json

import pytest

from assistant import debrief
from assistant.config import DEFAULTS
from assistant.hub import Hub


class Clock:
    def __init__(self):
        self.t = 1_790_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    clock = Clock()
    hub = Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None, clock=clock)
    qs = [  # the shapes the dream actually writes
        {"id": "a", "q": "¿Ya terminaste el póster?", "why": "x", "answered": "2026-10-17", "answer": "sí"},
        {"id": "b", "q": "¿Qué querías decir con 'la casa'?", "why": "x"},
        {"id": "c", "q": "¿Cómo va Nexo?", "why": "x", "asked_at": "2026-10-18T21:23:12"},
        {"id": "d", "q": "¿Seguiste con el ruso?", "why": "x", "asked_at": "2026-10-17T20:39:47",
         "answered": "2026-10-18"},
    ]
    (hub.dir / "state" / "dream_questions.json").write_text(json.dumps(qs, ensure_ascii=False), encoding="utf-8")
    return hub, clock


def saved(hub):
    return {q["id"]: q for q in json.loads((hub.dir / "state" / "dream_questions.json").read_text(encoding="utf-8"))}


def test_answered_questions_are_never_asked_again(env):
    hub, _ = env
    assert [q["id"] for q in debrief.open_dream_questions(hub)] == ["b", "c"]
    assert [q.split("]")[0] for q in debrief.take_dream_questions(hub)] == ["[b", "[c"]   # debrief skips a and d


def test_one_question_per_conversation_with_a_cooldown(env):
    hub, clock = env
    first = debrief.offer_dream_question(hub)
    assert first["id"] in ("b", "c")
    assert debrief.offer_dream_question(hub) is None          # next conversation right after: no nagging
    clock.t += 5 * 3600
    second = debrief.offer_dream_question(hub)
    assert second and second["id"] != first["id"]              # rotates to the other one


def test_question_shows_up_in_the_voice_prompt(env):
    pytest.importorskip("google.genai")
    from assistant.voice import VOICE_DEFAULTS, system_prompt
    hub, _ = env
    text = system_prompt(hub, VOICE_DEFAULTS)
    assert "nightly dream" in text and "dream_answer" in text
    assert "¿Qué querías decir con 'la casa'?" in text or "¿Cómo va Nexo?" in text


def test_no_question_during_lock_in(env):
    pytest.importorskip("google.genai")
    from assistant.voice import VOICE_DEFAULTS, system_prompt
    hub, _ = env
    hub.call("start_focus", {"lockin": True})
    assert "nightly dream" not in system_prompt(hub, VOICE_DEFAULTS)


def test_answer_is_saved_for_the_dream(env):
    hub, _ = env
    r = hub.call("dream_answer", {"id": "b", "answer": "La casa es la mansión del Second Brain."}, "voice")
    assert r["saved"] == "b" and r["left"] == 1
    q = saved(hub)["b"]
    assert q["pakito_heard"].startswith("La casa") and q["asked_at"] and "answered" not in q   # the dream marks that
    assert [x["id"] for x in debrief.open_dream_questions(hub)] == ["c"]
    assert hub.call("dream_questions")["questions"][0]["id"] == "c"


def test_answer_by_words_or_the_only_one_left(env):
    hub, _ = env
    assert hub.call("dream_answer", {"id": "nexo", "answer": "Va lento"})["saved"] == "c"
    assert hub.call("dream_answer", {"answer": "la mansión"})["saved"] == "b"
    assert "error" in hub.call("dream_answer", {"answer": "otra"})
