"""Runnable check of the Bugs.md correctness fixes:

    python -m tests.test_bugs [LIBRARY]      (default test_library, see tests/test_recipe.py)

  5. one song without vibe data leaves every other song's vibe ranks alone (dj.finalize);
  6. Gemini's answers are matched by the row number they carry, never by position or a reworded name (enrich.match);
  7. a vocal stem that is only separation noise does not read as singing; a sung stem reads as before
     (tagger.vocal_frames_from_stem, and transition.sung_segments for the voice gates);
  1. a song window read on its own is the same audio as the window cut from the whole file.
"""
import copy
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf


def check_vibe(library):
    from algo_rhythm import dj
    songs, _ = dj.load_library(library)
    keys = ("valence_pct", "dance_pct", "acoustic_pct", "in_ease", "out_ease", "mood")
    before = [{k: s[k] for k in keys} for s in songs]
    again = copy.deepcopy(songs)
    dj.finalize(again)
    assert [{k: s[k] for k in keys} for s in again] == before, "with every song's vibe, nothing changes"
    gone = 3
    again[gone].pop("vibe")
    dj.finalize(again)
    others = [i for i in range(len(songs)) if i != gone]
    spread = {round(again[i]["valence_pct"], 3) for i in others}
    assert len(spread) == len(others), f"the other songs keep distinct valence ranks: {sorted(spread)}"
    assert again[gone]["valence_pct"] == 0.5 and again[gone]["in_ease"] == 0.5 and again[gone]["pm"] is None
    assert all(again[i]["in_ease"] == songs[i]["in_ease"] for i in others)
    print(f"5. one song without vibe: it gets neutral values, the other {len(others)} keep their ranks")


def check_match():
    from algo_rhythm import enrich
    rows = [{"name": "Kesariya - Brahmastra"}, {"name": "Kala Chashma - Baar Baar Dekho"},
            {"name": "Tum Hi Ho - Aashiqui 2"}, {"name": "Malhari - Bajirao Mastani"}]
    ans = lambda k, name, e: {"id": k, "name": name, "energy": e}
    # reordered, one name reworded, same length: each answer still lands on its own song
    got = enrich.match(rows, [ans(3, "Tum Hi Ho", 1), ans(1, "Kesariya", 2), ans(4, "Malhari - Bajirao Mastani", 4),
                              ans(2, "Kala Chashma", 3)])
    assert {k: v["energy"] for k, v in got.items()} == {"tum hi ho - aashiqui 2": 1, "kesariya - brahmastra": 2,
                                                       "malhari - bajirao mastani": 4, "kala chashma - baar baar dekho": 3}, got
    # dropped: no id, out of range, a bool, given twice, and a wrong id carrying another row's exact name
    got = enrich.match(rows, [{"name": "Kesariya - Brahmastra", "energy": 2}, ans(9, "x", 1), ans(True, "x", 1),
                              ans(2, "Kala Chashma", 3), ans(2, "Kala Chashma again", 3),
                              ans(3, "Malhari - Bajirao Mastani", 4)])
    assert got == {}, got
    print("6. answers matched by their row number; reordered and reworded replies land on the right songs, bad ones dropped")


