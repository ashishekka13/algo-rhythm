"""Blind A/B listening harness: render the same transition under named variants, vote blind, fit the votes.

    .venv/bin/python ab_harness.py render LIBRARY overlap_length [--pairs 40] [--jobs 8]
    .venv/bin/python ab_harness.py serve LIBRARY [--experiment overlap_length] [--port 8766]
    .venv/bin/python ab_harness.py report LIBRARY
    .venv/bin/python ab_harness.py fit LIBRARY
    .venv/bin/python ab_harness.py selftest LIBRARY

Each clip is 12 s of song A, the whole transition, then 12 s of song B. Every clip is loudness-matched to the
same level so neither variant wins by being louder. Clips are saved under opaque random ids; the page learns
which variant was which only in the answer to its vote. Votes go to LIBRARY/_mixes/ab_votes.csv; the harness
never reads or writes the live DJ's play history. How to add an experiment: AB_HARNESS.md.
"""
import argparse
import csv
import hashlib
import json
import math
import random
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy.stats import binomtest, norm

from algo_rhythm import dj
from tools import feature_study
from algo_rhythm import live
from algo_rhythm import live_plan
from algo_rhythm import transition

SR = dj.OUT_SR
CONTEXT_S = 12           # seconds of A before and of B after the transition
CLIP_LUFS = -18          # every clip is set to this integrated loudness (leaves room for the peaks)
MAX_LU_GAP = 0.5         # the two clips of a trial may differ by at most this much loudness
PORT = 8766              # the live DJ uses 8765
PAGE = Path(__file__).resolve().parent.parent / "algo_rhythm" / "static" / "ab.html"
CHOICES = ("x", "y", "same", "both_bad")
ID = re.compile(r"[0-9a-f]{32}")

# An experiment = named variants (keyword arguments for transition.transition) + how to pick its pairs.
# "blend" pairs pass every blend gate (dj.transition_kind); they are sampled evenly across the tempo-gap range.
EXPERIMENTS = {
    "overlap_length": {"variants": {"32beat": {"overlap_beats": 32}, "16beat": {"overlap_beats": 16}},
                       "pairs": "blend", "n": 40},
}

FIELDS = ["timestamp", "experiment", "pair_id", "song_a", "song_b", "variant_x", "variant_y", "presented_first",
          "choice", "winner", "replays_x", "replays_y", "decide_s",
          "pair_kind", "tempo_gap", "chroma_corr", "feel_diff", "camelot_steps", "tempo_uncertain_a",
          "tempo_uncertain_b", "pair_score", *[f"term_{k}" for k in dj.PAIR_KEYS], "window_a_s", "window_b_s"]
CLIP_FIELDS = ["type", "overlap_s", "drift_ms", "flam_ms", "clash", "masking", "loudness_jump_db", "dip_db",
               "bass_pileup_db", "stretch", "nudge_ms", "vocal_overlap_s", "clean", "qa_pass", "qa_fail"]
FIELDS += [f"{side}_{k}" for side in ("x", "y") for k in CLIP_FIELDS]


def ab_dir(library, experiment=None):
    d = library / "_mixes" / "ab"
    return d / experiment if experiment else d


def votes_path(library):
    return library / "_mixes" / "ab_votes.csv"


# ---------------------------------------------------------------- rendering

def pair_features(A, B, wa, wb):
    """What is known about the pair before mixing: the columns a later model is fitted on."""
    terms, kind, _ = dj.pair_base(A, B)
    return {"pair_kind": kind, "tempo_gap": round(dj.tempo_gap(A["tempo"]["bpm"], B["tempo"]["bpm"]), 4),
            "chroma_corr": round(dj.chroma_corr(A, B), 4),
            "feel_diff": round(abs(A.get("triplet", 0.5) - B.get("triplet", 0.5)), 4),
            "camelot_steps": dj.key_steps(A["key"]["camelot"], B["key"]["camelot"]),
            "tempo_uncertain_a": int(A["tempo_uncertain"]), "tempo_uncertain_b": int(B["tempo_uncertain"]),
            "pair_score": round(dj.pair_score(terms), 4), **{f"term_{k}": round(v, 4) for k, v in terms.items()},
            "window_a_s": round(wa["end_s"] - wa["start_s"], 1), "window_b_s": round(wb["end_s"] - wb["start_s"], 1)}


