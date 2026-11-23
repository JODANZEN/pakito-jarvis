"""Find which Gemini Live models your API key can use, and which config features work.

    run-pakito.bat check          test every Live model, save the first that works
    run-pakito.bat check full     test Pakito's real config piece by piece on your model
    run-pakito.bat check claude   test the Claude Code link
    run-pakito.bat check speed    time each Live model with Pakito's real setup, keep the fastest
    run-pakito.bat check look     can your model see a screenshot? (sends a picture with a number on it)
    run-pakito.bat check dictation   pick the model that cleans up your dictation, and check it takes audio
    run-pakito.bat check notes   record a 30 s note and show exactly what take_note would save (inbox untouched)

Writes results into assistant/config.local.json.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

from google import genai

PREFERRED = ["gemini-3.1-flash-live-preview", "gemini-3.8-live"]   # 3.1 works on the free tier
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


def live_models(client) -> list[str]:
    live = [m.name.removeprefix("models/") for m in client.models.list()
            if "bidiGenerateContent" in (m.supported_actions or [])]
    return [m for m in PREFERRED if m in live] + [m for m in live if m not in PREFERRED]


async def first_working(client, say=print) -> str | None:
    """The first Live model this key can really talk to (setup uses it); stops at the first that works."""
    for name in live_models(client):
        res = await timed(probe(client, name), 20)
        say(f"  {'✅' if res == 'OK' else '❌'} {name}: {res}")
        if res == "OK":
            return name
    return None


async def check_models(client) -> None:
    order = live_models(client)
    live = order
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


async def check_use(client, name: str) -> None:
    """Try one specific model with this key (e.g. the newer gemini-3.8-live); switch to it only if it answers."""
    cfg = load_local()
    current = (cfg.get("voice") or {}).get("model") or PREFERRED[0]
    print(f"Trying {name} with your key (now using {current})…")
    res = await timed(probe(client, name), 25)
    if res != "OK":
        print(f"❌ {name}: {res}\nStaying on {current}." + (
            " (quota = your free tier doesn't include it yet, or today's limit is used up.)" if "quota" in res.lower()
            or "429" in res else ""))
        return
    cfg.setdefault("voice", {})["model"] = name
    save_local(cfg)
    print(f"✅ {name} answered → saved. Now run `run-pakito.bat check full` to test Pakito's tools on it.\n"
          f"To go back: run-pakito.bat check use {current}")


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
        if keep.get("long"):
            c["context_window_compression"] = {"sliding_window": {}}
            c["session_resumption"] = {}
        return c

    steps = [
        ("bare model", {}),
        ("+ system prompt", {"prompt": 1}),
        ("+ transcripts", {"prompt": 1, "transcripts": 1}),
        ("+ Pakito's tools", {"prompt": 1, "transcripts": 1, "functions": 1}),
        ("+ Google Search", {"prompt": 1, "transcripts": 1, "functions": 1, "search": 1}),
        ("Google Search alone", {"search": 1}),
        ("+ long sessions (reconnect, no 10-min cutoff)", {"prompt": 1, "transcripts": 1, "functions": 1, "long": 1}),
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
    long_ok = results["+ long sessions (reconnect, no 10-min cutoff)"] == "OK"
    if rest_ok:
        local.setdefault("voice", {})["long_sessions"] = long_ok
        save_local(local)
        print("→ Long sessions work: dropped connections reconnect." if long_ok else
              "→ Your model doesn't take long-session support: turned it off (conversations end at ~10 min).")
    if any("quota" in str(results[k]).lower() for k in ("+ Google Search", "Google Search alone")):
        print("  (Search failed on quota, not a block: the free tier's search allowance ran out for now. Run this "
              "again tomorrow to turn it back on.)")


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
    import os, shutil, subprocess
    print(f"Pakito uses: {exe}")
    try:
        v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        print(f"  version: {v}")
    except Exception as e:
        print(f"  version: couldn't run it ({type(e).__name__})")
    others = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for n in ("claude.exe", "claude.cmd", "claude"):
            f = os.path.join(d, n)
            if os.path.isfile(f) and os.path.normcase(f) != os.path.normcase(exe):
                others.add(f)
    if others:
        print("  other Claude installs on PATH (your terminal may use one of these instead):")
        for f in sorted(others):
            print(f"    {f}")
    for k in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(k):
            print(f"  note: {k} is set in this environment")
    print("Asking a test question (first run can take ~20 s)…")
    out = {}
    b = ClaudeBridge(cfg["claude"], data_dir(cfg), notify=lambda t, m: None, threaded=False)
    b.listeners.append(lambda job: out.update(job) or True)
    t = time.time()
    b.ask("Reply with exactly: Pakito link works.")
    if out.get("status") == "done":
        print(f"✅ {out['spoken']}  ({time.time() - t:.0f} s)")
    else:
        print(f"❌ {out.get('answer')}\n   If it mentions login: open a terminal, run `claude`, log in, /exit, try again.")


SPEED_MODELS = ["gemini-3.8-live", "gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-latest"]


async def time_turn(session, send) -> tuple[float, object]:
    """Time from sending to the first audio chunk. Returns (seconds, tool_call or None)."""
    import time as _t
    t0 = _t.perf_counter()
    await send()
    async for msg in session.receive():
        if msg.tool_call and msg.tool_call.function_calls:
            return _t.perf_counter() - t0, msg.tool_call
        sc = msg.server_content
        if sc and sc.model_turn and any(getattr(p, "inline_data", None) for p in sc.model_turn.parts or []):
            dt = _t.perf_counter() - t0
            async for m2 in session.receive():          # drain the rest of the turn
                if m2.server_content and m2.server_content.turn_complete:
                    break
            return dt, None
        if sc and sc.turn_complete:
            break
    return _t.perf_counter() - t0, None


async def speed_one(client, model: str, base_cfg: dict) -> dict:
    import time as _t
    from google.genai import types
    cfg = {k: v for k, v in base_cfg.items() if k != "realtime_input_config"}   # text turns for the test
    out = {}
    t0 = _t.perf_counter()
    async with client.aio.live.connect(model=model, config=cfg) as s:
        out["connect"] = _t.perf_counter() - t0
        plain = []
        for q in ("¿Por qué el cielo es azul? Responde en una frase.", "Dime un dato curioso sobre Marte, corto."):
            dt, _ = await time_turn(s, lambda q=q: s.send_realtime_input(text=q))
            plain.append(dt)
        out["answer"] = sum(plain) / len(plain)
        dt1, tc = await time_turn(s, lambda: s.send_realtime_input(text="Usa la herramienta lookup: ¿qué clima hace hoy en Tempe?"))
        if tc:
            fc = tc.function_calls[0]
            resp = [types.FunctionResponse(id=fc.id, name=fc.name,
                                           response={"results": [{"snippet": "Tempe today: sunny, 31 C, light wind."}]})]
            dt2, _ = await time_turn(s, lambda: s.send_tool_response(function_responses=resp))
            out["tool_decide"], out["after_tool"] = dt1, dt2
    return out


async def check_speed(client) -> None:
    from .config import load_config
    from .hub import Hub
    from .voice import VOICE_DEFAULTS, live_config
    cfg = load_config()
    vcfg = {**VOICE_DEFAULTS, **cfg.get("voice", {})}
    hub = Hub(cfg, launcher=lambda x: None, notifier=lambda *a: None)
    base = live_config(hub, vcfg)
    print("Timing each model with Pakito's real prompt + tools (seconds until it starts talking):\n")
    print(f"  {'model':40} {'connect':>8} {'answer':>8} {'tool call':>10} {'after tool':>11}")
    results = {}
    for m in SPEED_MODELS:
        try:
            r = await asyncio.wait_for(speed_one(client, m, base), timeout=120)
            results[m] = r
            f = lambda k: f"{r[k]:.1f}" if k in r else "-"
            print(f"  {m:40} {f('connect'):>8} {f('answer'):>8} {f('tool_decide'):>10} {f('after_tool'):>11}")
        except Exception as e:
            print(f"  {m:40} ❌ {str(e)[:70]}")
    ok = {m: r for m, r in results.items() if "answer" in r}
    if not ok:
        return
    score = lambda r: r["answer"] + r.get("tool_decide", 5) + r.get("after_tool", 5)
    best = min(ok, key=lambda m: score(ok[m]))
    local = load_local()
    local.setdefault("voice", {})["model"] = best
    save_local(local)
    print(f"\n→ Fastest overall: {best}. Saved it as your model. Restart Pakito.")


async def check_look(client) -> None:
    import io
    import random
    from PIL import Image, ImageDraw, ImageFont
    from google.genai import types
    from .config import load_config
    from .voice import VOICE_DEFAULTS
    vcfg = {**VOICE_DEFAULTS, **load_config().get("voice", {})}
    secret = str(random.randint(1000, 9999))
    img = Image.new("RGB", (900, 400), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 160)
    except OSError:
        font = ImageFont.load_default()
    d.text((120, 100), secret, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    cfg = {"response_modalities": ["AUDIO"], "output_audio_transcription": {}}
    print(f"Sending {vcfg['model']} a picture with a number on it…")
    heard = ""
    try:
        async with client.aio.live.connect(model=vcfg["model"], config=cfg) as s:
            await s.send_realtime_input(video=types.Blob(data=buf.getvalue(), mime_type="image/jpeg"))
            await asyncio.sleep(0.6)
            await s.send_realtime_input(text="What number is written in the picture I just sent? Say only the number.")
            async for msg in s.receive():
                sc = msg.server_content
                if sc and sc.output_transcription and sc.output_transcription.text:
                    heard += sc.output_transcription.text
                if sc and sc.turn_complete:
                    break
    except Exception as e:
        print(f"❌ {type(e).__name__}: {str(e)[:200]}")
        return
    digits = "".join(ch for ch in heard if ch.isdigit())
    if secret in digits:
        print(f"✅ It read {secret}. 'Mira esto' will work.")
    else:
        print(f"❌ The picture said {secret}, it answered: {heard.strip()!r}. Paste this to Claude.")


async def check_dictation(client) -> None:
    import math
    import random
    import struct
    import time as _t
    from .dictation import Transcriber, to_wav
    # 1.5 s of noise + a hum: no words. A good transcriber must return nothing here (no invented sentences).
    rnd = random.Random(1)
    frames = [struct.pack("<h", int(3000 * math.sin(2 * math.pi * 120 * i / 16000) + rnd.randint(-2500, 2500)))
              for i in range(24000)]
    wav = to_wav([b"".join(frames)])
    tr = Transcriber(client, load_local().get("voice", {}).get("dictation_model"))
    try:
        await tr(wav, "Notepad")                           # first call picks the model (slower, one time)
        times, outs = [], []
        for _ in range(2):
            t0 = _t.perf_counter()
            outs.append(await tr(wav, "Notepad"))
            times.append(_t.perf_counter() - t0)
    except Exception as e:
        print(f"❌ {str(e)[:200]}")
        return
    speed = f"{min(times):.1f} s per clip once warm"
    if any(outs):
        print(f"⚠ {tr.model} invented text from noise: {outs[0][:80]!r}. Pakito's own silence check stops most of "
              f"these (it never sends a clip with no voice), but tell Claude. ({speed})")
    else:
        print(f"✅ {tr.model}: returned nothing for noise, as it should. {speed}.")
    local = load_local()
    local.setdefault("voice", {})["dictation_model"] = tr.model
    save_local(local)
    print("Saved it. Restart Pakito, click into any text box, hold Ctrl+Win and talk.")


async def check_notes(client, seconds: int = 30) -> None:
    """run-pakito.bat check notes: record a long-ish note, transcribe it the way take_note now does. Nothing is
    saved to the inbox."""
    import time as _t
    from .dictation import Recorder, Transcriber
    from .voice import MIC
    from .voice_notes import speech_seconds_pcm, split_pcm
    from .config import load_config
    from .dictation import to_wav
    cfg = load_config()
    MIC["name"] = (cfg.get("voice") or {}).get("mic")
    print(f"Talk for up to {seconds} s, like a real note (Enter stops early)…")
    rec = Recorder()
    rec.start()
    try:
        await asyncio.wait_for(asyncio.to_thread(input), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    wav, secs = rec.stop()
    pcm = wav[44:]
    print(f"  recorded {secs:.0f} s, {speech_seconds_pcm(pcm):.0f} s of voice, {len(split_pcm(pcm, piece_sec=12))} "
          "piece(s) at the test's 12 s size")
    tr = Transcriber(client, (cfg.get("voice") or {}).get("dictation_model"), (cfg.get("voice") or {}).get("vocabulary"))
    from .voice_notes import NOTE_PROMPT
    text_parts, t1 = [], _t.time()
    try:
        for piece in split_pcm(pcm, piece_sec=12):         # small pieces here so a short test still exercises it
            if speech_seconds_pcm(piece) < 0.3:
                continue
            text_parts.append(await tr(to_wav([piece]), prompt=NOTE_PROMPT.format(vocab=", ".join(tr.vocab))))
    except Exception as e:
        print(f"❌ {str(e)[:200]}")
        return
    from .voice_notes import join_pieces
    text = join_pieces(text_parts)
    print(f"\n✅ {tr.model}, {_t.time() - t1:.1f} s:\n\n{text or '(nothing heard)'}\n")
    print("That's exactly what a voice note would save (nothing went to your inbox).")


def check_hands(seconds: int = 12) -> None:
    """Hand control step by step, with timings: model, each camera + driver, then a live preview (no mouse moves)."""
    import os
    import time as _t
    os.environ.setdefault("GLOG_minloglevel", "2")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    from .config import load_config
    from .gestures import HAND_DEFAULTS, INSTALL, make_marker, open_camera, pose
    hc = {**HAND_DEFAULTS, **(load_config().get("hands") or {})}

    def step(label, fn):
        t = _t.monotonic()
        try:
            out = fn()
            print(f"  ✅ {label} ({_t.monotonic() - t:.1f} s)")
            return out
        except Exception as e:
            print(f"  ❌ {label} ({_t.monotonic() - t:.1f} s): {type(e).__name__}: {e}")
            return None

    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
    except ImportError:
        print(INSTALL)
        return
    print(f"MediaPipe {mp.__version__}, OpenCV {cv2.__version__}")
    from .gestures import model_path, quiet_stderr
    if not (Path.home() / ".pakito" / "models" / "hand_landmarker.task").exists():
        step("downloading the hand model (~8 MB, once)", model_path)

    def load():
        with quiet_stderr():
            return make_marker(vision, BaseOptions)
    marker = step("hand model", load)
    if marker is None:
        return
    print("Cameras:")
    found = []
    for idx in range(3):
        cap, how = open_camera(cv2, idx, hc.get("backend"), log=lambda m: print("  " + m))
        if cap is not None:
            found.append((idx, how))
            cap.release()
    if not found:
        print("❌ No camera gave a picture. Is another app (Zoom, Teams, OBS, the Camera app) using it?")
        return
    idx, how = next(((i, h) for i, h in found if i == int(hc["camera"])), found[0])
    print(f"\nUsing camera {idx} via {how}. Live preview for {seconds} s: show your hand, point, pinch "
          f"(the mouse won't move). Press Q to stop early.")
    cap, how = open_camera(cv2, idx, how)
    t0, frames, seen, gestures = _t.monotonic(), 0, 0, {}
    while _t.monotonic() - t0 < seconds:
        ok, frame = cap.read()
        if not ok:
            continue
        frames += 1
        frame = cv2.flip(frame, 1)
        res = marker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB,
                                               data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)),
                                      int((_t.monotonic() - t0) * 1000))
        label = "no hand"
        if res.hand_landmarks:
            seen += 1
            p = pose([(q.x, q.y) for q in res.hand_landmarks[0]])
            label = "pinch" if p["pinch"] < 0.30 else p["gesture"]
            gestures[label] = gestures.get(label, 0) + 1
            h, w = frame.shape[:2]
            for q in res.hand_landmarks[0]:
                cv2.circle(frame, (int(q.x * w), int(q.y * h)), 3, (80, 220, 120), -1)
        cv2.putText(frame, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        cv2.imshow("Pakito hands check", frame)
        if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q")):
            break
    cap.release()
    cv2.destroyAllWindows()
    secs = max(_t.monotonic() - t0, 0.1)
    print(f"\n{frames / secs:.0f} frames/s · hand seen in {100 * seen // max(frames, 1)}% · gestures: "
          + (", ".join(f"{k} {v}" for k, v in sorted(gestures.items(), key=lambda kv: -kv[1])) or "none"))
    if (idx, how) != (int(hc["camera"]), hc.get("backend")):
        print(f'Tip: save it so Pakito opens it right away: "hands": {{"camera": {idx}, "backend": "{how}"}} '
              "in config.local.json")


def check_wake(seconds: int = 20) -> None:
    """Listen for the wake word for a while and show how sure it is, second by second (to tune the threshold)."""
    from .config import load_config
    from .voice import MIC
    from .wake import FRAME, INSTALL, WAKE_DEFAULTS, load_model, open_mic
    cfg = load_config()
    MIC["name"] = (cfg.get("voice") or {}).get("mic")
    w = {**WAKE_DEFAULTS, **((cfg.get("voice") or {}).get("wake") or {})}
    try:
        import numpy as np
        model, name = load_model(w["word"])
    except ImportError:
        print(INSTALL)
        return
    stream = open_mic()
    print(f"Say '{name.replace('_', ' ')}' a few times, and talk normally in between ({seconds} s). "
          f"Threshold now: {w['threshold']}.\n")
    best_all, hits = 0.0, 0
    for sec in range(seconds):
        best = 0.0
        for _ in range(12):                                # 12 x 80 ms ≈ 1 s
            frame = stream.read(FRAME, exception_on_overflow=False)
            best = max(best, float(model.predict(np.frombuffer(frame, dtype=np.int16)).get(name, 0.0)))
        best_all = max(best_all, best)
        mark = "  ← would wake" if best >= w["threshold"] else ""
        hits += bool(mark)
        print(f"  {sec + 1:2d}s  {'█' * int(best * 30):30s} {best:.2f}{mark}")
    stream.stop_stream()
    stream.close()
    print(f"\nHighest: {best_all:.2f}. Seconds that would wake it: {hits}.")
    if best_all < w["threshold"]:
        print(f"It never reached {w['threshold']}: try closer to the mic, or lower it, e.g. "
              f'"voice": {{"wake": {{"threshold": {max(0.2, round(best_all * 0.8, 2))}}}}} in config.local.json.')
    else:
        print("If it also woke while you were just talking, raise the threshold a little.")


async def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "claude":
        check_claude()
        return
    from .config import gemini_key, load_config
    if not gemini_key(load_config()):
        print("❌ No Gemini key. Run: run-pakito.bat setup")
        return
    client = genai.Client()
    if len(sys.argv) > 1 and sys.argv[1] == "dictation":
        await check_dictation(client)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "wake":
        check_wake()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "hands":
        check_hands()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "tablet":
        from .config import load_config
        from .tablet import Tablet
        st = Tablet(load_config().get("tablet")).status()
        print("Tablet OK: " + ", ".join(st["devices"]) + ". Try: 'Pakito, mira mi tablet'." if st["ok"]
              else "Tablet not ready: " + st["error"])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "notes":
        await check_notes(client)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "look":
        await check_look(client)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "speed":
        await check_speed(client)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "use":
        await check_use(client, sys.argv[2] if len(sys.argv) > 2 else "gemini-3.8-live")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "full":
        await check_full(client)
    else:
        await check_models(client)


if __name__ == "__main__":
    asyncio.run(main())
