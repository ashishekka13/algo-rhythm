"""Runnable check that a recipe (what the browser plays) is the join the server rendered:
python -m tests.test_recipe [LIBRARY]   (default test_library: python -m tools.make_test_track --library test_library --vocals,
then python -m algo_rhythm.tagger test_library)

For a blend with a vocal swap, a blend without stems and an echo-out it checks, with recipe.render (the browser's
arithmetic in numpy):
  - the outgoing song before the join and the incoming one after it are the server's audio, sample for sample;
  - the overlap is the server's audio through the canonical MP3, at normal speed (no pitch change): waveform match,
    and the seams on either side are continuous;
  - joins.rebuild makes exactly the incoming excerpt again from its description, for the next request.
"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from algo_rhythm import dj, joins, live_plan, media, recipe, tagger

SR = dj.OUT_SR
EXACT = 3e-3  # "sample for sample": gain curves are simplified to within recipe.TOL (0.002) of the exact gain


def db(x):
    return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)


def env(x, w=int(0.05 * SR)):
    m = x.mean(axis=1)
    return np.array([db(m[i:i + w]) for i in range(0, len(m) - w, w)])


class Audio:
    def __init__(self, library):
        self.library, self.cache = library, {}

    def asset(self, kind, audio):
        """store_asset's encoding, kept in memory: the url is a name for it."""
        path = Path(tempfile.mkdtemp()) / f"{kind}.mp3"
        media.encode(audio, SR, path)
        x, _ = sf.read(path, always_2d=True, dtype="float64")
        url = f"asset-{len(self.cache)}"
        self.cache[(url, "asset")] = x
        return url

    def __call__(self, file, stem):
        key = (file, stem)
        if key not in self.cache:
            if stem == "asset":  # stored by the server: /media/<path in the library>
                from urllib.parse import unquote
                path = self.library / unquote(file.removeprefix("/media/"))
            else:
                path = self.library / file if stem == "mix" else media.stem_path(self.library, tagger.tag_key(file))
            x, sr = sf.read(path, always_2d=True, dtype="float64")
            assert sr == SR
            self.cache[key] = x[:, :2] if x.shape[1] > 1 else np.repeat(x, 2, axis=1)
        return self.cache[key]


def pair(library, songs, planner, a_name, b_name, kind):
    i = next(k for k, s in enumerate(songs) if a_name in s["file"])
    j = next(k for k, s in enumerate(songs) if b_name in s["file"])
    ea = planner.first_song(0.5, a_name)
    eb = planner.entry(j, planner.win[j][0], {}, kind, "test", 0.7, 0.0)
    a = joins.load(library, songs[i], ea["window"])
    jn = joins.render_join(library, songs, ea, a, 0, eb)
    return a, jn, eb


def check(library, songs, planner, audio, a_name, b_name, kind, label):
    a, j, eb = pair(library, songs, planner, a_name, b_name, kind)
    start = recipe.start_item(a)
    J = recipe.next_join_at(start, 0, len(j.before))
    item = recipe.join_item(a, j, J, asset=audio.asset)
    plays = [start["body"] | {"end": J}] + item["join"]["plays"] + [item["body"]]
    L = item["overlap_s"]
    print(f"\n--- {label}: {j.plan['kind']} at {J:.2f} s, overlap {L:.2f} s, {len(json.dumps(item)) // 1024} KB")
    problems = []
    assert all(len(p["src"]) == 1 for p in plays), "every play runs at normal speed"

    def compare(t0, t1, want, what, tol, snr=None):
        got = recipe.render(plays, t0, t1, audio)[:len(want)]
        err = float(np.abs(got - want[:len(got)]).max())
        ratio = db(want[:len(got)]) - db(got - want[:len(got)])
        print(f"{what}: max sample error {err:.2e}, signal to error {ratio:.1f} dB")
        if (snr is None and err > tol) or (snr is not None and ratio < snr):
            problems.append(f"{label}: {what} differs (max {err:.2e}, {ratio:.1f} dB)")

    compare(0.0, J, j.before, "before the join", EXACT)
    compare(J, J + L, j.overlap, "the overlap (MP3)", None, snr=25)
    compare(J + L, J + L + 8, j.nxt.audio[j.nxt_skip:], "after the join", EXACT)
    # the seams: no step where one play hands over to the next
    for t in (J, J + L):
        x = recipe.render(plays, t - 0.01, t + 0.01, audio).mean(axis=1)
        y = np.concatenate([j.before, j.overlap, j.nxt.audio[j.nxt_skip:]])[int(round((t - 0.01) * SR)):][:len(x)].mean(axis=1)
        step = float(np.abs(np.diff(x)).max() - np.abs(np.diff(y)).max())
        print(f"seam at {t:.2f} s: largest sample step {step:+.3f} over the server's")
        if step > 0.05:
            problems.append(f"{label}: click at the seam at {t:.2f} s")

    # the next request rebuilds the incoming excerpt from its description alone
    nb = joins.rebuild(library, songs[eb["song"]], j.nxt.p)
    err = float(np.abs(nb.audio - j.nxt.audio).max()) if nb.audio.shape == j.nxt.audio.shape else float("inf")
    same_beats = np.allclose(nb.beats, j.nxt.beats)
    voc_ok = (nb.voc is None) == (j.nxt.voc is None) and (nb.voc is None or np.abs(nb.voc - j.nxt.voc).max() < 1e-9)
    print(f"rebuild: audio error {err:.1e}, beats {'same' if same_beats else 'DIFFER'}, vocals {'same' if voc_ok else 'DIFFER'}")
    if err > 1e-9 or not same_beats or not voc_ok:
        problems.append(f"{label}: rebuild does not reproduce the incoming excerpt")
    return problems


def main(library="test_library"):
    library = Path(library).resolve()
    songs, _ = dj.load_library(library)
    planner = live_plan.Planner(songs, 80)
    audio = Audio(library)
    problems = []
    problems += check(library, songs, planner, audio, "Synth 03", "Synth 06", "blend", "blend with vocal swap")
    problems += check(library, songs, planner, audio, "Synth 15", "Synth 12", "blend", "blend, no stems, 2:1 beats")
    problems += check(library, songs, planner, audio, "Synth 05", "Synth 14", "echo", "echo-out")
    if problems:
        print("\nRECIPE FAIL\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("\nRECIPE PASS")


if __name__ == "__main__":
    main(*sys.argv[1:])