def pick_pairs(songs, planner, n, rng):
    """`n` blendable pairs spread evenly over the tempo-gap range; no song used more than twice."""
    pairs = [(a, b) for a in range(len(songs)) for b in range(len(songs))
             if a != b and planner.win[a] and planner.win[b] and dj.transition_kind(songs[a], songs[b])[0] == "blend"]
    pairs.sort(key=lambda p: dj.tempo_gap(songs[p[0]]["tempo"]["bpm"], songs[p[1]]["tempo"]["bpm"]))
    used, out = {}, []
    for bin_ in np.array_split(np.arange(len(pairs)), n):
        for k in rng.sample(list(bin_), len(bin_)):
            a, b = pairs[k]
            if used.get(a, 0) < 2 and used.get(b, 0) < 2:
                used[a], used[b] = used.get(a, 0) + 1, used.get(b, 0) + 1
                out.append((a, b))
                break
    return out


def normalise(x, meter):
    """Same limiter as the live engine, then set the integrated loudness to CLIP_LUFS (twice: the limiter moves it)."""
    x = 0.97 * dj.soft_limit(x.copy())
    for _ in range(3):
        x = x * 10 ** ((CLIP_LUFS - meter.integrated_loudness(x)) / 20)
        if np.abs(x).max() > 0.98:
            x = 0.97 * dj.soft_limit(x)
    return np.clip(x, -1, 1)


def render_clip(library, A, wa, B, wb, opts):
    """One clip, rendered the way the live engine renders a blend (live.Engine._render_join, first try, no
    fallback): quiet-head trim and gain staging on B, the vocal swap, the level lift. Returns (audio, info, at_s)."""
    seg, beats, seg_voc = dj.load_excerpt_vocals(library, A, wa)
    nxt, nxt_beats, voc = dj.load_excerpt_vocals(library, B, wb)
    extras = (voc,) if voc is not None else ()
    nxt, nxt_beats, extras = live.trim_quiet_head(seg, nxt, nxt_beats, SR, extras=extras)
    nxt, extras = live.stage_both(seg, nxt, SR, extras=extras)
    nxt_voc = extras[0] if extras else None
    before, overlap, nxt_skip, info = transition.transition(seg, beats, nxt, nxt_beats, SR, "blend", seg_voc, nxt_voc, **opts)
    if info.get("vocal_swap"):
        overlap = live.level_swap(before, overlap, nxt, nxt_skip, SR)
    info["clean"] = info["type"] == "blend" and live.blend_is_clean(before, overlap, nxt, nxt_skip, info, SR)
    head = before[-CONTEXT_S * SR:]
    return np.concatenate([head, overlap, nxt[nxt_skip:nxt_skip + CONTEXT_S * SR]]), info, len(head) / SR


def clip_qa(path, A, B, wa, wb, info, at_s):
    """dj.qa on the clip, keeping the checks about how this one transition sounds (plus peak and dead air).
    The energy step is left out: it is about the song choice, the same for every variant."""
    row = lambda s, w: {"file": s["file"], "energy_pct": s["energy_pct"], "target_energy": s["energy_pct"],
                        "bpm": s["tempo"]["bpm"], "window": w, "terms": {"vibe": 1.0, "join": 1.0}}
    rows = [{**row(A, wa), "position_s": 0.0, "transition_in": "start"},
            {**row(B, wb), "position_s": at_s, "transition_in": "blend", "transition_info": info}]
    checks = [c for c in dj.qa(path, rows)["checks"] if (c["check"].startswith("T1 ") and c["check"] != "T1 energy step")
              or c["check"] in ("peak", "dead-air seconds")]
    return {c["check"]: c for c in checks}


def _init(library):
    feature_study._init(library, True)  # loads the library in each worker; measure() reads it from there