def check_vocal_floor(library):
    from algo_rhythm import tagger
    tmp = Path(tempfile.mkdtemp())

    def old(path):  # the rule before the floor
        import librosa
        v, _ = librosa.load(path, sr=tagger.SR, mono=True)
        db = 20 * np.log10(librosa.feature.rms(y=v, frame_length=2048, hop_length=tagger.VOCAL_HOP)[0] + 1e-9)
        return np.clip((db - np.percentile(db, 90) + 24) / 16, 0, 1)
    new = lambda path: tagger.vocal_frames_from_stem(str(path)) / tagger.VOCAL_SCALE
    stems = sorted((library / "stems").glob("*.vocals.*"))[:4]
    assert stems, "the test library has vocal stems"
    for st in stems:
        a, b = old(str(st)), new(st)
        assert np.abs(a - b).max() < 1e-6, (st.name, np.abs(a - b).max())  # float32 rounding of the scale
    rng = np.random.default_rng(0)
    noise = rng.standard_normal((44100 * 30, 2)) * 10 ** (-62 / 20)  # Demucs residue on an instrumental: about -60 dBFS
    sf.write(tmp / "noise.wav", noise, 44100)
    was, now = float(np.mean(old(str(tmp / "noise.wav")))), float(np.mean(new(tmp / "noise.wav")))
    assert was > 0.5 and now < 0.01, (was, now)
    print(f"7. a sung stem reads exactly as before ({len(stems)} stems); separation noise read as {was:.0%} singing, now {now:.0%}")
    # the voice gates and the MC's room use transition.sung_segments: the same floor there
    from algo_rhythm import transition
    floor = transition.SUNG_FLOOR_DB
    for st in stems:
        v, sr = sf.read(st, always_2d=True)
        transition.SUNG_FLOOR_DB = (-999, -999)
        old_segs = transition.sung_segments(v, sr)
        transition.SUNG_FLOOR_DB = floor
        assert transition.sung_segments(v, sr) == old_segs, st.name
    loud_noise = rng.standard_normal((44100 * 30, 2)) * 10 ** (-50 / 20)
    sung = sum(b - a for a, b in transition.sung_segments(loud_noise, 44100))
    assert sung == 0, sung
    print("7b. sung segments: the same for sung stems; none in separation noise at -50 dBFS")


def check_window_read(library):
    from algo_rhythm import dj
    worst = 0.0
    for f in sorted(library.rglob("*.mp3"))[:4]:
        if any(p in f.parts for p in ("stems", "assets", "_mixes")):
            continue
        full, _ = sf.read(f, always_2d=True, dtype="float64")
        for lo, hi in ((0, 44100 * 5), (44100 * 37 + 123, 44100 * 61), (len(full) - 44100 * 3, len(full) + 999)):
            w = dj._read_window(f, lo, hi)
            want = full[lo:hi]
            assert w.shape == want.shape, (f.name, w.shape, want.shape)
            worst = max(worst, float(np.abs(w - want).max()))
    assert worst < 1e-6, worst
    print(f"1. a window read on its own matches the whole file's samples to {worst:.1e} (decoder rounding)")


def check_talk_deadline():
    """A guest waits with the phone in hand: a slow or busy Gemini gets a quick rules answer that says why."""
    import time
    from algo_rhythm import genai, talk
    songs = [{"file": "a.mp3", "name": "Party Song", "energy_pct": 0.9, "llm": None}]
    state = {"level": "medium", "mood": None, "status": "playing", "now": None, "next": []}
    talk.DEADLINE_S, real = 0.5, talk.DEADLINE_S
    try:
        def slow(*a, **k):
            time.sleep(3)
            raise genai.GenAIError("never answers in time")
        t0 = time.time()
        r = talk.interpret("party now", state, songs, generate=slow)
        took = time.time() - t0
        assert r["source"] == "rules" and took < 1.5 and r["reply"].startswith("The AI took too long"), (took, r)
        def busy(*a, **k):
            raise genai.RateLimited("Gemini quota used up")
        r = talk.interpret("dedicate it to Ishan", state, songs, generate=busy)
        assert r["reply"].startswith("The AI is busy right now and simple rules did not understand") and not r["actions"], r
        def down(*a, **k):
            raise genai.GenAIError("HTTP 500")
        r = talk.interpret("party now", state, songs, generate=down)
        assert r["reply"].startswith("The AI is unavailable, so I used simple rules") and r["actions"], r
    finally:
        talk.DEADLINE_S = real
    print(f"8. talk: a hanging AI answered by the rules in {took:.1f} s; busy and unavailable said apart")


def main(library="test_library"):
    library = Path(library).resolve()
    check_vibe(library)
    check_match()
    check_vocal_floor(library)
    check_window_read(library)
    check_talk_deadline()
    print("BUGS PASS")


if __name__ == "__main__":
    main(*sys.argv[1:])
