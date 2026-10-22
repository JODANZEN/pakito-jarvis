"""The slow brain: Claude through Claude Code (`claude -p`), on your Claude plan's login — no API key.

Jobs run in background threads. Every job (questions here, builds in builds.py) lives in ONE ledger,
state/jobs.json, which the voice side reads too, so both brains agree on what's running and done.
When a job finishes:
  * it's appended to <vault>/Assistant/Claude/<date>.md
  * a toast pops up
  * listeners (an open voice conversation) get it so Gemini can say it out loud
  * if nobody heard it yet, it waits in `undelivered()` for the next conversation
Follow-ups ("y también…") resume the same Claude session, so it remembers the earlier work.

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
    "You're answering a question {who} asked their voice assistant (Pakito).{about} Start with a SPOKEN answer of "
    "at most 80 words: plain sentences, no markdown, no lists, it will be read aloud. Then, only if useful, add a "
    "line '---' and fuller detail (markdown OK) that goes to their notes. For school: help them organize and "
    "understand, don't do graded work for them."
)
KEEP_JOBS = 40


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


def is_login_error(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ("failed to authenticate", "oauth", "not logged in", "please run /login",
                                "invalid api key", "authentication_error", "login expired"))


def default_runner(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, **kwargs)
    return p.returncode, p.stdout, p.stderr


def parse_output(out: str) -> dict:
    """Claude Code's output -> {result, session_id, is_error, actions}.
    Handles --output-format json (one object) and stream-json (one event per line; we keep every
    tool call it made, straight from Claude Code, not from Claude's own summary)."""
    out = out.strip()
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            return {"result": data.get("result") or "", "session_id": data.get("session_id"),
                    "is_error": bool(data.get("is_error")), "actions": [], "blocked": []}
    except json.JSONDecodeError:
        pass
    res = {"result": "", "session_id": None, "is_error": False, "actions": [], "blocked": []}
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        res["session_id"] = ev.get("session_id") or res["session_id"]
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    res["actions"].append(_describe(block.get("name", "?"), block.get("input") or {}))
        elif ev.get("type") == "result":
            res["result"] = ev.get("result") or ""
            res["is_error"] = bool(ev.get("is_error"))
            for d in ev.get("permission_denials") or []:         # what the reviewer / rules stopped
                if isinstance(d, dict):
                    b = _describe(d.get("tool_name", "?"), d.get("tool_input") or {})
                    res["blocked"].append(f"{b['tool']}: {b['what']}")
    if not res["result"] and not res["actions"]:
        res["result"] = out
    return res


def _describe(name: str, inp: dict) -> dict:
    if name == "Bash":
        return {"tool": "Bash", "what": str(inp.get("command", ""))[:300]}
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return {"tool": name, "what": str(inp.get("file_path") or inp.get("notebook_path") or "")}
    if name in ("Read", "Glob", "Grep"):
        return {"tool": name, "what": str(inp.get("file_path") or inp.get("pattern") or inp.get("path") or "")[:200]}
    if name in ("WebFetch", "WebSearch"):
        return {"tool": name, "what": str(inp.get("url") or inp.get("query") or "")[:200]}
    return {"tool": name, "what": json.dumps(inp, ensure_ascii=False)[:200]}


class ClaudeBridge:
    def __init__(self, cfg: dict, data_dir: Path, notify: Callable[[str, str], None],
                 runner=default_runner, clock: Callable[[], float] = time.time, threaded: bool = True,
                 who: str = "the user", about: str = "", local=lambda p: p):
        self.cfg = cfg
        self.system = SYSTEM.format(who=who, about=f" About them: {about.strip()}" if about and about.strip() else "")
        self.dir = Path(data_dir) / "Claude"
        self.work = local(Path(data_dir) / "state" / "claude_workspace")  # neutral cwd: no repo CLAUDE.md/hooks
        self.ledger = local(Path(data_dir) / "state" / "jobs.json")      # each computer runs its own jobs
        self.notify = notify
        self.runner = runner
        self.clock = clock
        self.threaded = threaded
        self.jobs: dict[str, dict] = {}
        self.listeners: list[Callable[[dict], bool]] = []
        self.finishers: dict[str, Callable[[dict], None]] = {}    # kind -> extra step after a run (builds)
        self._lock = threading.Lock()
        self._load()

    # ---------- the shared ledger ----------
    def _load(self) -> None:
        try:
            for j in json.loads(self.ledger.read_text(encoding="utf-8")):
                if j.get("status") == "running":                # Pakito restarted mid-job
                    j.update(status="interrupted", spoken="That job was cut off when Pakito restarted.")
                j.setdefault("delivered", True)
                self.jobs[j["id"]] = j
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def persist(self) -> None:
        with self._lock:
            keep = sorted(self.jobs.values(), key=lambda j: j.get("started", 0))[-KEEP_JOBS:]
            self.jobs = {j["id"]: j for j in keep}
            data = json.dumps(keep, ensure_ascii=False, indent=1, default=str)
        try:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.ledger.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self.ledger)
        except OSError:
            pass

    @property
    def pending(self) -> int:
        """Quick questions still running. A voice conversation stays open for these (not for builds)."""
        return sum(1 for j in self.jobs.values() if j["status"] == "running" and j.get("kind", "ask") == "ask")

    def running(self) -> list[dict]:
        return [j for j in self.jobs.values() if j["status"] == "running"]

    def find(self, ref: str | None = None, kind: str | None = None, with_session: bool = False) -> dict | None:
        """A job by id, by a word from its task or project, or the latest one."""
        jobs = sorted(self.jobs.values(), key=lambda j: j.get("started", 0), reverse=True)
        if kind:
            jobs = [j for j in jobs if j.get("kind", "ask") == kind]
        if with_session:
            jobs = [j for j in jobs if j.get("session_id")]
        if not ref:
            return jobs[0] if jobs else None
        r = ref.lower().strip()
        for j in jobs:
            if j["id"] == r or r in f"{j.get('question', '')} {j.get('project') or ''}".lower():
                return j
        return None

    def summary(self, job: dict) -> dict:
        keys = ("id", "kind", "status", "question", "project", "branch", "spoken", "review", "commits",
                "files_changed", "blocked")
        out = {k: job.get(k) for k in keys if job.get(k) not in (None, "", [])}
        out["minutes"] = round(((job.get("finished") or self.clock()) - job.get("started", self.clock())) / 60, 1)
        if job.get("actions"):
            out["commands_run"] = [a["what"] for a in job["actions"] if a["tool"] == "Bash"][-8:]
        return out

    def new_job(self, kind: str, question: str, **extra) -> dict:
        job = {"id": uuid.uuid4().hex[:6], "kind": kind, "question": question, "status": "running",
               "started": self.clock(), "delivered": False, **extra}
        with self._lock:
            self.jobs[job["id"]] = job
        self.persist()
        return job

    def start(self, job: dict, cmd: list[str], cwd: Path, timeout: int) -> None:
        if self.threaded:
            threading.Thread(target=self._run, args=(job, cmd, cwd, timeout), daemon=True).start()
        else:
            self._run(job, cmd, cwd, timeout)

    # ---------- questions ----------
    def base_cmd(self, prompt: str, output: str = "json") -> list[str] | None:
        exe = find_claude(self.cfg.get("path"))
        if not exe:
            return None
        cmd = [exe, "-p", prompt, "--output-format", output] + (["--verbose"] if output == "stream-json" else [])
        if self.cfg.get("model"):
            cmd += ["--model", self.cfg["model"]]
        return cmd

    def _ask_flags(self) -> list[str]:
        return ["--append-system-prompt", self.system, "--permission-mode", "dontAsk",
                "--allowedTools", self.cfg.get("allowed_tools", "WebSearch,WebFetch")]

    def command(self, question: str, context: str | None, image: str | None = None) -> list[str] | None:
        prompt = question if not context else f"{question}\n\nContext from the conversation: {context}"
        if image:
            prompt += (f"\n\nHe's asking about what's on his screen: the screenshot is the file {image} in your "
                       "working directory. Read it first.")
        cmd = self.base_cmd(prompt)
        if cmd is None:
            return None
        flags = self._ask_flags()
        if image:
            i = flags.index("--allowedTools") + 1
            flags[i] = flags[i] + ",Read"
        return cmd + flags

    def ask(self, question: str, context: str | None = None, image: Path | None = None) -> dict:
        cmd = self.command(question, context, image=Path(image).name if image else None)
        if cmd is None:
            return {"error": "Claude Code isn't installed. In PowerShell: irm https://claude.ai/install.ps1 | iex, "
                             "then run `claude` once to log in."}
        job = self.new_job("ask", question, **({"cleanup": str(image)} if image else {}))
        self.work.mkdir(parents=True, exist_ok=True)
        self.start(job, cmd, self.work, int(self.cfg.get("timeout_sec", 180)))
        return {"asked": True, "job": job["id"], "note": "Claude is working on it; the answer comes back in ~10-60 s."}

    def follow_up_ask(self, prev: dict, instruction: str) -> dict:
        cmd = self.base_cmd(instruction)
        if cmd is None:
            return {"error": "Claude Code isn't installed."}
        cmd += self._ask_flags() + ["--resume", prev["session_id"]]
        job = self.new_job("ask", instruction, follows=prev["id"])
        self.work.mkdir(parents=True, exist_ok=True)
        self.start(job, cmd, self.work, int(self.cfg.get("timeout_sec", 180)))
        return {"asked": True, "job": job["id"], "continues": prev["id"]}

    # ---------- running any job ----------
    def _run(self, job: dict, cmd: list[str], cwd: Path, timeout: int) -> None:
        try:
            try:
                code, out, err = self.runner(cmd, cwd, timeout)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"ran out of time ({max(1, timeout // 60)} min)")
            data = parse_output(out)
            job["session_id"] = data["session_id"] or job.get("session_id")
            if data["actions"]:
                job["actions"] = (job.get("actions") or []) + data["actions"]
            if data.get("blocked"):
                job["blocked"] = (job.get("blocked") or []) + data["blocked"]
            text = data["result"]
            if data["is_error"]:
                code = code or 1
            if code != 0 or not text:
                raise RuntimeError((text or err or f"exit code {code}").strip()[:300])
            spoken, _, detail = text.partition("\n---")
            job.update(status="done", answer=text, spoken=spoken.strip(), detail=detail.strip())
        except Exception as e:
            what = "the build" if job.get("kind") == "build" else "an answer"
            if is_login_error(str(e)):
                job.update(status="error", login_expired=True, answer=f"Claude failed: {e}", detail="",
                           spoken="Claude's login on this PC expired. Open a terminal, run claude, log in again, "
                                  "then ask me again.")
            else:
                job.update(status="error", answer=f"Claude failed: {e}",
                           spoken=f"Claude couldn't finish {what}: {e}"[:200], detail="")
        if job.get("cleanup"):                          # e.g. a screenshot: never kept after the job
            try:
                Path(job.pop("cleanup")).unlink(missing_ok=True)
            except OSError:
                pass
        finish = self.finishers.get(job.get("kind", "ask"))
        if finish:
            try:
                finish(job)
            except Exception as e:
                job["finish_error"] = str(e)[:200]
        job["finished"] = self.clock()
        job["seconds"] = round(job["finished"] - job["started"], 1)
        self._save(job)
        heard = False
        for cb in list(self.listeners):
            try:
                heard = bool(cb(job)) or heard
            except Exception:
                pass
        job["delivered"] = heard
        self.persist()
        if not heard:
            ok = job["status"] == "done"
            title = ("Claude finished the build" if ok else "Claude's build failed") if job.get("kind") == "build" \
                else ("Claude answered" if ok else "Claude failed")
            self.notify(title, job["spoken"][:200])

    def _save(self, job: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        now = datetime.fromtimestamp(self.clock())
        f = self.dir / f"{now:%Y-%m-%d}.md"
        head = "" if f.exists() else f"# Claude answers — {now:%Y-%m-%d}\n"
        body = job["spoken"] + (f"\n\n{job['detail']}" if job.get("detail") else "")
        if job.get("review"):
            body += f"\n\nReview: {job['review']}"
        label = "Build: " if job.get("kind") == "build" else ""
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"{head}\n## {now:%H:%M} — {label}{job['question']}\n\n{body}\n")

    def undelivered(self) -> list[dict]:
        return [j for j in self.jobs.values() if j["status"] not in ("running", "open") and not j["delivered"]]

    def mark_delivered(self, jobs: list[dict]) -> None:
        for j in jobs:
            j["delivered"] = True
        if jobs:
            self.persist()
