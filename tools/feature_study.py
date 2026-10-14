"""Which song features predict a clean blend? Renders many candidate blends (the overlap only), measures each one
from the audio, and ranks the features we know BEFORE mixing by how well they predict those measurements.

    .venv/bin/python feature_study.py LIBRARY [--pairs 160] [--jobs 4]

Measured from the rendered overlap (the "outcomes"):
  flam_ms       median beat misalignment between the two songs after beat-matching (a flam / train-wreck)
  drift_ms      how much that misalignment wanders over the overlap (grids that do not really fit)
  chord_clash   1 - correlation of the two instrumentals' pitch-class profiles while both play (out-of-key)
  masking       share of the 250 Hz - 4 kHz range where both songs are loud at once (mud)
Writes LIBRARY/_mixes/feature_study.csv and prints, per outcome, the features ranked by |Spearman correlation|.
"""
import argparse
import csv
import random
import sys
from multiprocessing import Pool
from pathlib import Path

import librosa
import numpy as np
from scipy.stats import spearmanr

from algo_rhythm import dj
from algo_rhythm import live_plan
from algo_rhythm import transition

SR = dj.OUT_SR
SNAP = True  # same beat snapping as transition.transition; --no-snap measures the old grid-only warp
_lib = _songs = None


def triplet_feel(library, song):
    """0 = straight eighths, 1 = triplet/compound (6/8, dadra, shuffle): onset strength at beat thirds vs halves."""
    y, sr = librosa.load(library / song["file"], sr=22050, mono=True, offset=song["duration_s"] * 0.3, duration=40)
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=256)
    t0 = song["duration_s"] * 0.3
    beats = song["beats"][(song["beats"] > t0) & (song["beats"] < t0 + 39)] - t0
    at = lambda t: env[np.clip(np.round(t * sr / 256).astype(int), 0, len(env) - 1)]
    p = np.diff(beats)
    if len(p) < 8:
        return 0.5
    half, third = at(beats[:-1] + p / 2), (at(beats[:-1] + p / 3) + at(beats[:-1] + 2 * p / 3)) / 2
    return float(np.mean(third) / (np.mean(third) + np.mean(half) + 1e-9))


def _init(library, snap):
    global _lib, _songs, SNAP
    _lib, SNAP = library, snap
    _songs, _ = dj.load_library(library)


def local_offsets(a, b, sr, win_s=4.0):
    out = []
    for k in range(0, len(a) - int(win_s * sr), int(win_s * sr / 2)):
        seg = slice(k, k + int(win_s * sr))
        out.append(transition.phase_offset(a[seg], b[seg], sr, max_ms=90) / sr * 1000)
    return np.array(out)


