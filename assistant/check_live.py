"""Find which Gemini Live models your API key can use, and which config features work.

    run-pakito.bat check          test every Live model, save the first that works
    run-pakito.bat check full     test Pakito's real config piece by piece on your model
    run-pakito.bat check claude   test the Claude Code link

Writes results into assistant/config.local.json.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

from google import genai

PREFERRED = ["gemini-3.8-live", "gemini-3.1-flash-live-preview"]
LOCAL = Path(__file__).with_name("config.local.json")


async def probe(client, name: str, config: dict | None = None) -> str:
    config = config or {"response_modalities": ["AUDIO"]}
    try:
        async with client.aio.live.connect(model=name, config=config) as s:
            await s.send_realtime_input(text="Say hi in one word.")
            async for msg in s.receive():
                sc = msg.server_content
                if sc and sc.turn_complete:
                    return "OK"
        return "no response"
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:120]}"


async def timed(coro, t=25):
    try:
        return await asyncio.wait_for(coro, timeout=t)
    except asyncio.TimeoutError:
        return "timed out"


def load_local() -> dict:
    return json.loads(LOCAL.read_text(encoding="utf-8")) if LOCAL.exists() else {}


def save_local(cfg: dict) -> None:
    LOCAL.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


async def check_models(client) -> None:
    live = [m.name.removeprefix("models/") for m in client.models.list()
            if "bidiGenerateContent" in (m.supported_actions or [])]
    order = [m for m in PREFERRED if m in live] + [m for m in live if m not in PREFERRED]
    print(f"Live-capable models your key sees: {len(live)}\n")
    working = None
    for name in order:
        res = await timed(probe(client, name), 20)
        print(f"  {'✅' if res == 'OK' else '❌'} {name}: {res}")
        if res == "OK" and not working:
            working = name
    print()
    if not working:
        print("None worked. Check your limits at https://aistudio.google.com/rate-limit")
        return
    cfg = load_local()
    cfg.setdefault("voice", {})["model"] = working
    save_local(cfg)
    print(f"Using {working} → saved to {LOCAL.name}. Now: run-pakito.bat check full")


async def check_full(client) -> None:
    from .config import load_config
    from .hub import Hub
    from .voice import VOICE_DEFAULTS, live_config

    cfg = load_config()
    vcfg = {**VOICE_DEFAULTS, **cfg.get("voice", {})}
    hub = Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)
    full = live_config(hub, {**vcfg, "google_search": True})
    model = vcfg["model"]

    def variant(**keep):
        c = {"response_modalities": ["AUDIO"]}
        if keep.get("prompt"):
            c["system_instruction"] = full["system_instruction"]
        if keep.get("transcripts"):
            c["input_audio_transcription"] = {}
            c["output_audio_transcription"] = {}
        tools = []
        if keep.get("search"):
            tools.append({"google_search": {}})
        if keep.get("functions"):
            tools.append(copy.deepcopy(full["tools"][-1]))
        if tools:
            c["tools"] = tools
        if keep.get("voice") and "speech_config" in full:
            c["speech_config"] = full["speech_config"]
        return c

    steps = [
        ("bare model", {}),
        ("+ system prompt", {"prompt": 1}),
        ("+ transcripts", {"prompt": 1, "transcripts": 1}),
        ("+ Pakito's tools", {"prompt": 1, "transcripts": 1, "functions": 1}),
        ("+ Google Search", {"prompt": 1, "transcripts": 1, "functions": 1, "search": 1}),
        ("Google Search alone", {"search": 1}),
    ]
    print(f"Testing Pakito's config on {model}:\n")
    results = {}
    for label, keep in steps:
        res = await timed(probe(client, model, variant(**keep)))
        results[label] = res
        print(f"  {'✅' if res == 'OK' else '❌'} {label}: {res}")
        await asyncio.sleep(1)
    print()
    search_ok = results["+ Google Search"] == "OK" and results["Google Search alone"] == "OK"
    rest_ok = results["+ Pakito's tools"] == "OK"
    local = load_local()
    local.setdefault("voice", {})["google_search"] = search_ok
    save_local(local)
    if rest_ok and not search_ok:
        print("→ Google Search is what your free tier blocks. Turned it off in config.local.json;\n"
              "  Pakito will use a free search tool instead. Run run-pakito.bat.")
    elif rest_ok and search_ok:
        print("→ Everything works here. If voice still fails, it's the audio itself — tell Claude.")
    else:
        print("→ Something before Google Search fails — paste this to Claude.")


def check_claude() -> None:
    import time
    from .claude_bridge import ClaudeBridge, find_claude
    from .config import data_dir, load_config

    cfg = load_config()
    exe = find_claude(cfg["claude"].get("path"))
    if not exe:
        print("❌ Claude Code not found.\n   PowerShell: irm https://claude.ai/install.ps1 | iex\n"
              "   then open a NEW terminal, run `claude`, log in, and type /exit.")
        return
    print(f"Found Claude Code: {exe}\nAsking a test question (first run can take ~20 s)…")
    out = {}
    b = ClaudeBridge(cfg["claude"], data_dir(cfg), notify=lambda t, m: None, threaded=False)
    b.listeners.append(lambda job: out.update(job) or True)
    t = time.time()
    b.ask("Reply with exactly: Pakito link works.")
    if out.get("status") == "done":
        print(f"✅ {out['spoken']}  ({time.time() - t:.0f} s)")
    else:
        print(f"❌ {out.get('answer')}\n   If it mentions login: open a terminal, run `claude`, log in, /exit, try again.")


async def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "claude":
        check_claude()
        return
    client = genai.Client()
    if len(sys.argv) > 1 and sys.argv[1] == "full":
        await check_full(client)
    else:
        await check_models(client)


if __name__ == "__main__":
    asyncio.run(main())