def render_pair(job):
    """Render every variant of one pair. Returns the trial dict, or None if any variant could not be rendered."""
    library, name, ia, wa, ib, wb = job
    exp, songs = EXPERIMENTS[name], feature_study._songs
    A, B = songs[ia], songs[ib]
    out_dir, meter = ab_dir(library, name) / "clips", pyln.Meter(SR)
    clips = {}
    try:
        for variant, opts in exp["variants"].items():
            audio, info, at_s = render_clip(library, A, wa, B, wb, opts)
            audio = normalise(audio, meter)
            cid = secrets.token_hex(16)
            path = out_dir / f"{cid}.flac"
            sf.write(path, audio, SR, subtype="PCM_16")
            checks = clip_qa(path, A, B, wa, wb, info, at_s)
            old = transition.OVERLAP_BEATS  # feature_study.measure reads the overlap length from here
            transition.OVERLAP_BEATS = opts.get("overlap_beats", old)
            try:
                m = feature_study.measure((ia, wa, ib, wb)) or {}
            finally:
                transition.OVERLAP_BEATS = old
            val = lambda k: checks[k]["value"] if k in checks else None
            clips[variant] = {"id": cid, "lufs": round(meter.integrated_loudness(sf.read(path)[0]), 2),
                              "seconds": round(len(audio) / SR, 1), "transition_at_s": round(at_s, 1),
                              "type": info["type"], "overlap_s": info.get("overlap_s"),
                              "drift_ms": m.get("drift_ms"), "flam_ms": m.get("flam_ms"), "clash": m.get("chord_clash"),
                              "masking": m.get("masking"), "loudness_jump_db": val("T1 loudness jump dB"),
                              "dip_db": val("T1 dip dB"), "bass_pileup_db": val("T1 bass pile-up dB"),
                              "stretch": val("T1 tempo stretch"), "nudge_ms": val("T1 beat-grid nudge ms"),
                              "vocal_overlap_s": info.get("vocal_overlap_s"), "clean": bool(info.get("clean")),
                              "qa_pass": all(c["ok"] for c in checks.values()),
                              "qa_fail": [k for k, c in checks.items() if not c["ok"]]}
    except Exception as e:  # one bad pair must not stop the run
        print(f"skip {A['name'][:30]} -> {B['name'][:30]}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        for c in clips.values():
            (out_dir / f"{c['id']}.flac").unlink(missing_ok=True)
        return None
    if any(c["type"] != "blend" for c in clips.values()):  # a variant fell back to an echo-out: not a fair test
        for c in clips.values():
            (out_dir / f"{c['id']}.flac").unlink(missing_ok=True)
        return None
    pid = hashlib.sha1(f"{A['file']}|{B['file']}|{wa['start_beat']}|{wb['start_beat']}".encode()).hexdigest()[:12]
    return {"pair_id": pid, "song_a": A["name"], "song_b": B["name"], "features": pair_features(A, B, wa, wb),
            "clips": clips}


def cmd_render(args):
    library, name = args.library.resolve(), args.experiment
    exp = EXPERIMENTS[name]
    n = args.pairs or exp["n"]
    songs, _ = dj.load_library(library)
    planner = live_plan.Planner(songs, live.EXCERPT_S)
    rng = random.Random(args.seed)
    cand = pick_pairs(songs, planner, int(n * 1.3) + 2, rng)  # spares: some pairs are too short for 32 beats
    jobs = []
    for a, b in cand:
        wb = next((w for w in planner.win[b] if w["natural_start"]), planner.win[b][0])
        jobs.append((library, name, a, planner.win[a][0], b, wb))
    (ab_dir(library, name) / "clips").mkdir(parents=True, exist_ok=True)
    for old in (ab_dir(library, name) / "clips").glob("*.flac"):  # a re-render replaces the experiment's clips
        old.unlink()
    t0 = time.time()
    print(f"{name}: rendering {len(jobs)} candidate pairs x {len(exp['variants'])} variants", flush=True)
    with Pool(args.jobs, initializer=_init, initargs=(library,)) as pool:
        trials = [t for t in pool.map(render_pair, jobs, chunksize=1) if t]
    for t in trials[n:]:  # keep n, spread over the range (the spares come from the same bins)
        for c in t["clips"].values():
            (ab_dir(library, name) / "clips" / f"{c['id']}.flac").unlink(missing_ok=True)
    trials = trials[:n]
    manifest = {"experiment": name, "variants": list(exp["variants"]), "rendered": time.strftime("%Y-%m-%d %H:%M"),
                "trials": trials}
    (ab_dir(library, name) / "trials.json").write_text(json.dumps(manifest, indent=1))
    print(f"{len(trials)} trials ready in {time.time() - t0:.0f} s -> {ab_dir(library, name)}", flush=True)
    return 0


# ---------------------------------------------------------------- serving

def load_trials(library, only=None):
    out = []
    for f in sorted(ab_dir(library).glob("*/trials.json")):
        m = json.loads(f.read_text())
        if only is None or m["experiment"] in only:
            out += [{**t, "experiment": m["experiment"], "dir": f.parent} for t in m["trials"]]
    return out


def assign_order(trial, rng):
    """Which variant plays as X (first) and which as Y: random per trial."""
    names = list(trial["clips"])
    rng.shuffle(names)
    return names[0], names[1]


def append_vote(path, row):
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def voted(path):
    if not path.exists():
        return set()
    with path.open() as f:
        return {(r["experiment"], r["pair_id"]) for r in csv.DictReader(f)}


class Harness:
    """The trial queue and the secret mapping from tokens and clip ids to variants. Nothing here is sent to the
    page except opaque ids, until a vote is in."""

    def __init__(self, library, only=None, votes=None, rng=None):
        self.votes = votes or votes_path(library)
        self.rng = rng or random.SystemRandom()
        done = voted(self.votes)
        self.queue = [t for t in load_trials(library, only) if (t["experiment"], t["pair_id"]) not in done]
        self.rng.shuffle(self.queue)
        self.total, self.done = len(self.queue), 0
        self.files = {c["id"]: t["dir"] / "clips" / f"{c['id']}.flac" for t in self.queue for c in t["clips"].values()}
        self.open, self.times, self.lock = {}, [], threading.Lock()

    def next(self):
        with self.lock:
            if not self.queue:
                return {"done": True, "n": self.done, "total": self.total}
            t = self.queue[0]
            vx, vy = assign_order(t, self.rng)
            token = secrets.token_hex(16)
            self.open[token] = (t, vx, vy, time.time())
            avg = np.mean(self.times) if self.times else 60.0
            return {"token": token, "x": f"/clip/{t['clips'][vx]['id']}", "y": f"/clip/{t['clips'][vy]['id']}",
                    "n": self.done + 1, "total": self.total, "eta_s": int(avg * len(self.queue)),
                    "transition_at_s": [t["clips"][vx]["transition_at_s"], t["clips"][vy]["transition_at_s"]]}

    def skip(self, token):
        with self.lock:
            t, *_ = self.open.pop(token)
            if t in self.queue:
                self.queue.remove(t)
                self.queue.append(t)

    def vote(self, token, choice, replays_x, replays_y):
        if choice not in CHOICES:
            raise ValueError("unknown choice")
        with self.lock:
            t, vx, vy, t0 = self.open.pop(token)
            decide = time.time() - t0
            winner = {"x": vx, "y": vy}.get(choice, "")
            row = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "experiment": t["experiment"],
                   "pair_id": t["pair_id"], "song_a": t["song_a"], "song_b": t["song_b"], "variant_x": vx,
                   "variant_y": vy, "presented_first": vx, "choice": choice, "winner": winner,
                   "replays_x": int(replays_x), "replays_y": int(replays_y), "decide_s": round(decide, 1),
                   **t["features"],
                   **{f"x_{k}": t["clips"][vx].get(k) for k in CLIP_FIELDS},
                   **{f"y_{k}": t["clips"][vy].get(k) for k in CLIP_FIELDS}}
            append_vote(self.votes, row)
            if t in self.queue:
                self.queue.remove(t)
            self.done += 1
            self.times.append(min(decide, 300))
            return {"x": vx, "y": vy, "winner": winner, "choice": choice, "experiment": t["experiment"],
                    "song_a": t["song_a"], "song_b": t["song_b"]}


