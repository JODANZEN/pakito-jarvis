"""Task list. For now a markdown checklist in the vault (Assistant/Tasks.md, Obsidian checkboxes).

Everything goes through TaskStore's four methods, so swapping in Google Tasks later
(phone sync) means writing one new class, not touching the tools or the debrief.
"""
from __future__ import annotations

import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

HEADER = "# Tasks\n\nPakito keeps this list. Tick boxes here or say \"ya terminé X\".\n\n"
LINE = re.compile(r"^- \[( |x)\] (.+?)(?:  ✅ (\d{4}-\d{2}-\d{2}))?$")


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t.lower())).strip()


class TaskStore:
    def open(self) -> list[str]: ...
    def done_on(self, day: str) -> list[str]: ...
    def add(self, text: str) -> dict: ...
    def complete(self, text: str, day: str) -> dict: ...


class LocalTasks(TaskStore):
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(HEADER, encoding="utf-8")

    def _read(self) -> list[dict]:
        items = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            m = LINE.match(line.strip())
            if m:
                items.append({"done": m.group(1) == "x", "text": m.group(2).strip(), "day": m.group(3)})
        return items

    def _write(self, items: list[dict]) -> None:
        text = self.path.read_text(encoding="utf-8")
        keep = [l for l in text.splitlines() if not LINE.match(l.strip())]
        head = "\n".join(keep).rstrip() + "\n\n"
        open_ = [f"- [ ] {i['text']}" for i in items if not i["done"]]
        done = [f"- [x] {i['text']}" + (f"  ✅ {i['day']}" if i.get("day") else "") for i in items if i["done"]]
        self.path.write_text(head + "\n".join(open_ + done) + "\n", encoding="utf-8")

    def open(self) -> list[str]:
        return [i["text"] for i in self._read() if not i["done"]]

    def done_on(self, day: str) -> list[str]:
        return [i["text"] for i in self._read() if i["done"] and i.get("day") == day]

    def _find(self, items: list[dict], text: str, open_only: bool = True):
        q = _norm(text)
        best, score = None, 0.0
        for i in items:
            if open_only and i["done"]:
                continue
            t = _norm(i["text"])
            s = SequenceMatcher(None, q, t).ratio()
            if q and (q in t or t in q):
                s = max(s, 0.85)
            if s > score:
                best, score = i, s
        return best if score >= 0.6 else None

    def add(self, text: str) -> dict:
        items = self._read()
        if self._find(items, text):
            return {"exists": True, "task": self._find(items, text)["text"]}
        items.append({"done": False, "text": text.strip(), "day": None})
        self._write(items)
        return {"added": text.strip()}

    def complete(self, text: str, day: str) -> dict:
        items = self._read()
        hit = self._find(items, text)
        if hit:
            hit["done"], hit["day"] = True, day
            self._write(items)
            return {"completed": hit["text"], "was_on_list": True}
        items.append({"done": True, "text": text.strip(), "day": day})   # did something not on the list: still log it
        self._write(items)
        return {"completed": text.strip(), "was_on_list": False}


def today_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
