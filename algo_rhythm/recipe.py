"""What the browser plays: a join (algo_rhythm.joins) turned into "plays" it can schedule with the Web Audio API.

The browser mixes; nothing here makes audio for it except two kinds of short assets the server must make anyway (the
MC's spoken line and a synthesised drum fill). Times are music time: seconds since the party's first song started,
not counting pauses (the host page keeps that clock).

A play is one source node:
    {"file": song file, "stem": "mix" | "vocals" | "asset", "url": asset url (stem "asset" only),
     "at": music time it starts, "end": music time it stops (null: until the next join takes over),
     "src": [[t, s], ...]   song time s (seconds into the file) at music time t, linear between points; after the last
                            point it goes on at the last slope (1.0 for a single point). The slope is the playback rate.
     "gain": [[t, g], ...]  linear between points, held flat before the first and after the last,
     "hp": [[t, hz], ...]   optional highpass (Butterworth, 12 dB/octave), exponential between points}
A join adds "duck": [[t, g], ...], a gain on all the music (not on a play with "role": "mc", the MC's line), linear
between points.

Accuracy: a blend's beat warp is a playback-rate change (the pitch moves with it, up to the 10% tempo gate), where
the server renders it with rubberband (pitch kept). Everything else is the same arithmetic as the server's render;
render() below is a reference player with the browser's rules, used by the tests to check a recipe against it.
"""
import numpy as np
from scipy.signal import lfilter

from algo_rhythm import dj, joins, mc, transition

SR = dj.OUT_SR
VERSION = 1
T_DP = 9         # decimals kept for times (1 ns: the read position stays exact to 1/20000 of a sample); gains keep 5
GRID_S = 0.02    # gain curves are sampled this finely, plus their exact corners, then simplified
TOL = 0.002      # ...keeping every point a straight line between its neighbours would miss by more than this
BLOCK = 128      # the Web Audio render quantum: filter frequencies change once per block


