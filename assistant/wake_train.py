"""Train "Pakito" as the wake word, on your own voice, on this computer (~5 minutes of recording, <1 min training).

  run-pakito.bat train          record a session, train, switch Pakito to it
  run-pakito.bat train more     record more (adds to what's saved), retrain: fixes misses / false wake-ups
  run-pakito.bat train only     retrain from what's saved, no recording

How it works: openWakeWord turns every 80 ms of audio into a 96-number "speech embedding" (a model Google trained on
lots of speech). A wake word model is just a small network that looks at the last 1.28 s of those (16 x 96) and
says "that was the word" or not. Instead of the official recipe (thousands of synthetic voices, an hour on a GPU),
this trains that small network on your own recordings: you saying "Pakito" (positives), and you talking normally,
similar words and room noise (negatives). It's tuned to your voice and your room, which is what Pakito needs.
Recordings stay in ~/.pakito/wake_data (more sessions = better); the model is ~/.pakito/models/pakito.npz.
"""
from __future__ import annotations

import os
import sys
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

RATE = 16000
WIN = 16                                   # embedding frames per window (16 x 80 ms = 1.28 s)
NAME = "pakito"
HOME = Path.home() / ".pakito"
DATA = HOME / "wake_data"
MODEL = HOME / "models" / "pakito.npz"
HARD_NEGATIVES = ["Paquita", "poquito", "bonito", "palito", "Pakistán", "taquito", "Paco", "Pepito", "a quitar",
                  "Patricio", "chiquito", "Jarvis"]
HARD_SENTENCES = ["Necesito un poquito de café.", "Patricio dijo que estuvo bonito.", "Paco vive en Pakistán.",
                  "Me comí unos taquitos ayer.", "Se cayó el palito.", "Paquita es mi prima.", "Qué chiquito está.",
                  "Hay que quitar eso de ahí.", "Put it in my pocket.", "Pass the ticket, please.",
                  "Hey Jarvis, pon música.", "Pepito llegó tarde otra vez."]
FRAMES = 3                                  # windows in a row above the threshold to wake (personal models)


def frame_end(k: int) -> float:
    """When the audio seen by embedding frame k ends (s): 76 mel frames of 10 ms, stepped by 8."""
    return 0.08 * k + 0.76


# ---------------------------------------------------------------- audio helpers
def speech_bounds(x: np.ndarray, rate: int = RATE) -> tuple[float, float] | None:
    """Where the spoken word is in a short clip (s), from loudness. None if nothing word-like."""
    hop = rate // 100
    n = len(x) // hop
    if n < 10:
        return None
    rms = np.sqrt(np.mean(x[:n * hop].astype(np.float64).reshape(n, hop) ** 2, axis=1))
    floor = np.percentile(rms, 20)
    on = rms > max(floor * 4, 250)
    idx = np.flatnonzero(on)
    if len(idx) < 8:
        return None
    start, end = idx[0] / 100, (idx[-1] + 1) / 100
    return (start, end) if 0.2 <= end - start <= 2.0 else None