def make_handler(h, port):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get("Host") not in hosts:  # stops DNS rebinding
                return self._send(403, {"error": "bad host"})
            if self.path == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/next":
                return self._send(200, h.next())
            m = re.fullmatch(r"/clip/([0-9a-f]{32})", self.path)
            if m and m.group(1) in h.files:
                return self._send(200, h.files[m.group(1)].read_bytes(), "audio/flac")
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.headers.get("Host") not in hosts:
                return self._send(403, {"error": "bad host"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._send(415, {"error": "send application/json"})
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 1024:
                return self._send(400, {"error": "bad body size"})
            try:
                body = json.loads(self.rfile.read(n))
                if self.path == "/api/vote":
                    return self._send(200, h.vote(body["token"], body["choice"], int(body.get("replays_x", 0)),
                                                  int(body.get("replays_y", 0))))
                if self.path == "/api/skip":
                    h.skip(body["token"])
                    return self._send(200, {"ok": True})
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, {"error": str(e) or "bad request"})
            self._send(404, {"error": "not found"})

    return Handler


def cmd_serve(args):
    library = args.library.resolve()
    h = Harness(library, set(args.experiment) if args.experiment else None)
    if not h.total:
        print("nothing to vote on: render an experiment first (or every trial already has a vote)")
        return 1
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(h, args.port))
    print(f"{h.total} trials. Open http://127.0.0.1:{args.port}  (Ctrl+C to stop; votes -> {h.votes})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------- report and fit

def read_votes(path):
    with path.open() as f:
        return list(csv.DictReader(f))


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 1.0
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return c - half, c + half


def summarize(rows, variants):
    """Win rate of variants[0] over variants[1] among decisive votes, the 95% CI on the difference in win
    rates, a two-sided binomial test, and the no-difference / both-bad counts."""
    first = sum(r["winner"] == variants[0] for r in rows)
    second = sum(r["winner"] == variants[1] for r in rows)
    n = first + second
    lo, hi = wilson(first, n)
    return {"n": len(rows), "decisive": n, "wins": {variants[0]: first, variants[1]: second},
            "win_rate": first / n if n else None, "diff_ci": (2 * lo - 1, 2 * hi - 1),
            "p_value": binomtest(first, n, 0.5).pvalue if n else None,
            "same": sum(r["choice"] == "same" for r in rows), "both_bad": sum(r["choice"] == "both_bad" for r in rows)}


def gap_bucket(g):
    g = float(g)
    return "0-3%" if g < 0.03 else "3-6%" if g < 0.06 else "6-10%" if g < 0.10 else "10%+"


def cmd_report(args):
    library = args.library.resolve()
    path = Path(args.votes) if args.votes else votes_path(library)
    if not path.exists():
        print(f"no votes yet ({path})")
        return 1
    rows = read_votes(path)
    for name in sorted({r["experiment"] for r in rows}):
        sub = [r for r in rows if r["experiment"] == name]
        variants = list(EXPERIMENTS[name]["variants"]) if name in EXPERIMENTS else sorted({r["variant_x"] for r in sub})
        s = summarize(sub, variants)
        print(f"\n== {name}: {variants[0]} vs {variants[1]}  ({s['n']} trials, {s['decisive']} with a winner)")
        for v in variants:
            k = s["wins"][v]
            print(f"   {v:16s} won {k:3d}  ({k / s['decisive']:.0%} of decisive)" if s["decisive"] else f"   {v}: -")
        if s["decisive"]:
            lo, hi = s["diff_ci"]
            print(f"   difference in win rate ({variants[0]} - {variants[1]}): {2 * s['win_rate'] - 1:+.0%}, "
                  f"95% CI {lo:+.0%} .. {hi:+.0%}; binomial p = {s['p_value']:.3g}"
                  f"{'  (significant)' if s['p_value'] < 0.05 else '  (not significant)'}")
        print(f"   no difference: {s['same']}   both bad: {s['both_bad']}")
        for label, key in (("tempo gap", lambda r: gap_bucket(r["tempo_gap"])), ("transition type", lambda r: r["pair_kind"])):
            print(f"   by {label}:")
            for b in sorted({key(r) for r in sub}):
                t = summarize([r for r in sub if key(r) == b], variants)
                rate = f"{t['win_rate']:.0%} for {variants[0]}" if t["decisive"] else "no decisive votes"
                print(f"      {b:10s} n={t['n']:3d}  {rate}  (same {t['same']}, both bad {t['both_bad']})")
    return 0


FIT_PAIR = ["tempo_gap", "chroma_corr", "feel_diff", "camelot_steps", "tempo_uncertain_a", "tempo_uncertain_b",
            *[f"term_{k}" for k in dj.PAIR_KEYS]]
FIT_CLIP = ["overlap_s", "drift_ms", "flam_ms", "clash", "masking", "loudness_jump_db", "dip_db", "bass_pileup_db",
            "stretch", "nudge_ms", "vocal_overlap_s"]


def clip_rows(rows):
    """One row per clip heard: label 1 = it won or was 'no difference', 0 = it lost or 'both bad'."""
    X, y = [], []
    for r in rows:
        for side in ("x", "y"):
            vals = [r.get(k) for k in FIT_PAIR] + [r.get(f"{side}_{k}") for k in FIT_CLIP]
            X.append([float(v) if v not in (None, "", "None") else np.nan for v in vals])
            y.append(1.0 if r["choice"] in (side, "same") else 0.0)
    return np.array(X), np.array(y)


def logistic(X, y, ridge=1.0, iters=50):
    """Logistic regression by Newton's method, with a small ridge so a tiny vote set still converges.
    Returns (coefficients incl. intercept, standard errors)."""
    A = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(A.shape[1])
    pen = np.full(A.shape[1], ridge)
    pen[0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-A @ w))
        H = A.T @ (A * (p * (1 - p))[:, None]) + np.diag(pen)
        step = np.linalg.solve(H, A.T @ (y - p) - pen * w)
        w += step
        if np.abs(step).max() < 1e-8:
            break
    return w, np.sqrt(np.diag(np.linalg.inv(H)))


