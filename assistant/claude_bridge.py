"""Ask Claude through Claude Code (`claude -p`), using your Claude plan's login — no API key.

Runs in a background thread because it takes ~10–60 s. When an answer arrives:
  * it's appended to <vault>/Assistant/Claude/<date>.md
  * a toast pops up
  * listeners (an open voice conversation) get it so Gemini can say it out loud
  * if nobody heard it yet, it waits in `undelivered()` for the next conversation

Setup (once): PowerShell → irm https://claude.ai/install.ps1 | iex → new terminal → `claude` → log in.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

SYSTEM = (
    "You're answering a question Jose asked his voice assistant (Pakito). He's a CS + Physics "
    "student at ASU. Start with a SPOKEN answer of at most 80 words: plain sentences, no markdown, "
    "no lists, it will be read aloud. Then, only if useful, add a line '---' and fuller detail "
    "(markdown OK) that goes to his notes. For school: help him organize and understand, don't "
    "do graded work for him."
)


def find_claude(configured: str | None = None) -> str | None:
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("claude")
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for p in (home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"):
        if p.exists():
            return str(p)
    return None


def default_runner(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, **kwargs)
    return p.returncode, p.stdout, p.stderr


class ClaudeBridge:
    def __init__(self, cfg: dict, data_dir: Path, notify: Callable[[str, str], None],
                 runner=default_runner, clock: Callable[[], float] = time.time, threaded: bool = True):
        self.cfg = cfg
        self.dir = Path(data_dir) / "Claude"
        self.work = Path(data_dir) / "state" / "claude_workspace"  # neutral cwd: no repo CLAUDE.md/hooks
        self.notify = notify
        self.runner = runner
        self.clock = clock
        self.threaded = threaded
        self.jobs: dict[str, dict] = {}
        self.listeners: list[Callable[[dict], bool]] = []
        self._lock = threading.Lock()

    @property
    def pending(self) -> int:
        return sum(1 for j in self.jobs.values() if j["status"] == "running")

    def command(self, question: str, context: str | None) -> list[str] | None:
        exe = find_claude(self.cfg.get("path"))
        if not exe:
            return None
        prompt = question if not context else f"{question}\n\nContext from the conversation: {context}"
        cmd = [exe, "-p", prompt, "--output-format", "json", "--append-system-prompt", SYSTEM,
               "--permission-mode", "dontAsk", "--allowedTools", self.cfg.get("allowed_tools", "WebSearch,WebFetch")]
        if self.cfg.get("model"):
            cmd += ["--model", self.cfg["model"]]
        return cmd

    def ask(self, question: str, context: str | None = None) -> dict:
        cmd = self.command(question, context)
        if cmd is None:
            return {"error": "Claude Code isn't installed. In PowerShell: irm https://claude.ai/install.ps1 | iex, "
                             "then run `claude` once to log in."}
        job = {"id": uuid.uuid4().hex[:6], "question": question, "status": "running",
               "started": self.clock(), "delivered": False}
        with self._lock:
            self.jobs[job["id"]] = job
        if self.threaded:
            threading.Thread(target=self._run, args=(job, cmd), daemon=True).start()
        else:
            self._run(job, cmd)
        return {"asked": True, "job": job["id"], "note": "Claude is working on it; the answer comes back in ~10-60 s."}

    def _run(self, job: dict, cmd: list[str]) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        try:
            code, out, err = self.runner(cmd, self.work, int(self.cfg.get("timeout_sec", 180)))
            text = ""
            try:
                data = json.loads(out)
                text = data.get("result") or ""
                if data.get("is_error"):
                    code = code or 1
            except json.JSONDecodeError:
                text = out.strip()
            if code != 0 or not text:
                raise RuntimeError((text or err or f"exit code {code}").strip()[:300])
            spoken, _, detail = text.partition("\n---")
            job.update(status="done", answer=text, spoken=spoken.strip(), detail=detail.strip())
        except Exception as e:
            job.update(status="error", answer=f"Claude failed: {e}", spoken=f"Claude couldn't answer: {e}"[:200], detail="")
        job["seconds"] = round(self.clock() - job["started"], 1)
        self._save(job)
        heard = False
        for cb in list(self.listeners):
            try:
                heard = bool(cb(job)) or heard
            except Exception:
                pass
        job["delivered"] = heard
        if not heard:
            self.notify("Claude answered" if job["status"] == "done" else "Claude failed", job["spoken"][:200])

    def _save(self, job: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        now = datetime.fromtimestamp(self.clock())
        f = self.dir / f"{now:%Y-%m-%d}.md"
        head = "" if f.exists() else f"# Claude answers — {now:%Y-%m-%d}\n"
        body = job["spoken"] + (f"\n\n{job['detail']}" if job.get("detail") else "")
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"{head}\n## {now:%H:%M} — {job['question']}\n\n{body}\n")

    def undelivered(self) -> list[dict]:
        return [j for j in self.jobs.values() if j["status"] != "running" and not j["delivered"]]

    def mark_delivered(self, jobs: list[dict]) -> None:
        for j in jobs:
            j["delivered"] = True