def measure(job):
    ia, wa, ib, wb = job
    A, B = _songs[ia], _songs[ib]
    try:
        a, ab, av = dj.load_excerpt_vocals(_lib, A, wa)
        b, bb, bv = dj.load_excerpt_vocals(_lib, B, wb)
        step_a, step_b = transition.beat_steps(ab, bb)
        ob = transition.OVERLAP_BEATS
        tail, head = ab[-(ob * step_a + 1)::step_a], bb[:ob * step_b + 1:step_b]
        if len(tail) < ob + 1 or len(head) < ob + 1:
            return None
        if SNAP:
            tail = tail[0] + transition.snap_beats(a[int(round(tail[0] * SR)):int(round(tail[-1] * SR))], tail - tail[0], SR)
            head = transition.snap_beats(b[:int(round(head[-1] * SR))], head, SR)
        slots, _, _ = transition.overlap_slots(tail, head)
        a0 = int(round(tail[0] * SR))
        a_w, b_w = transition.warp(a[a0:], tail, slots, SR), transition.warp(b, head, slots, SR)
        nudge = transition.phase_offset(a_w, b_w, SR)
        b_w = transition.shift(b_w, nudge)
        if av is not None and bv is not None:  # judge harmony and mud on the instrumentals: the swap removes voices
            a_i = a_w - transition.warp(av[a0:], tail, slots, SR)
            b_i = b_w - transition.shift(transition.warp(bv, head, slots, SR), nudge)
        else:
            a_i, b_i = a_w, b_w
        ma, mb = (librosa.resample(librosa.to_mono(x.T), orig_sr=SR, target_sr=22050) for x in (a_w, b_w))
        offs = local_offsets(ma, mb, 22050)
        mid = slice(len(ma) // 4, 3 * len(ma) // 4)
        ia_, ib_ = (librosa.resample(librosa.to_mono(x.T), orig_sr=SR, target_sr=22050)[mid] for x in (a_i, b_i))
        ca = librosa.feature.chroma_cqt(y=ia_, sr=22050).mean(axis=1)
        cb = librosa.feature.chroma_cqt(y=ib_, sr=22050).mean(axis=1)
        sa, sb = (librosa.amplitude_to_db(np.abs(librosa.stft(x, n_fft=2048, hop_length=512)), ref=1.0) for x in (ia_, ib_))
        f = librosa.fft_frequencies(sr=22050, n_fft=2048)
        band = (f >= 250) & (f <= 4000)
        top = max(sa[band].max(), sb[band].max())
        loud = (sa[band] > top - 35) & (sb[band] > top - 35) & (np.abs(sa[band] - sb[band]) < 6)
        return {"flam_ms": float(np.median(np.abs(offs))), "drift_ms": float(np.ptp(offs)) if len(offs) else 0.0,
                "chord_clash": float(1 - np.corrcoef(ca, cb)[0, 1]), "masking": float(loud.mean())}
    except Exception as e:  # one bad pair must not stop the study
        print(f"skip {A['name'][:30]} -> {B['name'][:30]}: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def features(A, B, wa, wb, trip):
    llm = lambda s, k, d: (s.get("llm") or {}).get(k, d)
    ca, cb = np.array(A["vectors"]["chroma"]), np.array(B["vectors"]["chroma"])
    return {
        "tempo_gap": dj.tempo_gap(A["tempo"]["bpm"], B["tempo"]["bpm"]),
        "key_steps": dj.key_steps(A["key"]["camelot"], B["key"]["camelot"]),
        "key_conf_min": min(A["key"]["confidence"], B["key"]["confidence"]),
        "song_chroma_corr": float(np.corrcoef(ca, cb)[0, 1]),
        "tempo_conf_min": min(A["tempo"]["confidence"], B["tempo"]["confidence"]),
        "beat_strength_min": min(A["tempo"]["beat_strength"], B["tempo"]["beat_strength"]),
        "tempo_flag_any": float(any("tempo_disagreement" in s["quality"]["flags"] for s in (A, B))),
        "kick_disagree_max": max(min(abs(s["tempo"]["kick_band_bpm"] * r - s["tempo"]["bpm"]) for r in (0.5, 1, 2)) / s["tempo"]["bpm"]
                                 for s in (A, B)),
        "triplet_max": max(trip[A["file"]], trip[B["file"]]),
        "triplet_diff": abs(trip[A["file"]] - trip[B["file"]]),
        "energy_diff": abs(A["energy_pct"] - B["energy_pct"]),
        "timbre_cos": float(A["timbre"] @ B["timbre"]),
        "valence_diff": abs(A["valence_pct"] - B["valence_pct"]),
        "dance_diff": abs(A["dance_pct"] - B["dance_pct"]),
        "acoustic_diff": abs(A["acoustic_pct"] - B["acoustic_pct"]),
        "perc_min": min(A["energy"]["percussive_ratio"], B["energy"]["percussive_ratio"]),
        "onset_rate_diff": abs(A["energy"]["onset_rate"] - B["energy"]["onset_rate"]),
        "out_vocal": wa["out_vocal"], "in_vocal": wb["in_vocal"],
        "out_q": wa["out_q"], "in_q": wb["in_q"],
        "gemini_energy_diff": abs(llm(A, "energy", 2.5) - llm(B, "energy", 2.5)),
        "pair_score": dj.pair_score(dj.pair_base(A, B)[0]),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("library", type=Path)
    ap.add_argument("--pairs", type=int, default=160)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-snap", action="store_true")
    ap.add_argument("--out", default="feature_study.csv")
    args = ap.parse_args(argv)
    global SNAP
    SNAP = not args.no_snap
    library = args.library.resolve()
    songs, _ = dj.load_library(library)
    planner = live_plan.Planner(songs, 100)
    blends = [(a, b) for a in range(len(songs)) for b in range(len(songs))
              if a != b and planner.win[a] and planner.win[b] and dj.transition_kind(songs[a], songs[b])[0] == "blend"]
    random.Random(7).shuffle(blends)
    jobs = []
    for a, b in blends[:args.pairs]:
        wb = next((w for w in planner.win[b] if w["natural_start"]), planner.win[b][0])
        jobs.append((a, planner.win[a][0], b, wb))
    used = {songs[i]["file"]: songs[i] for job in jobs for i in (job[0], job[2])}
    print(f"{len(blends)} blendable pairs; rendering {len(jobs)}, measuring rhythm feel of {len(used)} songs", flush=True)
    with Pool(args.jobs, initializer=_init, initargs=(library, SNAP)) as pool:
        trip = dict(zip(used, pool.starmap(triplet_feel, [(library, s) for s in used.values()])))
        outs = pool.map(measure, jobs, chunksize=2)
    rows = []
    for (a, wa, b, wb), out in zip(jobs, outs):
        if out:
            rows.append({"from": songs[a]["name"], "to": songs[b]["name"], **features(songs[a], songs[b], wa, wb, trip), **out})
    path = library / "_mixes" / args.out
    path.parent.mkdir(exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    feats = [k for k in rows[0] if k not in ("from", "to", "flam_ms", "drift_ms", "chord_clash", "masking")]
    print(f"\n{len(rows)} blends measured -> {path}")
    for out in ("flam_ms", "drift_ms", "chord_clash", "masking"):
        y = np.array([r[out] for r in rows])
        ranked = sorted(((spearmanr([r[k] for r in rows], y)[0], k) for k in feats), key=lambda t: -abs(np.nan_to_num(t[0])))
        print(f"\n{out}: median {np.median(y):.3g}, worst 10% {np.percentile(y, 90):.3g}")
        for rho, k in ranked[:8]:
            print(f"   {k:20s} rho {rho:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
