"""Synthesize test tracks with known ground truth, to check the tagger and mixer.

  python make_test_track.py                 one track: 124 BPM, A minor (8A), 32 bars
  python make_test_track.py --library DIR   a 16-track library with varied tempo/key/energy

Structure of every track: 8-bar intro (pad + hats), main section (kick + bass +
pad + hats), 8-bar outro (pad + hats, fading).
"""
import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
PITCHES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Chord progressions as (root offset, quality) from the tonic.
PROGRESSIONS = {
    "minor": [(0, "min"), (8, "maj"), (5, "min"), (7, "maj")],   # i - VI - iv - V
    "major": [(0, "maj"), (7, "maj"), (9, "min"), (5, "maj")],   # I - V - vi - IV
}
TRIADS = {"maj": (0, 4, 7), "min": (0, 3, 7)}


def midi_hz(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def tone(freq, dur, harmonics=(1.0, 0.5, 0.25), attack=0.01, decay=None):
    t = np.arange(int(dur * SR)) / SR
    wave = sum(a * np.sin(2 * np.pi * freq * (k + 1) * t) for k, a in enumerate(harmonics))
    env = np.minimum(1, t / attack)
    if decay:
        env *= np.exp(-t / decay)
    return wave * env


def kick():
    t = np.arange(int(0.25 * SR)) / SR
    freq = 45 + 75 * np.exp(-t / 0.03)  # pitch sweep 120 -> 45 Hz
    return np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t / 0.12)


def hat(rng):
    n = int(0.05 * SR)
    noise = np.diff(rng.standard_normal(n + 1))  # crude highpass
    return noise * np.exp(-np.arange(n) / SR / 0.012)


def place(buf, clip, at_s, gain):
    i = int(at_s * SR)
    j = min(len(buf), i + len(clip))
    buf[i:j] += gain * clip[: j - i]


def make_track(path, bpm=124, tonic="A", mode="minor", energy=0.5, bars=32, seed=0):
    """`energy` 0..1 adds 16th hats, brighter chords and louder drums."""
    rng = np.random.default_rng(seed)
    beat = 60 / bpm
    root = 48 + PITCHES.index(tonic)  # tonic in octave 3 (MIDI 48 = C3)
    out = np.zeros(int((bars * 4 + 4) * beat * SR))
    k, h = kick(), hat(rng)
    harmonics = (1.0, 0.5, 0.25) + ((0.2, 0.15) if energy > 0.5 else ())
    hat_steps = 4 if energy > 0.6 else 2

    for bar in range(bars):
        bar_t = bar * 4 * beat
        main_section = 8 <= bar < bars - 8
        offset, quality = PROGRESSIONS[mode][bar % 4]
        chord_root = root + 12 + offset
        for n in TRIADS[quality]:
            place(out, tone(midi_hz(chord_root + n), 4 * beat, harmonics, attack=0.3), bar_t, 0.08)
        for b in range(4):
            t = bar_t + b * beat
            for s in range(1, hat_steps, 2 if hat_steps == 2 else 1):
                place(out, h, t + s * beat / hat_steps, 0.06 + 0.1 * energy)
            if main_section:
                place(out, k, t, 0.6 + 0.4 * energy)
                place(out, tone(midi_hz(root - 12 + offset), beat * 0.45, decay=0.15), t + beat / 2, 0.35)

    fade_start = int((bars - 4) * 4 * beat * SR)
    out[fade_start:] *= np.linspace(1, 0, len(out) - fade_start)
    out = 0.9 * out / np.abs(out).max()
    sf.write(path, np.column_stack([out, out]), SR)
    return {"bpm": bpm, "key": f"{tonic} {mode}", "intro_end_s": round(8 * 4 * beat, 2),
            "outro_start_s": round((bars - 8) * 4 * beat, 2)}


# A deliberately mixed library: a 120-128 house cluster that should blend, plus
# outliers (90, 100, 140, 174) that should be placed carefully or echo-out.
LIBRARY = [
    (120, "A", "minor", 0.3), (122, "E", "minor", 0.4), (123, "C", "major", 0.5),
    (124, "A", "minor", 0.6), (124, "D", "minor", 0.7), (125, "G", "major", 0.8),
    (126, "B", "minor", 0.9), (126, "F#", "minor", 0.5), (128, "D", "major", 0.7),
    (128, "A", "major", 0.85), (122, "F", "major", 0.35), (127, "C#", "minor", 0.95),
    (90, "G", "minor", 0.3), (100, "C", "minor", 0.45), (140, "E", "minor", 0.8),
    (174, "D", "minor", 0.9),
]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", type=Path)
    args = ap.parse_args()
    if not args.library:
        print(make_track("test_124bpm_Amin.wav"))
    else:
        args.library.mkdir(parents=True, exist_ok=True)
        truth = {}
        for i, (bpm, tonic, mode, energy) in enumerate(LIBRARY):
            name = f"Synth {i:02d} - {bpm}bpm {tonic}{'m' if mode == 'minor' else ''} e{int(energy * 100)}.mp3"
            truth[name] = make_track(args.library / name, bpm, tonic, mode, energy, bars=48, seed=i)
            print(name)
        (args.library / "ground_truth.json").write_text(json.dumps(truth, indent=2))
