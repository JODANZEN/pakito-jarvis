"""Tests run against the original, course-specific starter routines (new installs get a generic set)."""
import pytest

from assistant import hub as hub_mod

COURSE_ROUTINES = """# Routines

Pakito runs these when you say the name or an alias. Edit freely.
Actions: `open: <url>` · `app: <name>` · `book: <name> [page]` · `music: <search>` · `search: <query>` ·
`focus: [minutes] <label>` · `lockin: on`
(no minutes on `focus` = use the trainer's current level; `lockin: on` = blocked apps get closed until the break).
Accepted suggestions get added at the bottom.

## work mode
aliases: work, research, research mode, trabajo
- open: https://claude.ai
- music: lofi beats

## lock in
aliases: study, study mode, lock in mode, lockin, estudiar
- open: https://canvas.asu.edu
- open: https://chatgpt.com
- music: deep focus
- lockin: on
- focus: study

## homework
aliases: homework mode, hw, tarea
- open: https://canvas.asu.edu
- open: https://chatgpt.com
- music: chill study

## phy 310
aliases: mechanics, classical mechanics, mecanica, taylor, phy 310 study
- book: taylor
- focus: PHY 310

## physics reading
aliases: phy 302, phy 302 reading, perusall, reading, lectura
- open: https://app.perusall.com
- open: https://chatgpt.com
"""


@pytest.fixture(autouse=True)
def course_routines(monkeypatch):
    monkeypatch.setattr(hub_mod, "SEED_ROUTINES", COURSE_ROUTINES)
