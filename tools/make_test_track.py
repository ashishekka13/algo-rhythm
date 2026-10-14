"""Synthesize test tracks with known ground truth, to check the tagger and mixer.

  python make_test_track.py                 one track: 124 BPM, A minor (8A), 32 bars
  python make_test_track.py --library DIR   a 16-track library with varied tempo/key/energy
  python -m tools.make_test_track --library DIR --vocals
                                            ...with a sung line in most tracks and its exact vocal stem

Structure of every track: 8-bar intro (pad + hats), main section (kick + bass +
pad + hats), 8-bar outro (pad + hats, fading). With --vocals, a voice sings 2-bar lines
(a breath of 1.5 beats between them) through the main section, except one instrumental
phrase in its middle, and LIBRARY/stems/<key>.vocals.mp3 holds exactly that voice: the
vocal swap and the lyric-safe cuts can be checked against the truth.
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


def voice_line(notes_hz, beat, sr=SR):
    """One sung line: a note per beat, gliding between notes, with vibrato and a vowel-ish spectrum."""
    n = int(len(notes_hz) * beat * sr)
    t = np.arange(n) / sr
    f = np.repeat(notes_hz, int(beat * sr))[:n]
    f = np.pad(f, (0, n - len(f)), mode="edge")
    f = np.convolve(f, np.ones(int(0.04 * sr)) / int(0.04 * sr), "same")  # portamento
    f[: int(0.04 * sr)] = notes_hz[0]
    f = f * (1 + 0.006 * np.sin(2 * np.pi * 5.5 * t))
    ph = 2 * np.pi * np.cumsum(f) / sr
    wave = sum(a * np.sin(k * ph) for k, a in ((1, 1.0), (2, 0.55), (3, 0.4), (4, 0.2), (5, 0.12)))
    env = np.minimum(1, t / 0.03) * np.minimum(1, (t[-1] - t) / 0.08)
    return wave * env


def make_track(path, bpm=124, tonic="A", mode="minor", energy=0.5, bars=32, seed=0, vocals=False):
    """`energy` 0..1 adds 16th hats, brighter chords and louder drums. vocals=True also sings and returns the voice
    (mono, same length) under "vocals"; the written file is music + voice."""
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

    voice = np.zeros_like(out)
    lines = []
    if vocals:
        scale = [0, 2, 3, 5, 7, 8, 10] if mode == "minor" else [0, 2, 4, 5, 7, 9, 11]
        gap_bar = 8 + (bars - 16) // 2  # one instrumental phrase (4 bars) in the middle of the main section
        for bar in range(8, bars - 8, 2):
            if gap_bar <= bar < gap_bar + 4:
                continue
            steps = rng.integers(0, len(scale), 7)
            notes = [midi_hz(root + 12 + scale[k]) for k in steps]
            line = voice_line(np.array(notes, float), beat)[: int(6.5 * beat * SR)]
            at = bar * 4 * beat
            place(voice, line, at, 0.22)
            lines.append([round(at, 3), round(at + len(line) / SR, 3)])

    fade_start = int((bars - 4) * 4 * beat * SR)
    ramp = np.linspace(1, 0, len(out) - fade_start)
    out[fade_start:] *= ramp
    voice[fade_start:] *= ramp
    peak = np.abs(out + voice).max()
    out, voice = 0.9 * out / peak, 0.9 * voice / peak
    mix = np.column_stack([out + voice, out + voice])
    if str(path).endswith(".mp3"):
        from algo_rhythm import media
        media.encode(mix, SR, path)
    else:
        sf.write(path, mix, SR)
    truth = {"bpm": bpm, "key": f"{tonic} {mode}", "intro_end_s": round(8 * 4 * beat, 2),
             "outro_start_s": round((bars - 8) * 4 * beat, 2)}
    if vocals:
        truth.update(vocals=np.column_stack([voice, voice]), sung_lines=lines)
    return truth


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



NO_VOICE = (13, 15)  # tracks left instrumental, so the no-stem paths are exercised too


def make_library(library, vocals=False):
    """The 16-track library (LIBRARY) in `library`; with vocals, stems/<key>.vocals.mp3 + <key>.json for the sung ones."""
    import hashlib
    from algo_rhythm import media, tagger
    library = Path(library)
    library.mkdir(parents=True, exist_ok=True)
    truth = {}
    for i, (bpm, tonic, mode, energy) in enumerate(LIBRARY):
        name = f"Synth {i:02d} - {bpm}bpm {tonic}{'m' if mode == 'minor' else ''} e{int(energy * 100)}.mp3"
        sing = vocals and i not in NO_VOICE
        t = make_track(library / name, bpm, tonic, mode, energy, bars=48, seed=i, vocals=sing)
        if sing:
            key = tagger.tag_key(name)
            media.encode(t.pop("vocals"), SR, library / media.stem_rel(key))
            digest = hashlib.sha256((library / name).read_bytes()).hexdigest()
            (library / "stems" / f"{key}.json").write_text(json.dumps({"sha256": digest, "model": "synthetic", "sr": SR}))
        truth[name] = t
        print(name)
    (library / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    write_enrich(library)
    return truth


def write_enrich(library):
    """Made-up Gemini song details (LIBRARY/enrich/<key>.json, as algo_rhythm.enrich writes them), from each track's
    energy, so the mood and energy paths run without calling Gemini."""
    from algo_rhythm import tagger
    (Path(library) / "enrich").mkdir(exist_ok=True)
    for i, (bpm, tonic, mode, energy) in enumerate(LIBRARY):
        name = f"Synth {i:02d} - {bpm}bpm {tonic}{'m' if mode == 'minor' else ''} e{int(energy * 100)}.mp3"
        level = min(4, 1 + int(energy * 4))
        party = energy >= 0.6
        answer = {"energy": level, "familiarity": 3, "occasion": ["club"] if party else ["late night"],
                  "lyric_mood": "energetic" if party else ("romantic" if mode == "major" else "melancholic"),
                  "confidence": 0.8, "style": "club_edm" if party else "ballad", "rhythm": "straight", "sound": "electronic",
                  "language": "english" if i % 5 == 4 else "hindi", "voice": "female", "year": 2016 + i % 8,
                  "moment": "peak" if energy >= 0.85 else "groove" if party else "warmup",
                  "title": f"Synth {i:02d}", "film": f"{tonic}{'m' if mode == 'minor' else ''} {bpm}", "singers": ["Test Voice"],
                  "model": "made-up"}
        (Path(library) / "enrich" / f"{tagger.tag_key(name)}.json").write_text(json.dumps(answer, indent=1))



if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", type=Path)
    ap.add_argument("--vocals", action="store_true", help="sing in most tracks and write their exact vocal stems")
    args = ap.parse_args()
    if not args.library:
        print(make_track("test_124bpm_Amin.wav"))
    else:
        make_library(args.library, args.vocals)
