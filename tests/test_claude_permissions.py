"""Nov 22: Claude couldn't read his notes ('no tengo permiso de lectura') and he had no way to say yes."""
import json

from assistant import claude_bridge
from assistant.claude_bridge import ClaudeBridge, grant_rule, parse_output, rule_path


def bridge(tmp_path, cfg=None, runner=None):
    vault = tmp_path / "Second Brain"
    vault.mkdir()
    b = ClaudeBridge({"allowed_tools": "WebSearch,WebFetch", **(cfg or {})}, tmp_path / "data", lambda *a: None,
                     runner=runner or (lambda *a: (0, "{}", "")), threaded=False, vault=vault)
    return b, vault


def test_windows_and_mac_paths_become_rules():
    assert rule_path(r"C:\Users\jdsn2\OneDrive\Second Brain") == "//c/Users/jdsn2/OneDrive/Second Brain"
    assert rule_path("/Users/pedro/Second Brain/") == "//Users/pedro/Second Brain"


def test_questions_can_read_and_write_only_his_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_bridge, "find_claude", lambda p=None: "claude")
    b, vault = bridge(tmp_path)
    cmd = b.command("plan my finals", None)
    assert cmd[cmd.index("--add-dir") + 1] == str(vault) and "--allowedTools" not in cmd
    perms = json.loads((b.work / "pakito-permissions.json").read_text(encoding="utf-8"))["permissions"]
    v = rule_path(vault)
    assert f"Read({v}/**)" in perms["allow"] and f"Edit({v}/**)" in perms["allow"]       # one rule, space and all
    assert "Read" not in perms["allow"] and "Edit" not in perms["allow"]                   # nothing outside the vault
    assert perms["additionalDirectories"] == [str(vault)] and "Bash" in perms["deny"]
    assert str(vault) in b.system and "CLAUDE.md" in b.system


def test_vault_access_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_bridge, "find_claude", lambda p=None: "claude")
    b, _ = bridge(tmp_path, {"vault_access": False})
    assert "--add-dir" not in b.command("x", None)
    assert not any("Edit(" in r for r in b.allow_rules())


def test_denials_in_json_output_are_kept():
    out = json.dumps({"result": "No pude ver tu calendario.", "session_id": "s1", "permission_denials": [
        {"tool_name": "mcp__claude_ai_Google_Calendar__list_events", "tool_input": {}},
        {"tool_name": "Bash", "tool_input": {"command": "dir"}}]})
    r = parse_output(out)
    assert r["blocked_tools"] == ["mcp__claude_ai_Google_Calendar__list_events", "Bash"] and len(r["blocked"]) == 2


def test_grant_rules_never_allow_a_shell_or_writes():
    assert grant_rule("mcp__claude_ai_Google_Calendar__list_events") == "mcp__claude_ai_Google_Calendar"
    assert grant_rule("Bash") is None and grant_rule("PowerShell") is None and grant_rule("Write") is None
    assert grant_rule("WebFetch") == "WebFetch"


def test_blocked_job_says_how_to_allow_and_grant_asks_again(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_bridge, "find_claude", lambda p=None: "claude")
    saved = {}
    monkeypatch.setattr("assistant.setup_wizard.save_local", lambda u: saved.update(u))
    calls = []

    def runner(cmd, cwd, timeout):
        calls.append(cmd)
        if len(calls) == 1:
            return 0, json.dumps({"result": "No pude ver tu calendario.", "session_id": "s1", "permission_denials": [
                {"tool_name": "mcp__claude_ai_Google_Calendar__list_events", "tool_input": {}},
                {"tool_name": "Bash", "tool_input": {"command": "dir"}}]}), ""
        return 0, json.dumps({"result": "Listo, puse tus clases.", "session_id": "s1"}), ""

    b, _ = bridge(tmp_path, runner=runner)
    b.ask("pon mis clases en el calendario")
    job = b.find()
    assert "dale permiso" in job["spoken"] and "Bash" not in job["spoken"].split("blocked from:")[1]
    r = b.grant(job)
    assert r["allowed"] == ["mcp__claude_ai_Google_Calendar"] and r["never_allowed"] == ["Bash"]
    assert saved["claude"]["granted"] == ["mcp__claude_ai_Google_Calendar"]
    assert "--resume" in calls[-1] and "mcp__claude_ai_Google_Calendar" in b.allow_rules()
    assert b.find()["spoken"] == "Listo, puse tus clases."