def _simplify(pts, tol=TOL):
    """Drop points that lie (within tol) on the straight line between the points kept either side of them."""
    if len(pts) <= 2:
        return pts
    t, g = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
    keep = np.zeros(len(t), bool)
    keep[[0, -1]] = True
    stack = [(0, len(t) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = g[i] + (g[j] - g[i]) * (t[i + 1:j] - t[i]) / max(t[j] - t[i], 1e-12)
        err = np.abs(g[i + 1:j] - seg)
        k = int(np.argmax(err))
        if err[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack += [(i, m), (m, j)]
    return [[round(float(t[k]), T_DP), round(float(g[k]), 5)] for k in np.flatnonzero(keep)]


def _curve(t0, t1, fn, corners=()):
    """fn(t) (t: seconds from t0, numpy) sampled from 0 to t1 - t0 on the grid and at the corners, as points in music
    time, simplified."""
    dur = max(t1 - t0, 0.0)
    ts = np.unique(np.concatenate([np.arange(0, dur, GRID_S), [dur], [c for c in corners if 0 <= c <= dur]]))
    return _simplify([[t0 + float(x), float(v)] for x, v in zip(ts, fn(ts))])


def _play(file, stem, at, end, src, gain, hp=None):
    p = {"file": file, "stem": stem, "at": round(at, T_DP), "end": None if end is None else round(end, T_DP),
         "src": [[round(a, T_DP), round(b, T_DP)] for a, b in src], "gain": gain}
    if hp:
        p["hp"] = hp
    return p


def body(ex, start, at, end=None):
    """Excerpt `ex` from its sample `start` onward at normal speed, starting at music time `at`."""
    t0 = start / SR
    stop = len(ex.audio) / SR  # the excerpt's end: the next join normally takes over before it
    dur = stop - t0
    gain = _curve(at, at + dur, lambda x: joins.gain_at(ex.p, t0 + x), [c - t0 for c in joins.gain_corners(ex.p)])
    return _play(ex.p["file"], "mix", at, at + dur if end is None else end, [[at, ex.src0 + t0]], gain)


def start_item(ex):
    """The opening song: its excerpt from the top at music time 0."""
    return {"v": VERSION, "start_s": 0.0, "overlap_s": 0.0, "join": None, "body": body(ex, 0, 0.0),
            "file": ex.p["file"]}


def _sweep(swap_end):
    """transition.blend's bass swap: highpass 20 -> 250 Hz on a log scale over swap_end seconds, then held."""
    return lambda x: 20 * (250 / 20) ** np.clip(x / swap_end, 0, 1)


def blend_plays(a, j, J):
    """Plays for a blend from excerpt a into j.nxt starting at music time J (transition.transition's arithmetic)."""
    p, b = j.plan, j.nxt
    L = p["n"] / SR
    slots, tail, head = np.array(p["slots"]), np.array(p["tail"]), np.array(p["head"])
    nudge = p["nudge"] / SR
    t = lambda x: np.asarray(x, float)
    xf = lambda x: np.clip(t(x) / L, 0, 1) * np.pi / 2           # the equal-power crossfade's angle
    lift_pts = p.get("lift")
    lift = (lambda x: 10 ** (np.interp(t(x), [q[0] for q in lift_pts], [q[1] for q in lift_pts]) / 20)) \
        if lift_pts else (lambda x: np.ones_like(t(x)))
    a_src = lambda x: np.interp(t(x), slots, tail)                # excerpt seconds of a at overlap time x
    b_lo, b_hi = max(0.0, nudge), min(L, slots[-1] + nudge)       # b is delayed (or advanced) by the nudge, then cut to L
    b_src = lambda x: np.interp(t(x) - nudge, slots, head - head[0])
    env_a = lambda x: joins.gain_at(a.p, a_src(x))
    env_b = lambda x: joins.gain_at(b.p, b_src(x))
    sw = _sweep(p["swap_end_s"])
    hp = _curve(J, J + L, sw, corners=[p["swap_end_s"]])
    corners = list(slots) + [float(np.interp(c, head - head[0], slots)) + nudge for c in joins.gain_corners(b.p)]
    a_map = [[J + s, a.src0 + tt] for s, tt in zip(slots, tail)]
    b_map = [[J + s + nudge, b.src0 + h - head[0]] for s, h in zip(slots, head) if b_lo - 1e-9 <= s + nudge <= b_hi + 1e-9]
    if nudge < 0:  # b starts part-way into its first beat
        b_map = [[J, b.src0 + float(b_src(0.0))]] + b_map
    fa, fb = a.p["file"], b.p["file"]
    g = p.get("gates")
    plays = [_play(fa, "mix", J, J + L, a_map, _curve(J, J + L, lambda x: env_a(x) * np.cos(xf(x)) * lift(x), corners), hp)]
    if g:
        fade = g["fade_s"]
        g_out = lambda x: 1 - np.clip((t(x) - g["out_s"]) / fade, 0, 1)
        g_in = lambda x: np.clip((t(x) - g["in_s"]) / fade, 0, 1)
        gc = corners + [g["out_s"], g["out_s"] + fade, g["in_s"], g["in_s"] + fade]
        # instrumental = mix - vocals: a's vocals go through the same highpass with the opposite sign, and come back
        # unfiltered on their own gate; b's vocals follow their gate instead of the crossfade
        plays += [_play(fa, "vocals", J, J + L, a_map, _curve(J, J + L, lambda x: -env_a(x) * np.cos(xf(x)) * lift(x), gc), hp),
                  _play(fa, "vocals", J, J + L, a_map, _curve(J, J + L, lambda x: env_a(x) * g_out(x) * lift(x), gc)),
                  _play(fb, "mix", J + b_lo, J + b_hi, b_map, _curve(J + b_lo, J + b_hi, lambda x: env_b(x + b_lo) * np.sin(xf(x + b_lo)) * lift(x + b_lo), [c - b_lo for c in corners])),
                  _play(fb, "vocals", J + b_lo, J + b_hi, b_map,
                        _curve(J + b_lo, J + b_hi, lambda x: env_b(x + b_lo) * (g_in(x + b_lo) - np.sin(xf(x + b_lo))) * lift(x + b_lo),
                               [c - b_lo for c in gc]))]
    else:
        plays.append(_play(fb, "mix", J + b_lo, J + b_hi, b_map,
                           _curve(J + b_lo, J + b_hi, lambda x: env_b(x + b_lo) * np.sin(xf(x + b_lo)), [c - b_lo for c in corners])))
    return plays, L, J + L


def echo_plays(a, j, J, asset):
    """Plays for an echo-out from excerpt a into j.nxt at music time J (live.echo_out and transition.echo_tail)."""
    p, b = j.plan, j.nxt
    n_rep, n_src = int(p["rep_s"] * SR), int(p["src_period_s"] * SR)
    edge = min(int(transition.SEAM_FADE_S * SR), n_rep // 4) / SR
    total = p["repeats"] * n_rep / SR
    L = p["n"] / SR                                       # the overlap: the echo is cut where the song under it ends
    src_end = a.src0 + p["cut"] / SR                      # song time where a stops
    rate = (n_src / SR) / (n_rep / SR)
    env = float(joins.gain_at(a.p, np.array([p["cut"] / SR - 0.001]))[0])
    fa = a.p["file"]
    plays = []
    for k in range(1, p["repeats"] + 1):
        t0, t1 = J + (k - 1) * n_rep / SR, J + k * n_rep / SR
        if t0 >= J + L:
            break
        t1 = min(t1, J + L)
        hz = min(4000, 250 * 1.6 ** (k - 1))
        fb_k = p["feedback"] ** k
        fade = lambda x, t0=t0: 1 - (x + t0 - J) / total
        edges = lambda x: np.clip(np.minimum(x / edge, (n_rep / SR - x) / edge), 0, 1)
        gain = lambda x, fade=fade, fb_k=fb_k: env * fb_k * fade(x) * edges(x)
        rate_pts = [[t0, src_end - n_src / SR], [t0 + n_rep / SR, src_end - n_src / SR + rate * n_rep / SR]]
        corners = [edge, n_rep / SR - edge]
        plays.append(_play(fa, "mix", t0, t1, rate_pts, _curve(t0, t1, gain, corners), [[t0, hz]]))
        if p["music_only"]:
            plays.append(_play(fa, "vocals", t0, t1, rate_pts, _curve(t0, t1, lambda x, g=gain: -g(x), corners), [[t0, hz]]))
    delay = j.plan.get("fill_delay", 0) / SR
    if "fill" in j.plan:
        url = asset("fill", j.plan["fill"])
        plays.append({"stem": "asset", "url": url, "at": round(J, T_DP), "end": round(J + len(j.plan["fill"]) / SR, T_DP),
                      "src": [[round(J, T_DP), 0.0]], "gain": [[round(J, T_DP), 1.0]]})
    hold = (p.get("entry") or [0, 0, None])[2]
    if hold is not None and b.voc is not None:  # the incoming singer is held back until a line starts
        g = lambda x: (np.clip((x - hold) / joins.live.ENTRY_FADE_S, 0, 1) - 1) * joins.gain_at(b.p, x)
        end = J + delay + hold + joins.live.ENTRY_FADE_S
        plays.append(_play(b.p["file"], "vocals", J + delay, end, [[J + delay, b.src0]],
                           _curve(J + delay, end, g, [hold, hold + joins.live.ENTRY_FADE_S] + joins.gain_corners(b.p))))
    return plays, L, J + delay  # the incoming song runs on its own clock from the cut: its body starts there


def mc_play(at, voice_s, url, gain):
    """The MC's line at music time `at` and the duck it puts on the music."""
    play = {"stem": "asset", "role": "mc", "url": url, "at": round(at, T_DP), "end": round(at + voice_s, T_DP),
            "src": [[round(at, T_DP), 0.0]], "gain": [[round(at, T_DP), round(gain, 5)]]}
    return play, [[round(at + a, T_DP), round(g, 5)] for a, g in mc.duck_points(0.0, voice_s)]


def join_item(a, j, J, asset=lambda kind, audio: None, mc_line=None):
    """Everything the browser needs for the song j.nxt coming in at music time J: the join's plays, then its body.
    mc_line: (seconds after J, voice audio, voice gain) or None. asset(kind, audio) stores audio, returning its url."""
    if j.plan["kind"] == "blend":
        plays, L, body_at = blend_plays(a, j, J)
        start = j.plan["b_skip"]
    else:
        plays, L, body_at = echo_plays(a, j, J, asset)
        start = 0
    duck = []
    if mc_line:
        at, voice, gain = mc_line
        play, duck = mc_play(J + at, len(voice) / SR, asset("mc", voice), gain)
        plays.append(play)
    return {"v": VERSION, "start_s": round(J, T_DP), "overlap_s": round(L, T_DP), "file": j.nxt.p["file"],
            "join": {"type": j.plan["kind"], "plays": plays, "duck": duck}, "body": body(j.nxt, start, body_at)}


def next_join_at(item, nxt_skip, a0):
    """Music time where the next join starts: this song's body reaches excerpt sample a0 (before[:a0] is all of it
    that plays before the join) having started at excerpt sample nxt_skip at the body's own start."""
    return item["body"]["at"] + (a0 - nxt_skip) / SR


# ---------------------------------------------------------------- reference player (tests, clips)

def _biquad_hp(hz):
    """RBJ cookbook highpass with Q = 1/sqrt(2): the Web Audio BiquadFilterNode (Q -3.01 dB) and scipy's butter(2)."""
    w = 2 * np.pi * hz / SR
    alpha = np.sin(w) / (2 / np.sqrt(2))
    cw = np.cos(w)
    b = np.array([(1 + cw) / 2, -(1 + cw), (1 + cw) / 2])
    a = np.array([1 + alpha, -2 * cw, 1 - alpha])
    return b / a[0], a / a[0]


def _interp_exp(t, pts):
    """Exponential interpolation between points (filter frequencies), held flat outside them."""
    ts, vs = np.array([q[0] for q in pts]), np.log(np.array([q[1] for q in pts]))
    return np.exp(np.interp(t, ts, vs))


def render(plays, t0, t1, audio, duck=()):
    """The browser's arithmetic in numpy: mix `plays` from music time t0 to t1. audio(file, stem) -> (frames, 2) array at
    SR; audio(url, "asset") for assets. Linear interpolation between samples; filters change every BLOCK samples."""
    n = int(round((t1 - t0) * SR))
    t = t0 + np.arange(n) / SR
    out, voice = np.zeros((n, 2)), np.zeros((n, 2))
    for p in plays:
        end = p["end"] if p["end"] is not None else t1
        live_ = (t >= p["at"] - 1e-9) & (t < end - 1e-9)
        if not live_.any():
            continue
        idx = np.flatnonzero(live_)
        tt = t[idx]
        src = p["src"]
        if len(src) == 1:
            s = src[0][1] + (tt - src[0][0])
        else:
            ts, ss = np.array([q[0] for q in src]), np.array([q[1] for q in src])
            slope = (ss[-1] - ss[-2]) / (ts[-1] - ts[-2])
            s = np.where(tt <= ts[-1], np.interp(tt, ts, ss), ss[-1] + (tt - ts[-1]) * slope)
        buf = audio(p["url"], "asset") if p["stem"] == "asset" else audio(p["file"], p["stem"])
        pos = s * SR
        i0 = np.clip(np.floor(pos).astype(int), 0, len(buf) - 2)
        fr = (pos - i0)[:, None]
        x = buf[i0] * (1 - fr) + buf[i0 + 1] * fr
        x[(pos < 0) | (pos >= len(buf) - 1)] = 0.0
        if p.get("hp"):
            y, zi = np.empty_like(x), np.zeros((2, 2))
            hz = _interp_exp(tt, p["hp"])
            for k in range(0, len(x), BLOCK):
                bq, aq = _biquad_hp(hz[k])
                y[k:k + BLOCK], zi = lfilter(bq, aq, x[k:k + BLOCK], axis=0, zi=zi)
            x = y
        gts, gvs = np.array([q[0] for q in p["gain"]]), np.array([q[1] for q in p["gain"]])
        (voice if p.get("role") == "mc" else out)[idx] += x * np.interp(tt, gts, gvs)[:, None]
    if duck:
        out *= np.interp(t, [q[0] for q in duck], [q[1] for q in duck])[:, None]
    return out + voice
