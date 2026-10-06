"""Routines live in <vault>/Assistant/Routines.md so you can edit them in Obsidian.

Format:

    ## cse 355 study
    aliases: 355, theory of computation
    - open: https://canvas.asu.edu
    - app: vscode
    - music: lofi beats
    - focus: CSE 355            (optional minutes first: "focus: 30 CSE 355")

Action keys: open (url), app (name from config["apps"] or a command), music, search, focus,
lockin (`- lockin: on` makes this routine's focus block a lock-in block).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

ACTION_KEYS = ("open", "app", "music", "search", "focus", "lockin", "book")

HEADER = """# Routines

Pakito runs these when you say the name or an alias. Edit freely.
Actions: `open: <url>` · `app: <name>` · `music: <search>` · `search: <query>` · `focus: [minutes] <label>`
New ones get added at the bottom when you accept a suggestion.
"""


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse(md: str) -> list[dict]:
    routines, cur = [], None
    for raw in md.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            cur = {"name": line[3:].strip(), "aliases": [], "actions": []}
            routines.append(cur)
        elif cur is None or not line:
            continue
        elif line.lower().startswith("aliases:"):
            cur["aliases"] = [a.strip() for a in line.split(":", 1)[1].split(",") if a.strip()]
        elif line.startswith("- ") and ":" in line:
            key, val = line[2:].split(":", 1)
            key = key.strip().lower()
            if key in ACTION_KEYS:
                cur["actions"].append({"type": key, "value": val.strip()})
    return [r for r in routines if r["actions"]]


def render(r: dict) -> str:
    lines = [f"## {r['name']}"]
    if r.get("aliases"):
        lines.append("aliases: " + ", ".join(r["aliases"]))
    lines += [f"- {a['type']}: {a['value']}" for a in r["actions"]]
    return "\n".join(lines) + "\n"


def action_signature(actions: list[dict]) -> tuple:
    """Order-insensitive identity of a routine's actions (focus minutes ignored)."""
    sig = []
    for a in actions:
        v = a["value"]
        if a["type"] == "focus":
            v = re.sub(r"^\d+(\.\d+)?\s*", "", v)
        sig.append((a["type"], normalize(v) if a["type"] != "open" else v.strip().rstrip("/")))
    return tuple(sorted(sig))


class Routines:
    def __init__(self, path: Path, seed: str | None = None):
        self.path = Path(path)
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(seed or HEADER, encoding="utf-8")

    def all(self) -> list[dict]:
        return parse(self.path.read_text(encoding="utf-8"))

    def match(self, utterance: str, cutoff: float = 0.78) -> dict | None:
        u = normalize(utterance)
        if not u:
            return None
        best, best_score = None, 0.0
        for r in self.all():
            for name in [r["name"], *r["aliases"]]:
                n = normalize(name)
                if not n:
                    continue
                if u == n:
                    return r
                score = SequenceMatcher(None, u, n).ratio()
                # "start the cse 355 study thing" contains "cse 355 study"
                if re.search(rf"\b{re.escape(n)}\b", u) and len(n) >= 3:
                    score = max(score, 0.9)
                if score > best_score:
                    best, best_score = r, score
        return best if best_score >= cutoff else None

    def add(self, name: str, actions: list[dict], aliases: list[str] | None = None) -> dict:
        r = {"name": name.strip(), "aliases": aliases or [], "actions": actions}
        text = self.path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        self.path.write_text(text + "\n" + render(r), encoding="utf-8")
        return r
