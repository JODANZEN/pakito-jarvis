"""Nov 24: the instructions themselves get translated when he sets a base language, always from the master copy."""
import copy
import json

import pytest

from assistant import prompt_lang as pl
from assistant.voice import PROMPT_TEMPLATE


@pytest.fixture(autouse=True)
def tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "DIR", tmp_path / "prompts")


def fake_translation(text):
    """Pretend-Bulgarian: every line marked, slots and tool names kept."""
    return "\n".join(("[bg] " + line) if line.strip() else line for line in text.splitlines())


def test_the_master_copy_has_the_slots_it_needs():
    assert set(pl.fields(PROMPT_TEMPLATE)) == {"user", "now", "where", "language", "search_how", "rlines", "level",
                                               "phase", "recent_txt", "claude_txt", "coming_txt", "lang_end"}
    assert "ask_claude" in pl.tool_names(PROMPT_TEMPLATE)


def test_good_translation_is_saved_with_its_original_and_used():
    asked = []

    def gen(model, prompt):
        asked.append(model)
        return fake_translation(prompt.split("---\n", 1)[1])
    r = pl.translate("Bulgarian", PROMPT_TEMPLATE, gen)
    assert r["translated"] == "Bulgarian" and asked == [pl.TEXT_MODELS[0]]
    saved = json.loads((pl.DIR / "Bulgarian.json").read_text(encoding="utf-8"))
    assert saved["source"] == PROMPT_TEMPLATE and saved["source_hash"] == pl.source_hash(PROMPT_TEMPLATE)
    vals = {f: f"<{f}>" for f in pl.fields(PROMPT_TEMPLATE)}
    out = pl.fill(PROMPT_TEMPLATE, "Bulgarian", vals)
    assert out.startswith("[bg] You are Pakito") and "<user>" in out and "<recent_txt>" in out


def test_bad_translations_are_refused_and_the_next_model_tried():
    def gen(model, prompt):
        text = prompt.split("---\n", 1)[1]
        if model == pl.TEXT_MODELS[0]:
            return text.replace("{rlines}", "{rutinas}")             # renamed a slot
        if model == pl.TEXT_MODELS[1]:
            return text.replace("ask_claude", "preguntar_a_claude")  # translated a tool name
        return fake_translation(text)
    r = pl.translate("Spanish", PROMPT_TEMPLATE, gen)
    assert r["model"] == pl.TEXT_MODELS[2]
    assert pl.check(PROMPT_TEMPLATE, PROMPT_TEMPLATE.replace("{now}", "{ahora}"))
    assert pl.check(PROMPT_TEMPLATE, PROMPT_TEMPLATE[:500])                # too short
    assert pl.check(PROMPT_TEMPLATE, PROMPT_TEMPLATE + " { ")              # stray brace


def test_nothing_usable_falls_back_to_the_master_copy():
    r = pl.translate("Bulgarian", PROMPT_TEMPLATE, lambda m, p: "short")
    assert "error" in r
    vals = {f: "x" for f in pl.fields(PROMPT_TEMPLATE)}
    assert pl.fill(PROMPT_TEMPLATE, "Bulgarian", vals).startswith("You are Pakito")


def test_a_changed_original_makes_the_old_translation_stale():
    pl.translate("Bulgarian", PROMPT_TEMPLATE, lambda m, p: fake_translation(p.split("---\n", 1)[1]))
    assert pl.cached("Bulgarian", PROMPT_TEMPLATE)
    assert pl.cached("Bulgarian", PROMPT_TEMPLATE + "\n- a new rule") is None   # redone from the new original


def test_english_never_translates():
    called = []
    pl.prepare("English", PROMPT_TEMPLATE, generate=lambda m, p: called.append(1) or "")
    assert called == []


def test_system_prompt_uses_the_translation(tmp_path):
    from assistant.config import DEFAULTS
    from assistant.hub import Hub
    from assistant.voice import VOICE_DEFAULTS, system_prompt
    pl.translate("Bulgarian", PROMPT_TEMPLATE, lambda m, p: fake_translation(p.split("---\n", 1)[1]))
    cfg = copy.deepcopy(DEFAULTS)
    cfg["vault_path"] = str(tmp_path)
    cfg["user"] = {"name": "Jose", "language": "Bulgarian"}
    hub = Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)
    p = system_prompt(hub, dict(VOICE_DEFAULTS))
    assert p.startswith("[bg] You are Pakito") and "Jose" in p and "always speak Bulgarian" in p
    assert p.rstrip().endswith("Remember: speak Bulgarian.")
