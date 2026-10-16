"""Build jobs: Claude Code doing real work in one of your repos while you do something else.

Safety, in layers:
  1. Isolation  — every job gets its own git worktree (a separate folder) on a new `pakito/...` branch.
                  Your checkout and your other branches are never touched.
  2. Reviewer   — Claude Code's auto mode: a second model checks every action before it runs and blocks
                  anything beyond the task or driven by hostile text it read. Blocked = skipped, it keeps going.
  3. Hard rules — push, switching branches, merges, resets and worktree surgery are denied outright
                  (deny rules hold in every permission mode, even the free one).
  4. You        — nothing reaches your branch until you approve, and nothing leaves the PC until you say push.
                  The review shows every command it ran and every file it touched, recorded from Claude Code's
                  own event stream, not from Claude's summary of itself.

Free mode ("modo libre"): no reviewer, for local jobs you trust. Only in a VISIBLE terminal you can watch and
close, only after you type your passphrase in Pakito's console (`libre`), one job per unlock, and the hard
rules + separate branch still apply.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from .claude_bridge import ClaudeBridge, find_claude

MIN_AUTO_VERSION = (2, 1, 281)          # auto mode in `claude -p` needs this or newer
BUILD_SYSTEM = (
    "You're doing a build job Jose handed off through his voice assistant (Pakito) while he does something "
    "else. You're in a git worktree on a branch made just for this job; his own checkout is untouched. Do the "
    "task end to end: make the changes, run the project's tests or build if there's an obvious way, and commit "
    "your work on this branch with clear messages. Never push, switch branches, merge, or touch other worktrees. "
    "Don't install new packages: if one is needed, say so in your summary. If something needs his decision, make "
    "the reasonable choice, note it, and keep going. Finish with a SPOKEN summary of at most 60 words (plain "
    "sentences, read aloud: what you did, what's left, anything he must decide). Then a line '---' and details "
    "for his review: files changed, how to try it, open questions."
)
FREE_SYSTEM = (
    "Jose started this session in free mode and is watching. You're in a git worktree on a branch made for this "
    "job; his own checkout is untouched. Commit your work on this branch. Never push, switch branches or merge."
)
HARD_DENY = ["Bash(git push*)", "Bash(git checkout*)", "Bash(git switch*)", "Bash(git merge*)",
             "Bash(git rebase*)", "Bash(git reset --hard*)", "Bash(git worktree*)", "Bash(git branch -D*)",
             "Bash(git remote*)", "Bash(git config*)"]
PASS_FILE = Path(__file__).with_name(".free_mode")       # salted hash of your passphrase; gitignored
UNLOCK_MIN = 10


def default_git(args: list[str], cwd: Path) -> tuple[int, str]:
    kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL, **kwargs)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def claude_version(exe: str) -> tuple[int, ...] | None:
    try:
        kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15, **kwargs).stdout
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
        return tuple(int(x) for x in m.groups()) if m else None
    except Exception:
        return None


def slug(text: str, words: int = 5) -> str:
    parts = ["".join(c for c in w.lower() if c.isalnum()) for w in text.split()[:words]]
    return "-".join(p for p in parts if p)[:40].strip("-") or "job"


# ---------- passphrase for free mode ----------
def set_passphrase(phrase: str, path: Path = PASS_FILE) -> None:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", phrase.encode(), bytes.fromhex(salt), 200_000).hex()
    Path(path).write_text(json.dumps({"salt": salt, "hash": h}), encoding="utf-8")


def check_passphrase(phrase: str, path: Path = PASS_FILE) -> bool:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    h = hashlib.pbkdf2_hmac("sha256", phrase.encode(), bytes.fromhex(d["salt"]), 200_000).hex()
    return secrets.compare_digest(h, d["hash"])


class Builds:
    def __init__(self, bridge: ClaudeBridge, cfg: dict, data_dir: Path, git=default_git,
                 open_terminal: Callable[[list[str], str], None] | None = None,
                 open_files: Callable[[list[str]], None] | None = None,
                 version: Callable[[str], tuple | None] = claude_version, pass_file: Path = PASS_FILE):
        self.bridge, self.cfg, self.git = bridge, cfg, git
        self.reviews = Path(data_dir) / "Builds"
        self.state = Path(data_dir) / "state"
        self.open_terminal = open_terminal
        self.open_files = open_files
        self.version = version
        self.pass_file = pass_file
        self.free_until = 0.0
        bridge.finishers["build"] = self._finish

    # ---------- start ----------
    def _prepare(self, task: str, repo: Path, project: str) -> dict:
        repo = Path(repo)
        code, _ = self.git(["rev-parse", "--show-toplevel"], repo)
        if code != 0:
            return {"error": f"{project} isn't a git repo, so there's no safe branch to work on."}
        busy = [j for j in self.bridge.running() if j.get("kind") == "build" and j.get("project") == project]
        if busy:
            return {"error": f"Claude is already building in {project} ('{busy[0]['question'][:60]}'). "
                             "Wait for it, or send a follow-up when it's done."}
        _, base = self.git(["rev-parse", "HEAD"], repo)
        short = uuid.uuid4().hex[:4]
        branch = f"pakito/{slug(task)}-{short}"
        wt = repo.parent / ".pakito-worktrees" / f"{repo.name}-{slug(task, 3)}-{short}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        code, out = self.git(["worktree", "add", "-b", branch, str(wt), "HEAD"], repo)
        if code != 0:
            return {"error": f"Couldn't make a branch for it: {out[:200]}"}
        return {"repo": str(repo), "worktree": str(wt), "branch": branch, "base": base.strip(), "project": project}

    def _flags(self) -> list[str]:
        return ["--append-system-prompt", BUILD_SYSTEM, "--permission-mode", "auto",
                "--max-turns", str(self.cfg.get("build_max_turns", 80)), "--disallowedTools", *HARD_DENY]

    def _timeout(self) -> int:
        return int(self.cfg.get("build_timeout_min", 20)) * 60

    def start(self, task: str, repo: Path, project: str, context: str | None = None) -> dict:
        exe = find_claude(self.cfg.get("path"))
        if not exe:
            return {"error": "Claude Code isn't installed."}
        v = self.version(exe)
        if v is not None and v < MIN_AUTO_VERSION:
            return {"error": f"Claude Code {'.'.join(map(str, v))} is too old for safe background builds. "
                             "Run `claude update` in a terminal, then ask again."}
        prep = self._prepare(task, repo, project)
        if "error" in prep:
            return prep
        prompt = (f"{task}\n\n(Project: {project}. You're on branch {prep['branch']} in a worktree made for this "
                  "job." + (f" Context from the conversation: {context}" if context else "") + ")")
        cmd = self.bridge.base_cmd(prompt, output="stream-json") + self._flags()
        job = self.bridge.new_job("build", task, mode="auto", **prep)
        self.bridge.start(job, cmd, Path(prep["worktree"]), self._timeout())
        return {"started": True, "job": job["id"], "branch": prep["branch"],
                "note": "Claude is building it in the background on its own branch, with a safety reviewer on. "
                        "Takes a few minutes; Pakito will say when it's ready to review."}

    def follow_up(self, prev: dict, instruction: str) -> dict:
        if prev.get("review") in ("merged", "discarded"):
            return {"error": f"That job was already {prev['review']}. Start a new build instead."}
        if not Path(prev.get("worktree", "")).exists():
            return {"error": "That job's folder is gone. Start a new build instead."}
        cmd = self.bridge.base_cmd(instruction, output="stream-json") + self._flags() + ["--resume", prev["session_id"]]
        extra = {k: prev[k] for k in ("project", "repo", "worktree", "branch", "base")}
        job = self.bridge.new_job("build", instruction, mode="auto", follows=prev["id"], **extra)
        prev["review"] = "superseded"
        self.bridge.start(job, cmd, Path(prev["worktree"]), self._timeout())
        return {"started": True, "job": job["id"], "continues": prev["id"],
                "note": "Sent to the same Claude session on the same branch; it remembers the earlier work."}

    # ---------- after a run ----------
    def _finish(self, job: dict) -> None:
        wt, base = Path(job["worktree"]), job.get("base", "HEAD")
        _, dirty = self.git(["status", "--porcelain"], wt)
        if dirty.strip():                    # keep everything it did on the branch, so the review is complete
            self.git(["add", "-A"], wt)
            self.git(["commit", "-m", f"pakito: uncommitted work from job {job['id']}"], wt)
        _, log = self.git(["log", "--oneline", f"{base}..HEAD"], wt)
        _, files = self.git(["diff", "--stat", f"{base}..HEAD"], wt)
        _, diff = self.git(["diff", f"{base}..HEAD"], wt)
        job["commits"] = [l for l in log.splitlines() if l.strip()][:30]
        job["files_changed"] = [l.strip() for l in files.splitlines() if "|" in l][:40]
        job["review"] = "waiting" if job["commits"] else "nothing to review"
        self.reviews.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(job["started"]).strftime("%Y-%m-%d")
        (self.reviews / f"{stamp}-{job['id']}.diff").write_text(diff, encoding="utf-8")
        (self.reviews / f"{stamp}-{job['id']}.md").write_text(self.report(job), encoding="utf-8")

    def report(self, job: dict) -> str:
        acts = job.get("actions") or []
        cmds = [a["what"] for a in acts if a["tool"] == "Bash"]
        edits = sorted({a["what"] for a in acts if a["tool"] in ("Edit", "Write", "MultiEdit", "NotebookEdit")})
        web = [a["what"] for a in acts if a["tool"] in ("WebFetch", "WebSearch")]
        lines = [f"# Build review: {job['question'][:80]}", "",
                 f"- **Project:** {job.get('project')}  ·  **Branch:** `{job.get('branch')}`",
                 f"- **Status:** {job['status']}  ·  **Mode:** {job.get('mode', 'auto')}",
                 f"- **Folder:** `{job.get('worktree')}`", "",
                 "Say **\"apruébalo\"** to merge into your branch, **\"descártalo\"** to throw it away, or tell "
                 "Pakito what to change. Nothing is pushed until you say **\"súbelo\"**.", "",
                 "## Claude's summary", "", job.get("spoken", ""), "", job.get("detail", ""), "",
                 f"## Commands it ran ({len(cmds)})", ""]
        lines += [f"- `{c}`" for c in cmds] or ["- none"]
        lines += ["", f"## Files it edited ({len(edits)})", ""] + ([f"- {e}" for e in edits] or ["- none"])
        if web:
            lines += ["", "## Web it read", ""] + [f"- {w}" for w in web]
        if job.get("blocked"):
            lines += ["", "## Blocked by the reviewer / rules", ""] + [f"- {b}" for b in job["blocked"]]
        lines += ["", "## Commits", ""] + ([f"- {c}" for c in job.get("commits", [])] or ["- none"])
        lines += ["", "## Changes", ""] + [f"- {f}" for f in job.get("files_changed", [])]
        lines += ["", f"Full diff: `{job['id']}.diff` next to this file."]
        return "\n".join(lines) + "\n"

    # ---------- your decisions ----------
    def _paths(self, job: dict) -> list[str]:
        stamp = datetime.fromtimestamp(job["started"]).strftime("%Y-%m-%d")
        return [str(self.reviews / f"{stamp}-{job['id']}.md"), str(self.reviews / f"{stamp}-{job['id']}.diff")]

    def review(self, job: dict) -> dict:
        if job.get("mode") == "free" and job["status"] == "open":
            self._finish(job)
        paths = [p for p in self._paths(job) if Path(p).exists()]
        if not paths:
            return {"error": "No review yet; the job is still running." if job["status"] == "running"
                    else "No review file for that job."}
        if self.open_files:
            self.open_files(paths)
        return {"opened": paths, "summary": self.bridge.summary(job)}

    def approve(self, job: dict) -> dict:
        if job["status"] == "running":
            return {"error": "It's still running. Wait for it to finish."}
        if job.get("review") in ("merged", "discarded"):
            return {"error": f"Already {job['review']}."}
        if job.get("mode") == "free" and job["status"] == "open":
            self._finish(job)
        repo = Path(job["repo"])
        _, dirty = self.git(["status", "--porcelain"], repo)
        if dirty.strip():
            return {"error": f"Your {job.get('project')} checkout has uncommitted changes. Commit or stash them "
                             "first, then approve again."}
        _, here = self.git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
        code, out = self.git(["merge", "--no-ff", job["branch"], "-m",
                              f"Merge Pakito job {job['id']}: {job['question'][:60]}"], repo)
        if code != 0:
            self.git(["merge", "--abort"], repo)
            return {"error": f"Merging into {here} hit conflicts, so nothing changed. Fix it by hand or ask "
                             f"Claude for a follow-up. ({out[:150]})"}
        self.git(["worktree", "remove", "--force", job["worktree"]], repo)
        self.git(["branch", "-d", job["branch"]], repo)
        job.update(review="merged", merged_into=here.strip())
        self.bridge.persist()
        return {"merged": True, "into": here.strip(), "pushed": False,
                "note": "Merged locally only. Say 'súbelo' when you want it pushed."}

    def discard(self, job: dict) -> dict:
        if job["status"] == "running":
            return {"error": "It's still running. Wait for it to finish first."}
        if job.get("review") == "merged":
            return {"error": "Already merged; revert it in git if you don't want it."}
        repo = Path(job["repo"])
        self.git(["worktree", "remove", "--force", job["worktree"]], repo)
        self.git(["branch", "-D", job["branch"]], repo)
        job["review"] = "discarded"
        self.bridge.persist()
        return {"discarded": True, "branch": job["branch"]}

    def push(self, job: dict, confirm: bool = False) -> dict:
        if job.get("review") != "merged":
            return {"error": "Only approved (merged) work can be pushed. Approve it first."}
        if not confirm:
            return {"needs_confirmation": True,
                    "say": f"¿Subo {job.get('project')} ({job.get('merged_into')}) a GitHub? Say yes to push."}
        code, out = self.git(["push"], Path(job["repo"]))
        if code != 0:
            return {"error": f"Push failed: {out[:200]}"}
        job["review"] = "pushed"
        self.bridge.persist()
        return {"pushed": True, "branch": job.get("merged_into")}

    # ---------- free mode ----------
    def unlock(self, phrase: str, now: float) -> bool:
        if check_passphrase(phrase, self.pass_file):
            self.free_until = now + UNLOCK_MIN * 60
            return True
        return False

    def free(self, task: str, repo: Path, project: str, now: float) -> dict:
        if now > self.free_until:
            return {"error": "Free mode is locked. Type `libre` in Pakito's console and your passphrase to unlock "
                             "it for one job (saying it out loud doesn't count)."}
        exe = find_claude(self.cfg.get("path"))
        if not exe or not self.open_terminal:
            return {"error": "Claude Code isn't installed."}
        prep = self._prepare(task, repo, project)
        if "error" in prep:
            return prep
        self.free_until = 0.0                                   # one job per unlock
        tasks = self.state / "free_tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        job = self.bridge.new_job("build", task, mode="free", **prep)
        job["status"] = "open"
        task_file = tasks / f"{job['id']}.md"
        task_file.write_text(task, encoding="utf-8")
        # the task goes through a file so nothing he said ends up parsed by the shell
        prompt = f"Read the task in {task_file} and do it."
        self.open_terminal([exe, "--dangerously-skip-permissions", "--append-system-prompt", FREE_SYSTEM,
                            "--disallowedTools", *HARD_DENY, "--", prompt], prep["worktree"])
        self.bridge.persist()
        return {"opened": "free mode", "job": job["id"], "branch": prep["branch"],
                "note": "Claude Code is open in a new window with no permission checks. Watch it; close the window "
                        "to stop. When it's done, say 'revisa' to review, then approve or discard."}
