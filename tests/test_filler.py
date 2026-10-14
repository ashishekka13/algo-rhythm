"""Runnable check for algo_rhythm.filler: length on the ramped grid, hits on the beats, peak, off by default.

    python -m tests.test_filler
"""
import os

import librosa
import numpy as np

from algo_rhythm import filler

SR = 44100
HOP = 32

assert os.environ.get("FILLER") == "1" or filler.ENABLED is False, "filler must be off by default"

for style in ("snare_roll", "dhol"):
    for bpm_from, bpm_to in ((100, 128), (128, 100), (90, 120)):
        x = filler.fill(bpm_from, bpm_to, 8, SR, style)
        g = filler.grid(bpm_from, filler.near_tempo(bpm_from, bpm_to), 8)
        assert len(x) == int(round(g[-1] * SR)) + int(filler.TAIL_S * SR), (style, len(x))
        assert np.isclose(60 / (g[1] - g[0]), bpm_from) and np.isclose(60 / (g[-1] - g[-2]), bpm_to)
        assert np.abs(x).max() <= 1.0
        pad = int(0.1 * SR)  # silence in front, so the hit on beat 0 is detectable too
        env = librosa.onset.onset_strength(y=np.pad(x.mean(axis=1), (pad, 0)), sr=SR, hop_length=HOP)
        on = librosa.frames_to_time(librosa.onset.onset_detect(onset_envelope=env, sr=SR, hop_length=HOP),
                                    sr=SR, hop_length=HOP) - pad / SR
        err = [1000 * np.min(np.abs(on - t)) for t in g]
        assert max(err) < 5, (style, bpm_from, bpm_to, np.round(err, 1))

loud = filler.fill(100, 128, 8, SR, "snare_roll", ref=np.full((SR, 2), 0.9))
assert np.abs(loud).max() <= 1.0  # level-matched to a very loud song: the soft limit holds the peak
print("filler ok")