def cmd_fit(args):
    library = args.library.resolve()
    path = Path(args.votes) if args.votes else votes_path(library)
    rows = read_votes(path) if path.exists() else []
    X, y = clip_rows(rows)
    names = FIT_PAIR + FIT_CLIP
    if len(y) < 20:
        print(f"only {len(y)} clips heard; need at least 20 to fit anything")
        return 1
    keep = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all() and np.nanstd(X[:, j]) > 0]
    X = X[:, keep]
    X = np.where(np.isnan(X), np.nanmean(X, axis=0), X)
    X = (X - X.mean(axis=0)) / X.std(axis=0)  # standardised: coefficients are comparable across features
    w, se = logistic(X, y)
    print(f"{len(rows)} votes, {len(y)} clips; label = clip won or 'no difference'. Standardised coefficients:")
    fitted = {}
    for j, k in sorted(enumerate(keep), key=lambda t: -abs(w[t[0] + 1])):
        z = w[j + 1] / se[j + 1]
        fitted[names[k]] = w[j + 1]
        print(f"   {names[k]:22s} {w[j + 1]:+.3f}  (se {se[j + 1]:.3f}, p {2 * norm.sf(abs(z)):.3g})")
    print("\nHand-set pair_score weights (dj.WEIGHTS) vs what the votes say (not applied):")
    hand = sorted(dj.PAIR_KEYS, key=lambda k: -dj.WEIGHTS[k])
    for k in hand:
        f = fitted.get(f"term_{k}")
        print(f"   {k:12s} weight {dj.WEIGHTS[k]:.2f}   fitted {'%+.3f' % f if f is not None else 'n/a (no variation)'}")
    return 0


