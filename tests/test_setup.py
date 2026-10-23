import json
import os

from assistant import setup_wizard as sw
from assistant.claude_bridge import ClaudeBridge
from assistant.config import DEFAULTS, gemini_key, load_config, user_line, user_name


def test_defaults_are_not_tied_to_one_person():
    blob = json.dumps(DEFAULTS)
    assert "jdsn2" not in blob and "Jose" not in blob and "asu.edu" not in blob
    assert DEFAULTS["vault_path"].startswith("~")


def test_vault_path_expands_home(tmp_path, monkeypatch):
    monkeypatch.delenv("PAKITO_VAULT", raising=False)
    local = tmp_path / "c.json"
    local.write_text("{}")
    cfg = load_config(local)
    assert "~" not in cfg["vault_path"] and cfg["vault_path"].endswith("Pakito")


def test_user_line_and_name():
    assert user_name({}) == "the user"
    assert user_line({"user": {"name": "Ana", "about": "I study biology."}}) == "Ana (I study biology)"
    assert user_line({"user": {"name": "Ana", "about": ""}}) == "Ana"


def test_gemini_key_from_config_is_exported(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert gemini_key({}) is None
    assert gemini_key({"gemini_api_key": "abc123456789"}) == "abc123456789"
    assert os.environ["GEMINI_API_KEY"] == "abc123456789"
    monkeypatch.setenv("GEMINI_API_KEY", "fromenv")
    assert gemini_key({"gemini_api_key": "other"}) == "fromenv"


def test_claude_prompt_uses_the_user(tmp_path):
    b = ClaudeBridge({}, tmp_path, lambda *a: None, who="Ana", about="She studies biology.")
    assert "question Ana asked" in b.system and "She studies biology." in b.system
    assert "Jose" not in ClaudeBridge({}, tmp_path, lambda *a: None).system


def test_merge_and_save_keep_secrets(tmp_path):
    p = tmp_path / "config.local.json"
    p.write_text(json.dumps({"spotify": {"client_id": "SECRET"}, "voice": {"model": "m"}}))
    sw.save_local({"voice": {"mic": "Blue Yeti"}, "user": {"name": "Ana"}}, p)
    cfg = json.loads(p.read_text())
    assert cfg["spotify"]["client_id"] == "SECRET" and cfg["voice"] == {"model": "m", "mic": "Blue Yeti"}
    sw.save_local({"voice": {"mic": None}}, p)
    assert "mic" not in json.loads(p.read_text())["voice"]


def test_installed_browsers_dedupes_and_flags_firefox():
    env = {"LOCALAPPDATA": r"C:\L", "ProgramFiles": r"C:\P", "ProgramFiles(x86)": r"C:\X"}
    have = {os.path.join(r"C:\P", r"Google\Chrome\Application\chrome.exe"),
            os.path.join(r"C:\X", r"Google\Chrome\Application\chrome.exe"),
            os.path.join(r"C:\P", r"Mozilla Firefox\firefox.exe")}
    found = sw.installed_browsers(env, exists=lambda p: p in have)
    assert [b["name"] for b in found] == ["Chrome", "Firefox"]
    assert found[0]["chromium"] and not found[1]["chromium"]


def test_obsidian_vaults(tmp_path):
    v = tmp_path / "MyVault"
    v.mkdir()
    (tmp_path / "obsidian").mkdir()
    (tmp_path / "obsidian" / "obsidian.json").write_text(json.dumps(
        {"vaults": {"a": {"path": str(v)}, "b": {"path": str(tmp_path / "gone")}}}))
    assert sw.obsidian_vaults(str(tmp_path)) == [str(v)]
    assert sw.obsidian_vaults(str(tmp_path / "nope")) == []


def test_mask_never_shows_the_key():
    assert sw.mask("AIzaSyVERYSECRETKEY1234") == "…1234" and sw.mask(None) == "not set"


def test_wizard_you_and_gemini_steps(tmp_path):
    p = tmp_path / "config.local.json"
    answers = iter(["Ana", "She studies biology.", "Lima, Peru",      # you
                    "bad-key", "good-key"])                            # gemini: retry after a bad key
    said = []
    w = sw.Wizard(ask=lambda q: next(answers), say=said.append, local=p,
                  check_key=lambda k: None if k == "good-key" else "not valid")
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ.pop("GOOGLE_API_KEY", None)
    sw.webbrowser.open = lambda url: None
    w.find_model = lambda key: "gemini-3.1-flash-live-preview" if key == "good-key" else None
    w.you()
    w.gemini()
    cfg = json.loads(p.read_text())
    assert cfg["voice"]["model"] == "gemini-3.1-flash-live-preview"     # picked a model this key can use
    assert cfg["user"] == {"name": "Ana", "about": "She studies biology."}
    assert cfg["voice"]["location"] == "Lima, Peru" and cfg["gemini_api_key"] == "good-key"
    assert any("didn't work" in s for s in said)
    assert not any("good-key" in s for s in said)                     # never echoes the key


def test_wizard_enter_keeps_current_values(tmp_path):
    p = tmp_path / "config.local.json"
    p.write_text(json.dumps({"user": {"name": "Ana", "about": "x"}, "voice": {"location": "Lima"}}))
    w = sw.Wizard(ask=lambda q: "", say=lambda s: None, local=p)
    w.you()
    cfg = json.loads(p.read_text())
    assert cfg["user"]["name"] == "Ana" and cfg["voice"]["location"] == "Lima"


def test_wizard_notes_picks_plain_folder(tmp_path, monkeypatch):
    p = tmp_path / "config.local.json"
    monkeypatch.setattr(sw.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sw, "obsidian_vaults", lambda: [])
    w = sw.Wizard(ask=lambda q: "", say=lambda s: None, local=p)
    w.notes()
    assert json.loads(p.read_text())["vault_path"] == str(tmp_path / "Documents" / "Pakito")
    assert (tmp_path / "Documents" / "Pakito").is_dir()


def test_joining_a_folder_another_computer_uses(tmp_path, monkeypatch):
    vault = tmp_path / "Second Brain"
    (vault / "Assistant" / "logs").mkdir(parents=True)
    (vault / "Assistant" / "logs" / "events.jsonl").write_text("{}\n")
    (vault / "Assistant" / "state" / "devices").mkdir(parents=True)
    (vault / "Assistant" / "state" / "devices" / "joweedpc.json").write_text("{}")
    p = tmp_path / "config.local.json"
    p.write_text(json.dumps({"device_placeholder": 1}))
    said = []
    w = sw.Wizard(ask=lambda q: (said.append(q), "")[1], say=said.append, local=p)   # Enter = the default (join)
    monkeypatch.setattr("assistant.config.device_name", lambda cfg: "josesmac")
    w.device(vault)
    cfg = json.loads(p.read_text())
    assert cfg["device"] == {"name": "josesmac", "primary": False}
    assert any("joweedpc" in s for s in said)


def test_same_computer_rerunning_setup_stays_primary(tmp_path):
    vault = tmp_path / "v"
    (vault / "Assistant" / "logs").mkdir(parents=True)
    (vault / "Assistant" / "logs" / "events.jsonl").write_text("{}\n")
    p = tmp_path / "config.local.json"
    w = sw.Wizard(ask=lambda q: "", say=lambda s: None, local=p)         # no other heartbeats: default = mine
    w.device(vault)
    assert "device" not in json.loads(p.read_text() if p.exists() else "{}")


def test_pasted_paths_are_cleaned():
    from assistant.config import clean_path
    mac = "/Users/pedrosanchez/Library/CloudStorage/OneDrive-Personal/Second Brain"
    assert clean_path(f"'{mac}'") == mac                       # Oct 22: the quotes became part of the path
    assert clean_path(f' "{mac}" ') == mac
    if clean_path("a\\ b") == "a b":                             # backslash escapes only on Mac/Linux
        assert clean_path(mac.replace(" ", "\\ ")) == mac
    assert clean_path(r"C:\Users\jdsn2\OneDrive\Second Brain") == r"C:\Users\jdsn2\OneDrive\Second Brain"