def save_wav(path: Path, x: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(x.astype(np.int16).tobytes())


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def mix(x: np.ndarray, noise: np.ndarray, snr_db: float, rng) -> np.ndarray:
    if len(noise) < len(x):
        noise = np.tile(noise, len(x) // max(len(noise), 1) + 1)
    i = rng.integers(0, len(noise) - len(x) + 1)
    n = noise[i:i + len(x)].astype(np.float64)
    ps, pn = np.mean(x.astype(np.float64) ** 2) + 1e-9, np.mean(n ** 2) + 1e-9
    y = x + n * np.sqrt(ps / (pn * 10 ** (snr_db / 10)))
    return np.clip(y, -32768, 32767).astype(np.int16)


def variants(x: np.ndarray, noise: np.ndarray | None, rng) -> list[np.ndarray]:
    """The clip as recorded, quieter, louder, and over the room's noise."""
    out = [x, np.clip(x * 0.5, -32768, 32767).astype(np.int16), np.clip(x * 1.6, -32768, 32767).astype(np.int16)]
    if noise is not None and len(noise) > RATE // 2:
        out += [mix(x, noise, snr, rng) for snr in (20, 10, 5)]
    return out


# ---------------------------------------------------------------- features
class Embedder:
    def __init__(self):
        from openwakeword.utils import AudioFeatures
        self.af = AudioFeatures(inference_framework="onnx")

    def __call__(self, x: np.ndarray) -> np.ndarray:
        """(frames, 96) for a clip of int16 audio."""
        if len(x) < int(0.8 * RATE):
            x = np.concatenate([np.zeros(int(0.8 * RATE) - len(x), np.int16), x])
        e = np.asarray(self.af._get_embeddings(x.astype(np.int16)))
        return e.reshape(e.shape[0], -1).astype(np.float32)


def windows(emb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """All 16-frame windows (flattened) and when each one ends (s)."""
    n = emb.shape[0] - WIN + 1
    if n <= 0:
        return np.zeros((0, WIN * emb.shape[1]), np.float32), np.zeros(0)
    X = np.stack([emb[i:i + WIN].reshape(-1) for i in range(n)])
    ends = np.array([frame_end(i + WIN - 1) for i in range(n)])
    return X, ends


def positive_windows(emb: np.ndarray, start: float, end: float, pad: float) -> tuple[np.ndarray, np.ndarray]:
    """From a clip with the word in it: windows that end just after the word (positive) and windows that end
    before it starts (negative). The ones in between (half a word) are left out. `pad` = silence added in front."""
    X, ends = windows(emb)
    ends = ends - pad
    pos = (ends >= end) & (ends <= end + 0.32)
    neg = ends < start + 0.1
    return X[pos], X[neg]


# ---------------------------------------------------------------- the model
def train(Xp: np.ndarray, Xn: np.ndarray, seed: int = 0) -> dict:
    """A small network (1536 -> 32 -> 1), as plain numbers so Pakito doesn't need sklearn at run time."""
    from sklearn.neural_network import MLPClassifier
    rng = np.random.default_rng(seed)
    reps = max(1, len(Xn) // max(len(Xp), 1))
    X = np.vstack([np.repeat(Xp, reps, axis=0), Xn])
    y = np.concatenate([np.ones(len(Xp) * reps), np.zeros(len(Xn))])
    order = rng.permutation(len(X))
    X, y = X[order], y[order]
    mean, scale = X.mean(0), X.std(0) + 1e-6
    clf = MLPClassifier(hidden_layer_sizes=(32,), alpha=1e-2, max_iter=300, random_state=seed,
                        early_stopping=True, validation_fraction=0.1, n_iter_no_change=15)
    clf.fit((X - mean) / scale, y)
    return {"mean": mean.astype(np.float32), "scale": scale.astype(np.float32),
            "W1": clf.coefs_[0].astype(np.float32), "b1": clf.intercepts_[0].astype(np.float32),
            "W2": clf.coefs_[1].astype(np.float32), "b2": clf.intercepts_[1].astype(np.float32)}


def score(m: dict, X: np.ndarray) -> np.ndarray:
    h = np.maximum(((X - m["mean"]) / m["scale"]) @ m["W1"] + m["b1"], 0)
    z = (h @ m["W2"] + m["b2"]).reshape(-1)
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def wakes(scores: np.ndarray, thr: float, frames: int = FRAMES, refractory: int = 25) -> int:
    """How many times Pakito would wake on this score stream (frames in a row, then ~2 s of ignoring)."""
    n, run, last = 0, 0, -10 ** 9
    for i, sc in enumerate(scores):
        run = run + 1 if sc >= thr else 0
        if run >= frames and i - last > refractory:
            n, last, run = n + 1, i, 0
    return n


def pick_threshold(pos_clips: list[np.ndarray], neg_runs: list[np.ndarray], frames: int = FRAMES):
    """The strictest threshold that still catches >= 90% of the held-out 'Pakito's with no false wake-ups on the
    held-out talk (Nov 23 test: 0.85 x 3 frames caught 10/10 with 0 false, where 0.35 woke 8 times on talk).
    -> (threshold, share caught, false wake-ups)."""
    def stats(t):
        caught = float(np.mean([wakes(c, t, frames) > 0 for c in pos_clips])) if pos_clips else 0.0
        return caught, sum(wakes(r, t, frames) for r in neg_runs)
    for need in (0.9, 0.75, 0.5):
        for t in (0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.6, 0.5):
            caught, false = stats(t)
            if caught >= need and false == 0:
                return t, caught, false
    caught, false = stats(0.7)
    return 0.7, caught, false


class PersonalModel:
    """Runs a trained pakito.npz the way openWakeWord's Model runs its models: predict(80 ms) -> {name: score}."""

    def __init__(self, path: str | Path):
        from openwakeword.utils import AudioFeatures
        d = np.load(str(path))
        self.m = {k: d[k] for k in ("mean", "scale", "W1", "b1", "W2", "b2")}
        self.threshold = float(d["threshold"]) if "threshold" in d else 0.5
        self.name = Path(path).stem
        self.af = AudioFeatures(inference_framework="onnx")

    def predict(self, x: np.ndarray) -> dict:
        self.af(x.astype(np.int16))
        f = self.af.get_features(WIN).reshape(1, -1)
        return {self.name: float(score(self.m, f)[0])}

    def reset(self) -> None:
        self.af.reset()


# ---------------------------------------------------------------- building the data set
def in_context(x: np.ndarray, bounds: tuple[float, float], talk: np.ndarray | None, rng, n: int = 4):
    """The word cut out of its clip and put between bits of his own talking ("…oye, Pakito, pon música"):
    he won't always say it after a silence. -> [(clip, word_start, word_end)]"""
    s0, s1 = int(max(bounds[0] - 0.05, 0) * RATE), int(min(bounds[1] + 0.05, len(x) / RATE) * RATE)
    word = x[s0:s1]
    sil = lambda sec: (rng.normal(0, 60, int(sec * RATE))).astype(np.int16)

    def chunk(sec):
        if talk is None or len(talk) < int(sec * RATE) + 1:
            return np.zeros(0, np.int16)
        i = rng.integers(0, len(talk) - int(sec * RATE))
        return talk[i:i + int(sec * RATE)]
    out = []
    for k in range(n):
        before = chunk(rng.uniform(0.3, 1.0)) if k in (0, 2, 3) else np.zeros(0, np.int16)
        after = chunk(rng.uniform(0.3, 0.8)) if k in (1, 2, 3) else np.zeros(0, np.int16)
        gap_b, gap_a = sil(rng.uniform(0.05, 0.4)), sil(rng.uniform(0.0, 0.15))
        lead = sil(1.4)              # a window needs ~2 s of audio before it exists: don't let the word fall before
        start = (len(lead) + len(before) + len(gap_b)) / RATE + 0.05
        clip = np.concatenate([lead, before, gap_b, word, gap_a, after, sil(0.5)])
        out.append((clip, start, start + len(word) / RATE - 0.1))
    return out


def dataset(emb: Embedder, folder: Path, rng, holdout: float = 0.2):
    """Everything recorded so far -> train / held-out windows."""
    pos_files = sorted(folder.rglob("pos_*.wav"))
    neg_files = sorted(folder.rglob("neg_*.wav")) + sorted(folder.rglob("hard_*.wav"))
    noise_files = sorted(folder.rglob("noise_*.wav"))
    noise = np.concatenate([load_wav(f) for f in noise_files]) if noise_files else None
    talk_files = sorted(folder.rglob("neg_talk*.wav"))
    talk = np.concatenate([load_wav(f) for f in talk_files]) if talk_files else None
    pad = 0.6
    pads = np.zeros(int(pad * RATE), np.int16)
    n_hold = max(1, int(len(pos_files) * holdout)) if len(pos_files) >= 5 else 0
    step = max(1, len(pos_files) // max(n_hold, 1))
    Xp, Xn, hold, Xn_hold = [], [], [], []
    for i, f in enumerate(pos_files):
        x = load_wav(f)
        b = speech_bounds(x)
        if b is None:
            continue
        held = i % step == 0 and len(hold) < n_hold
        if held:                                      # judged on: alone, and in the middle of talking
            hold.append(windows(emb(np.concatenate([pads, x, np.zeros(RATE // 2, np.int16)])))[0])
            for clip, _, _ in in_context(x, b, talk, rng, n=2):
                hold.append(windows(emb(clip))[0])
            continue
        for v in variants(x, noise, rng):
            p, n = positive_windows(emb(np.concatenate([pads, v])), b[0], b[1], pad)
            Xp.append(p)
            Xn.append(n)
        for clip, s0, s1 in in_context(x, b, talk, rng):
            for v in ([clip, mix(clip, noise, 10, rng)] if noise is not None else [clip]):
                p, n = positive_windows(emb(v), s0, s1, 0.0)
                Xp.append(p)
                Xn.append(n)
    for f in neg_files + noise_files:
        x = load_wav(f)
        X, _ = windows(emb(x))
        if f.name.startswith("neg_") and len(X) > 50:      # hold out the end of each talking recording
            cut = int(len(X) * (1 - holdout))
            Xn.append(X[:cut])
            Xn_hold.append(X[cut:])
        elif f.name.startswith("hard_s") and int(f.stem.split("_")[-1]) % 4 == 3:
            Xn_hold.append(X)                              # a few tricky sentences only for the test
        else:
            Xn.append(X)
            if f.name.startswith("neg_"):
                Xn.append(windows(emb(np.clip(x * 0.5, -32768, 32767).astype(np.int16)))[0])
    cat = lambda parts, d: np.vstack([p for p in parts if len(p)]) if any(len(p) for p in parts) else \
        np.zeros((0, d), np.float32)
    D = WIN * 96
    return (cat(Xp, D), cat(Xn, D), [p for p in hold if len(p)], [r for r in Xn_hold if len(r)],
            {"positives": len(pos_files), "negatives": len(neg_files), "noise": len(noise_files)})


def fit(folder: Path = DATA, out: Path = MODEL, say: Callable[[str], None] = print, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    t = time.monotonic()
    emb = Embedder()
    Xp, Xn, hold_pos, Xn_hold, counts = dataset(emb, folder, rng)
    if len(Xp) < 20 or len(Xn) < 200:
        return {"error": f"Not enough recordings yet ({counts}). Record a session first: run-pakito.bat train"}
    m = train(Xp, Xn, seed)
    pos_runs = [score(m, c) for c in hold_pos]
    neg_runs = [score(m, r) for r in Xn_hold]
    thr, caught, false = pick_threshold(pos_runs, neg_runs)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez(str(out), threshold=np.float32(thr), frames=np.int32(FRAMES), **m)
    talk_sec = round(sum(len(r) for r in neg_runs) * 0.08)
    r = {"model": str(out), "threshold": thr, "frames": FRAMES,
         "held_out_caught": f"{round(caught * len(pos_runs))}/{len(pos_runs)}", "false_wakes": false,
         "talk_seconds_checked": talk_sec, "seconds": round(time.monotonic() - t, 1), **counts}
    say(f"\nTrained in {r['seconds']} s on {counts['positives']} 'Pakito's, {counts['negatives']} talking/"
        f"similar-word recordings and {counts['noise']} of room noise.")
    say(f"Test on recordings it didn't train on: caught {r['held_out_caught']} 'Pakito's (alone and mid-sentence), "
        f"{false} false wake-ups in ~{talk_sec} s of your talking. Threshold {thr}.")
    return r


# ---------------------------------------------------------------- recording session
def record(stream, seconds: float) -> np.ndarray:
    frames = int(seconds * RATE / 1280)
    return np.frombuffer(b"".join(stream.read(1280, exception_on_overflow=False) for _ in range(frames)),
                         dtype=np.int16)


def session(stream, say: Callable[[str], None] = print, ask: Callable[[str], str] = input, n_pos: int = 30,
            talk_sec: int = 90) -> Path:
    folder = DATA / datetime.now().strftime("%Y-%m-%d %H.%M")
    say("\n1/4  Room noise: stay quiet for 8 seconds…")
    time.sleep(0.5)
    save_wav(folder / "noise_room.wav", record(stream, 8))
    say(f"\n2/4  Say \"Pakito\" each time you see ●  ({n_pos} times, about 2 s each).")
    say("     Mix it up: normal, louder, softer, faster, slower, a bit farther from the mic, tired, excited.")
    ask("     Press Enter to start… ")
    got, tries = 0, 0
    while got < n_pos and tries < n_pos * 2:
        tries += 1
        sys.stdout.write(f"\r     ● Pakito  ({got + 1}/{n_pos})   ")
        sys.stdout.flush()
        x = record(stream, 2.2)
        if speech_bounds(x) is None:
            sys.stdout.write("\r     (didn't catch it, again)          \n")
            continue
        save_wav(folder / f"pos_{got:02d}.wav", x)
        got += 1
        time.sleep(0.25)
    say(f"\r     ✅ {got} recorded.                        ")
    say("\n3/4  Now words that sound close (so it learns they're NOT Pakito). Say each one when it shows.")
    ask("     Press Enter to start… ")
    for i, w in enumerate(HARD_NEGATIVES * 2):
        sys.stdout.write(f"\r     ● {w:<12} ({i + 1}/{len(HARD_NEGATIVES) * 2})   ")
        sys.stdout.flush()
        save_wav(folder / f"hard_{i:02d}.wav", record(stream, 2.2))
        time.sleep(0.2)
    say("\r     ✅ done.                                    ")
    say("\n     And these sentences, one at a time:")
    for i, sent in enumerate(HARD_SENTENCES):
        sys.stdout.write(f"\r     ● {sent:<36} ({i + 1}/{len(HARD_SENTENCES)})   ")
        sys.stdout.flush()
        save_wav(folder / f"hard_s_{i:02d}.wav", record(stream, 3.5))
        time.sleep(0.2)
    say("\r     ✅ done.                                                       ")
    say(f"\n4/4  Talk normally for {talk_sec} seconds, in Spanish and English: read something out loud, tell it about "
        "your day… Just don't say Pakito.")
    ask("     Press Enter to start… ")
    chunks = []
    for s in range(talk_sec // 5):
        sys.stdout.write(f"\r     🎙 {talk_sec - s * 5:3d} s left   ")
        sys.stdout.flush()
        chunks.append(record(stream, 5))
    save_wav(folder / "neg_talk.wav", np.concatenate(chunks))
    say("\r     ✅ done.              ")
    if ask("\n     Optional: play music or a video out loud for 30 s, so it learns to ignore that too? [y/N] "
           ).strip().lower().startswith(("y", "s")):
        say("     Playing… (30 s)")
        save_wav(folder / "neg_media.wav", record(stream, 30))
    return folder


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else "new"
    try:
        import openwakeword  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError:
        print("Training needs openWakeWord: run-pakito.bat pip install openwakeword")
        return
    os.environ.setdefault("ORT_LOGGING_LEVEL", "3")
    if mode != "only":
        from .config import load_config
        from .voice import MIC
        from .wake import open_mic
        MIC["name"] = (load_config().get("voice") or {}).get("mic")
        print("Training 'Pakito' on your voice. Sit where you usually talk to Pakito, at your normal distance.")
        if mode == "more":
            print("(Adding to your earlier recordings.)")
        stream = open_mic()
        try:
            session(stream)
        finally:
            stream.stop_stream()
            stream.close()
    print("\nTraining…")
    r = fit()
    if "error" in r:
        print(r["error"])
        return
    from .setup_wizard import save_local
    save_local({"voice": {"wake": {"word": str(MODEL), "threshold": r["threshold"], "frames": r["frames"],
                                   "enabled": True}}})
    print("\n✅ Pakito now wakes up to \"Pakito\" (restart it if it's running).")
    print("   Test it: run-pakito.bat check wake.  It misses you → run-pakito.bat train more (adds recordings).")
    print("   It wakes on its own → raise \"threshold\" a little in config.local.json, or train more with more talking.")


if __name__ == "__main__":
    main()