# ---------------------------------------------------------------- self-test

def selftest(args):
    """Blindness, order randomisation, loudness matching, report correctness, history isolation."""
    library = args.library.resolve()
    hist = library / "_mixes" / "live_history.json"
    hist_before = hist.read_bytes() if hist.exists() else None
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")

    # 1. blindness, on a synthetic experiment served for real
    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp)
        d = ab_dir(lib, "synthetic")
        (d / "clips").mkdir(parents=True)
        labels, trials, meter = ("ALPHAVARIANT", "BETAVARIANT"), [], pyln.Meter(SR)
        rng = np.random.default_rng(0)
        for i in range(3):
            clips = {}
            for v in labels:
                cid = secrets.token_hex(16)
                sf.write(d / "clips" / f"{cid}.flac", normalise(0.1 * rng.standard_normal((SR * 3, 2)), meter), SR)
                clips[v] = {"id": cid, "transition_at_s": 1.0}
            trials.append({"pair_id": f"p{i}", "song_a": "A", "song_b": "B", "features": {}, "clips": clips})
        (d / "trials.json").write_text(json.dumps({"experiment": "synthetic", "variants": list(labels), "trials": trials}))
        h = Harness(lib)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(h, None))
        port = srv.server_address[1]
        srv.RequestHandlerClass = make_handler(h, port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        get = lambda p: urllib.request.urlopen(base + p).read()
        html, nxt = get("/").decode(), get("/api/next")
        js = json.loads(nxt)
        leak = [l for l in labels if l.lower() in html.lower() or l.lower() in nxt.decode().lower()]
        check("variant names absent from the page and from /api/next", not leak, str(leak or ""))
        check("audio urls are opaque ids", all(re.fullmatch(r"/clip/[0-9a-f]{32}", js[k]) for k in ("x", "y")),
              f"{js['x']}, {js['y']}")
        audio = urllib.request.urlopen(base + js["x"])
        check("clip served as audio/flac with no name in headers", audio.headers["Content-Type"] == "audio/flac"
              and not any(l.lower() in str(audio.headers).lower() for l in labels))
        req = urllib.request.Request(base + "/api/vote", json.dumps({"token": js["token"], "choice": "x"}).encode(),
                                     {"Content-Type": "application/json"})
        ans = json.loads(urllib.request.urlopen(req).read())
        check("names revealed only in the vote answer", {ans["x"], ans["y"]} == set(labels))
        rows = read_votes(h.votes)
        check("vote logged to the harness's own csv", len(rows) == 1 and h.votes == votes_path(lib)
              and rows[0]["winner"] == ans["x"], str(h.votes))
        srv.shutdown()

        # 2. order randomisation
        r = random.Random(1)
        firsts = sum(assign_order(trials[0], r)[0] == labels[0] for _ in range(1000))
        check("order randomised (variant 1 played first 45-55% of 1000 trials)", 450 <= firsts <= 550, f"{firsts / 10:.1f}%")

    # 3. loudness of every real rendered trial
    gaps = []
    for t in load_trials(library):
        lu = [meter.integrated_loudness(sf.read(t["dir"] / "clips" / f"{c['id']}.flac")[0]) for c in t["clips"].values()]
        gaps.append(max(lu) - min(lu))
    if gaps:
        check(f"loudness matched within {MAX_LU_GAP} LU ({len(gaps)} rendered trials)", max(gaps) <= MAX_LU_GAP,
              f"largest gap {max(gaps):.2f} LU")
    else:
        print("SKIP  loudness: no rendered experiment yet")

    # 4. report recovers a known answer
    r = random.Random(3)
    fake = [{"winner": "a" if r.random() < 0.7 else "b", "choice": "x"} for _ in range(2000)]
    s = summarize(fake, ["a", "b"])
    lo, hi = s["diff_ci"]
    check("report recovers a known 70% win rate", abs(s["win_rate"] - 0.7) < 0.03 and lo < 0.4 < hi and s["p_value"] < 1e-6,
          f"win {s['win_rate']:.3f}, diff CI {lo:+.3f}..{hi:+.3f}")
    fake = [{"winner": "a" if r.random() < 0.5 else "b", "choice": "x"} for _ in range(400)]
    s = summarize(fake, ["a", "b"])
    check("report finds no effect when there is none", s["diff_ci"][0] < 0 < s["diff_ci"][1], f"p {s['p_value']:.2f}")
    X = np.random.default_rng(4).standard_normal((3000, 2))
    yb = (np.random.default_rng(5).random(3000) < 1 / (1 + np.exp(-(1.5 * X[:, 0])))).astype(float)
    w, _ = logistic(X, yb, ridge=0.0)
    check("logistic fit recovers a known coefficient", abs(w[1] - 1.5) < 0.2 and abs(w[2]) < 0.15, f"{w[1]:.2f}, {w[2]:.2f}")

    # 5. the live DJ's history is untouched
    check("live_history.json untouched", (hist.read_bytes() if hist.exists() else None) == hist_before)
    print("ALL PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("render")
    p.add_argument("library", type=Path)
    p.add_argument("experiment", choices=sorted(EXPERIMENTS))
    p.add_argument("--pairs", type=int)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("serve")
    p.add_argument("library", type=Path)
    p.add_argument("--experiment", action="append")
    p.add_argument("--port", type=int, default=PORT)
    for name in ("report", "fit"):
        p = sub.add_parser(name)
        p.add_argument("library", type=Path)
        p.add_argument("--votes")
    p = sub.add_parser("selftest")
    p.add_argument("library", type=Path)
    args = ap.parse_args(argv)
    if getattr(args, "port", None) == 8765:
        ap.error("8765 is the live DJ's port")
    return {"render": cmd_render, "serve": cmd_serve, "report": cmd_report, "fit": cmd_fit, "selftest": selftest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
