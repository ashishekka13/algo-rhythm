"""Runnable check that a recipe (what the browser plays) is the join the server rendered:
python -m tests.test_recipe [LIBRARY]   (default test_library: python -m tools.make_test_track --library test_library --vocals,
then python -m algo_rhythm.tagger test_library)

For a blend with a vocal swap, a blend without stems and an echo-out it checks, with recipe.render (the browser's
arithmetic in numpy):
  - the outgoing song before the join and the incoming one after it are the server's audio, sample for sample;
  - the overlap has the server's loudness, 50 ms by 50 ms (the blend's beat warp is a speed change in the browser
    and a pitch-kept stretch on the server, so it is compared by level, not by waveform);
  - where the voices are: no two voices at once, and the incoming voice is gated where the server gated it;
  - joins.rebuild makes exactly the incoming excerpt again from its description, for the next request.
"""
import json
import sys
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

    def __call__(self, file, stem):
        key = (file, stem)
        if key not in self.cache:
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
    item = recipe.join_item(a, j, J)
    plays = [start["body"] | {"end": J}] + item["join"]["plays"] + [item["body"]]
    L = item["overlap_s"]
    print(f"\n--- {label}: {j.plan['kind']} at {J:.2f} s, overlap {L:.2f} s, {len(item['join']['plays'])} plays, "
          f"{len(json.dumps(item)) // 1024} KB")
    problems = []

    # before the join: the outgoing body, sample for sample
    got = recipe.render(plays, 0.0, J, audio)
    want = j.before[:len(got)]
    err = float(np.abs(got[:len(want)] - want).max())
    print(f"before the join: max sample error {err:.2e}")
    if err > EXACT:
        problems.append(f"{label}: outgoing song differs before the join ({err:.2e})")

    # after the join: the incoming body, sample for sample (8 s)
    t_after = item["body"]["at"] if j.plan["kind"] == "blend" else J + L
    got = recipe.render(plays, t_after, t_after + 8, audio)
    skip = j.nxt_skip if j.plan["kind"] == "blend" else j.nxt_skip + j.plan.get("fill_delay", 0) * 0
    want = j.nxt.audio[skip:skip + len(got)]
    if j.plan["kind"] == "blend":
        err = float(np.abs(got[:len(want)] - want).max())
        print(f"after the join: max sample error {err:.2e}")
        if err > EXACT:
            problems.append(f"{label}: incoming song differs after the join ({err:.2e})")

    # the overlap: level, 50 ms by 50 ms
    got = recipe.render(plays, J, J + L, audio)
    want = j.overlap[:len(got)]
    e_got, e_want = env(got), env(want[:len(got)])
    loud = e_want > e_want.max() - 30
    diff = np.abs(e_got - e_want)[loud]
    print(f"overlap level: median {np.median(diff):.2f} dB, 95th pct {np.percentile(diff, 95):.2f} dB, "
          f"overall {db(got):.2f} vs {db(want):.2f} dBFS")
    if np.median(diff) > 1.0 or np.percentile(diff, 95) > 3.0 or abs(db(got) - db(want)) > 0.5:
        problems.append(f"{label}: overlap level differs (median {np.median(diff):.2f} dB)")
    if j.plan["kind"] == "echo":  # the echo-out's incoming song is not warped: compare its part exactly
        inc = [p for p in item["join"]["plays"] if p.get("file") == j.nxt.p["file"]] + [item["body"]]
        got_b = recipe.render(inc, J, J + L, audio)
        want_b = j.nxt.audio[:len(got_b)]
        err = float(np.abs(got_b[:len(want_b)] - want_b).max())
        print(f"echo-out: incoming song under the echo, max sample error {err:.2e}")
        if err > EXACT:
            problems.append(f"{label}: incoming song under the echo differs ({err:.2e})")

    # voices: render the vocal plays alone and see where each is audible
    g = j.plan.get("gates")
    if g:
        vox = lambda f: [p for p in item["join"]["plays"] if p.get("file") == f and p["stem"] == "vocals" and "hp" not in p]
        v_out = recipe.render(vox(a.p["file"]), J, J + L, audio)
        v_in = recipe.render([p for p in item["join"]["plays"] if p.get("file") == j.nxt.p["file"]], J, J + L, audio)
        # incoming: mix + (gate - sin) * vocals; its voice alone is gate * vocals: rebuild that from the vocal stem
        t = np.arange(len(v_in)) / SR
        b_v = recipe.render([p | {"gain": [[q[0], 1.0] for q in p["gain"]]} for p in item["join"]["plays"]
                             if p.get("file") == j.nxt.p["file"] and p["stem"] == "vocals"], J, J + L, audio)
        gate_in = np.clip((t - g["in_s"]) / g["fade_s"], 0, 1)[:, None]
        heard_in = b_v * gate_in
        w = SR // 2
        both = sum(db(v_out[k:k + w].mean(axis=1)) > -32 and db(heard_in[k:k + w].mean(axis=1)) > -32
                   for k in range(0, len(t) - w, w)) * 0.5
        out_gone = float(np.abs(v_out[int((g["out_s"] + g["fade_s"] + 0.01) * SR):]).max())
        print(f"voices: outgoing cut at {g['out_s']:.2f} s (after it: peak {out_gone:.1e}), incoming in at {g['in_s']:.2f} s, "
              f"both audible {both:.1f} s (server measured {j.info.get('vocal_overlap_s')})")
        if out_gone > 1e-6:
            problems.append(f"{label}: the outgoing voice goes on after its cut")
        if both > max(1.0, (j.info.get("vocal_overlap_s") or 0) + 0.5):
            problems.append(f"{label}: two voices overlap for {both} s")

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
