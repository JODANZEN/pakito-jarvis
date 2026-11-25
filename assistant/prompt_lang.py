"""Pakito's instructions in the language it's set to speak.

Gemini Live has no language switch: it speaks the way its instructions and the conversation lead it. Nov 24: with a
base language set, the English instructions full of Spanish examples still pulled it into Spanish. So when he says
"speak Bulgarian", the master copy (voice.PROMPT_TEMPLATE) is translated once with a Gemini text model:
  * always from the master copy, never from an earlier translation, so the meaning can't drift;
  * only the fixed instructions; the live parts (time, tasks, notes…) are slotted in afterwards as they are;
  * checked before use: every {slot} and every tool name must survive, and the length must stay close. If not, or if
    it isn't ready yet, Pakito uses the master copy plus the "always speak X" rule.
Kept in ~/.pakito/prompts/<Language>.json next to the original it came from (redone when the original changes).
"""
from __future__ import annotations

import hashlib
import json
import re
import string
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

DIR = Path.home() / ".pakito" / "prompts"
TEXT_MODELS = ("gemini-flash-latest", "gemini-3-flash-preview", "gemini-2.5-flash")
ENGLISH = {"english", "inglés", "ingles", "en"}
_lock = threading.Lock()
_busy: set[str] = set()

ASK = """Translate the instructions below for a voice assistant into {lang}.
Rules:
- Keep every placeholder in curly braces exactly as it is, e.g. {{user}}, {{now}}, {{rlines}}: same braces, same name.
- Keep tool and function names exactly (words like ask_claude, start_focus(lockin=true), look_at_tablet).
- Text in quotes that shows what HE might say ("mira esto", "modo trabajo", "dale permiso") stays exactly as it is:
  those are his own commands. Translate everything else, including example replies the assistant would say.
- Same lines, same order, same meaning. Don't add, drop, shorten or explain anything.
Output only the translated text.

---
{text}"""


def source_hash(template: str) -> str:
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]


def fields(text: str) -> list[str]:
    return sorted(f for _, f, _, _ in string.Formatter().parse(text) if f is not None)


def tool_names(text: str) -> set[str]:
    return set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text))


def check(original: str, translated: str) -> list[str]:
    """What's wrong with a translation (empty = good)."""
    problems = []
    try:
        if fields(translated) != fields(original):
            problems.append(f"slots changed: {sorted(set(fields(original)) ^ set(fields(translated)))}")
    except ValueError as e:                          # a stray { or } would break filling it in
        problems.append(f"broken braces: {e}")
    lost = sorted(tool_names(original) - tool_names(translated))
    if lost:
        problems.append(f"tool names lost: {lost[:6]}")
    ratio = len(translated) / max(len(original), 1)
    if not 0.6 <= ratio <= 1.9:
        problems.append(f"length changed too much ({ratio:.2f}x)")
    if "```" in translated:
        problems.append("came back wrapped in a code block")
    return problems


def _file(lang: str) -> Path:
    return DIR / f"{re.sub(r'[^A-Za-z0-9áéíóúñÁÉÍÓÚÑ -]', '', lang).strip().title() or 'Other'}.json"


def cached(lang: str, template: str) -> str | None:
    try:
        d = json.loads(_file(lang).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if d.get("source_hash") == source_hash(template) and not check(template, d.get("text", "")):
        return d["text"]
    return None


def translate(lang: str, template: str, generate: Callable[[str, str], str] | None = None,
              models: tuple = TEXT_MODELS) -> dict:
    """Translate the master copy into `lang` and save it. generate(model, prompt) -> text (Gemini by default)."""
    if generate is None:
        from google import genai
        client = genai.Client()

        def generate(model, prompt):
            return client.models.generate_content(model=model, contents=prompt).text or ""
    last = "no model answered"
    for model in models:
        try:
            out = generate(model, ASK.format(lang=lang, text=template)).strip()
        except Exception as e:
            last = f"{model}: {type(e).__name__}: {str(e)[:120]}"
            continue
        problems = check(template, out)
        if problems:
            last = f"{model}: {'; '.join(problems)}"
            continue
        DIR.mkdir(parents=True, exist_ok=True)
        _file(lang).write_text(json.dumps({"language": lang, "model": model, "made": datetime.now().isoformat(
            timespec="seconds"), "source_hash": source_hash(template), "text": out, "source": template},
            ensure_ascii=False, indent=1), encoding="utf-8")
        return {"translated": lang, "model": model, "file": str(_file(lang))}
    return {"error": f"Couldn't translate the instructions into {lang} ({last}); using the original + the rule."}


def prepare(lang: str | None, template: str, generate=None, log: Callable[[str], None] = lambda m: None) -> None:
    """Make sure a translation exists, in the background (Pakito keeps working with the original meanwhile)."""
    if not lang or lang.strip().lower() in ENGLISH or cached(lang, template):
        return
    with _lock:
        if lang in _busy:
            return
        _busy.add(lang)

    def go():
        try:
            r = translate(lang, template, generate)
            log(f"  (instructions in {lang}: {'ready' if 'error' not in r else r['error']})")
        finally:
            with _lock:
                _busy.discard(lang)
    threading.Thread(target=go, daemon=True, name=f"translate-{lang}").start()


def fill(template: str, lang: str | None, values: dict) -> str:
    """The instructions for this conversation: translated if there's a good translation, else the master copy."""
    text = cached(lang, template) if lang and lang.strip().lower() not in ENGLISH else None
    if text:
        try:
            return text.format(**values)
        except (KeyError, IndexError, ValueError):
            pass
    return template.format(**values)


def wait_ready(lang: str, template: str, timeout: float = 60) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cached(lang, template):
            return True
        time.sleep(0.2)
    return False
