"""Training 'Pakito' on his voice: the pieces, plus a tiny end-to-end run on synthetic sounds."""
import numpy as np
import pytest

from assistant import wake_train as wt

RATE = 16000


def tone(sec, f0, f1=None, amp=6000):
    t = np.arange(int(sec * RATE)) / RATE
    f = np.linspace(f0, f1 or f0, len(t))
    return (amp * np.sin(2 * np.pi * np.cumsum(f) / RATE)).astype(np.int16)


def test_speech_bounds_finds_the_word():
    x = np.concatenate([np.zeros(8000, np.int16), tone(0.6, 300, 900), np.zeros(16000, np.int16)])
    s, e = wt.speech_bounds(x)
    assert 0.45 <= s <= 0.55 and 1.05 <= e <= 1.15
    assert wt.speech_bounds(np.zeros(32000, np.int16)) is None


def test_window_timing_matches_openwakeword():
    assert wt.frame_end(0) == pytest.approx(0.76) and wt.frame_end(15) == pytest.approx(1.96)
    X, ends = wt.windows(np.zeros((20, 96), np.float32))
    assert X.shape == (5, 1536) and ends[0] == pytest.approx(1.96)


def test_wakes_needs_frames_in_a_row_and_ignores_repeats():
    s = np.array([0.9, 0.9, 0.1, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.0])
    assert wt.wakes(s, 0.85, 3) == 1 and wt.wakes(s, 0.85, 2) == 1 and wt.wakes(s, 0.95, 2) == 0


def test_threshold_is_the_strictest_that_still_catches_him():
    pos = [np.array([0.0, 0.97, 0.98, 0.99, 0.2])] * 9 + [np.array([0.0, 0.88, 0.9, 0.91, 0.0])]
    neg = [np.array([0.1, 0.8, 0.82, 0.81, 0.1])]              # tricky talk reaches 0.8 for 3 frames
    thr, caught, false = wt.pick_threshold(pos, neg)
    assert thr == 0.95 and caught == 0.9 and false == 0


def test_in_context_puts_the_word_between_his_talk():
    rng = np.random.default_rng(0)
    x = np.concatenate([np.zeros(8000, np.int16), tone(0.6, 300, 900), np.zeros(8000, np.int16)])
    talk = tone(5, 150, 250, 3000)
    for clip, s0, s1 in wt.in_context(x, (0.5, 1.1), talk, rng):
        assert s0 >= 1.4 and s1 - s0 == pytest.approx(0.6, abs=0.05) and len(clip) / RATE > s1 + 0.4


def test_train_and_score_shapes():
    rng = np.random.default_rng(0)
    pos = rng.normal(1.0, 0.3, (60, 1536)).astype(np.float32)
    neg = rng.normal(-1.0, 0.3, (300, 1536)).astype(np.float32)
    m = wt.train(pos, neg)
    assert wt.score(m, pos).mean() > 0.9 and wt.score(m, neg).mean() < 0.1


def test_end_to_end_on_synthetic_sounds(tmp_path):
    """A rising chirp plays 'Pakito'; other tones are talk. Records -> fit -> saved model -> streaming wake."""
    pytest.importorskip("openwakeword")
    pytest.importorskip("sklearn")
    rng = np.random.default_rng(3)
    data = tmp_path / "data" / "s1"
    noise = lambda sec: rng.normal(0, 80, int(sec * RATE)).astype(np.int16)
    wt.save_wav(data / "noise_room.wav", noise(8))
    for i in range(20):
        word = tone(rng.uniform(0.5, 0.7), 400, 1400, rng.uniform(3000, 9000))
        wt.save_wav(data / f"pos_{i:02d}.wav", np.concatenate([noise(rng.uniform(0.3, 0.8)), word, noise(1.0)]))
    talk = np.concatenate([np.concatenate([tone(rng.uniform(0.2, 0.6), f, f * rng.uniform(0.8, 1.2), 5000),
                                           noise(0.1)]) for f in rng.uniform(120, 2000, 200)])
    wt.save_wav(data / "neg_talk.wav", talk)
    for i in range(8):
        wt.save_wav(data / f"hard_{i:02d}.wav", np.concatenate([noise(0.5), tone(0.6, 1400, 400, 6000), noise(1)]))
    out = tmp_path / "pakito.npz"
    r = wt.fit(tmp_path / "data", out, say=lambda s: None)
    assert out.exists() and r["threshold"] >= 0.5 and r["frames"] == 3
    from assistant.wake import WakeWord, load_model
    model, name = load_model(str(out))
    assert name == "pakito"
    woke = []
    clock = [0.0]
    w = WakeWord({"word": str(out), "threshold": r["threshold"], "frames": 3}, lambda: woke.append(clock[0]),
                 clock=lambda: clock[0])
    w.model, w.name = model, name
    x = np.concatenate([noise(2), tone(0.6, 400, 1400, 6000), noise(2), tone(0.6, 1400, 400, 6000), noise(2)])
    for i in range(0, len(x) - 1280, 1280):
        clock[0] = i / RATE
        if w.step(x[i:i + 1280].tobytes()):
            w.release()
    assert len(woke) == 1 and 2.5 <= woke[0] <= 4.0           # the rising chirp, not the falling one
