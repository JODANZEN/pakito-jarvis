"""Typed console for v0 (voice comes next and uses the same hub).

    python -m assistant

Anything that isn't a command is treated as a request: if it matches a routine it runs,
otherwise Pakito watches the commands you run next and learns them.
"""
from __future__ import annotations

import json
import sys

from .hub import Hub

HELP = """\
focus [min] [label]   start a block (no minutes = trainer level)   e.g. focus cse 355 · focus 45 phy 302
d [note]              log a distraction                              e.g. d phone
stop                  end the block early / skip the break
status · stats [days]
open <url> · app <name> · music <query> · search <query>
claude <question>     ask Claude (answer arrives as a notification + in the vault)
recent                what Pakito did in the last 2 h
today                 how today is going · debrief = end the day (stops block, pauses music, saves the note)
routines              list routines
suggest               routines it thinks you should save
save <n> [name]       save suggestion n · nope <n> = never suggest it again
done                  close the current request (so the next actions don't count toward it)
help · quit
anything else         a request: runs a matching routine, or starts learning one
"""


def show(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def parse_focus(rest: str) -> dict:
    parts = rest.split(maxsplit=1)
    if parts:
        try:
            return {"minutes": float(parts[0]), "label": parts[1] if len(parts) > 1 else None}
        except ValueError:
            pass
    return {"label": rest or None}


def handle(hub: Hub, line: str) -> bool:
    line = line.strip()
    if not line:
        return True
    cmd, _, rest = line.partition(" ")
    cmd, rest = cmd.lower(), rest.strip()
    c = hub.call
    if cmd in ("quit", "exit", "q"):
        if hub.focus.phase == "focus" and not getattr(hub, "_quit_warned", False):
            hub._quit_warned = True
            print("A focus block is running (timers die when this closes). 'stop' to log it, or 'quit' again to drop it.")
            return True
        hub.end_request()
        return False
    if cmd == "help":
        print(HELP)
    elif cmd == "focus":
        show(c("start_focus", parse_focus(rest)))
    elif cmd in ("d", "distracted"):
        show(c("log_distraction", {"note": rest or None}))
    elif cmd == "stop":
        show(c("stop_focus"))
    elif cmd == "status":
        show(c("focus_status"))
    elif cmd == "stats":
        show(c("focus_stats", {"days": float(rest) if rest else 7}))
    elif cmd == "open" and rest:
        show(c("open_url", {"url": rest}))
    elif cmd == "app" and rest:
        show(c("open_app", {"name": rest}))
    elif cmd == "music" and rest:
        show(c("play_music", {"query": rest}))
    elif cmd == "search" and rest:
        show(c("web_search", {"query": rest}))
    elif cmd == "claude" and rest:
        show(c("ask_claude", {"question": rest}))
    elif cmd == "today":
        print(c("day_summary")["spoken"])
    elif cmd == "debrief":
        r = c("end_day")
        print(r["spoken"] + f"\n(saved to {r['note']})")
    elif cmd == "recent":
        print("\n".join(hub.recent(limit=20)) or "nothing yet")
    elif cmd == "routines":
        for r in c("list_routines")["routines"]:
            al = f"  ({', '.join(r['aliases'])})" if r["aliases"] else ""
            print(f"• {r['name']}{al}: " + " · ".join(f"{a['type']} {a['value']}" for a in r["actions"]))
    elif cmd == "suggest":
        hub.end_request()
        sug = c("routine_suggestions")["suggestions"]
        if not sug:
            print("Nothing repeated enough yet.")
        for i, s in enumerate(sug, 1):
            print(f"{i}. '{s['name']}' ×{s['count']}: " + " · ".join(f"{a['type']} {a['value']}" for a in s["actions"]))
    elif cmd == "save" and rest:
        n, _, name = rest.partition(" ")
        show(c("save_routine", {"index": float(n), "name": name or None}))
    elif cmd == "nope" and rest:
        show(c("dismiss_suggestion", {"index": float(rest)}))
    elif cmd == "done":
        s = hub.end_request()
        print("ok" if not s else f"Suggestion: save '{s['name']}'? → type 'suggest'")
    else:
        out = hub.request(line)
        if out.get("routine"):
            print(f"▶ {out['routine']}")
        else:
            print("No routine for that yet. Do it with commands and I'll learn it after a few times.")
        if out.get("suggestion"):
            print(f"💡 You keep doing '{out['suggestion']['name']}' the same way — type 'suggest'.")
    return True


def main() -> None:
    hub = Hub()
    hub.blocker.start()
    from . import server
    server.start(hub)
    print(f"Pakito v0 · vault: {hub.dir} · focus level {hub.focus.level} min · 'help' for commands")
    try:
        while True:
            try:
                line = input("› ")
            except EOFError:
                break
            if not handle(hub, line):
                break
    except KeyboardInterrupt:
        pass
    hub.end_request()
    if hub.focus.phase == "focus":
        print("Note: a focus block was running; it was not logged.")


if __name__ == "__main__":
    sys.exit(main())
