"""What the browser plays: a join (algo_rhythm.joins) turned into "plays" it can schedule with the Web Audio API.

The browser plays the songs from the bucket and mixes them; the overlap of each join is the one piece the server
renders for it (an asset): matching the beats there takes a pitch-kept time stretch (rubberband), which the Web Audio
API cannot do (a playback-rate change moves the pitch with the tempo). The MC's spoken line is an asset too. Times are music time: seconds since the party's first song started,
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

Accuracy: the bodies are the server's arithmetic on the song files, sample for sample; the overlap is the server's
audio through the canonical MP3 (media). render() below is a reference player with the browser's rules, used by the
tests to check a recipe against the server's render.
"""
import numpy as np
from scipy.signal import lfilter

from algo_rhythm import dj, joins, mc

SR = dj.OUT_SR
VERSION = 2
T_DP = 9         # decimals kept for times (1 ns: the read position stays exact to 1/20000 of a sample); gains keep 5
GRID_S = 0.02    # gain curves are sampled this finely, plus their exact corners, then simplified
TOL = 0.002      # ...keeping every point a straight line between its neighbours would miss by more than this
PAD_S = 0.1     # audio either side of a join's overlap in its asset, not played
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


def overlap_play(j, J, asset):
    """The join's overlap as the server rendered it (beats matched by a pitch-kept stretch, the bass swap, the vocal
    gates, an echo-out and its fill), one asset played at normal speed from music time J. The asset carries PAD_S of
    the audio either side, which is not played: an MP3 smears its first and last few milliseconds. Scaled into [-1, 1]
    for the MP3 and back up by the play's gain: the soft limiter after the mix sees the server's levels."""
    pad = int(PAD_S * SR)
    pre = j.before[max(0, len(j.before) - pad):]
    x = np.concatenate([pre, j.overlap, j.nxt.audio[j.nxt_skip:j.nxt_skip + pad]])
    peak = float(np.abs(x).max()) if len(x) else 0.0
    scale = 1.0 if peak <= 0.95 else 0.95 / peak
    url = asset("join", x * scale)
    L = len(j.overlap) / SR
    return {"stem": "asset", "url": url, "at": round(J, T_DP), "end": round(J + L, T_DP),
            "src": [[round(J, T_DP), round(len(pre) / SR, T_DP)]], "gain": [[round(J, T_DP), round(1 / scale, 5)]]}, L


def hold_play(ex, start, at):
    """An echo-out keeps the incoming singer out until a line starts (p["entry"]'s hold, excerpt seconds); where that
    reaches into the body, its vocals play again with the opposite sign until the voice is let in."""
    hold = (ex.p.get("entry") or [0, 0, None])[2]
    t0 = start / SR
    if hold is None or ex.voc is None or hold + joins.live.ENTRY_FADE_S <= t0:
        return None
    t1 = hold + joins.live.ENTRY_FADE_S
    g = lambda x: (np.clip((t0 + x - hold) / joins.live.ENTRY_FADE_S, 0, 1) - 1) * joins.gain_at(ex.p, t0 + x)
    corners = [hold - t0, t1 - t0] + [c - t0 for c in joins.gain_corners(ex.p)]
    return _play(ex.p["file"], "vocals", at, at + t1 - t0, [[at, ex.src0 + t0]], _curve(at, at + t1 - t0, g, corners))


def mc_play(at, voice_s, url, gain):
    """The MC's line at music time `at` and the duck it puts on the music."""
    play = {"stem": "asset", "role": "mc", "url": url, "at": round(at, T_DP), "end": round(at + voice_s, T_DP),
            "src": [[round(at, T_DP), 0.0]], "gain": [[round(at, T_DP), round(gain, 5)]]}
    return play, [[round(at + a, T_DP), round(g, 5)] for a, g in mc.duck_points(0.0, voice_s)]


def join_item(a, j, J, asset=lambda kind, audio: None, mc_line=None):
    """Everything the browser needs for the song j.nxt coming in at music time J: the join (the server's overlap), then
    the incoming song from j.nxt_skip. mc_line: (seconds after J, voice audio, voice gain) or None. asset(kind, audio)
    stores audio, returning its url."""
    play, L = overlap_play(j, J, asset)
    plays, duck = [play], []
    held = hold_play(j.nxt, j.nxt_skip, J + L)
    if held:
        plays.append(held)
    if mc_line:
        at, voice, gain = mc_line
        mplay, duck = mc_play(J + at, len(voice) / SR, asset("mc", voice), gain)
        plays.append(mplay)
    return {"v": VERSION, "start_s": round(J, T_DP), "overlap_s": round(L, T_DP), "file": j.nxt.p["file"],
            "join": {"type": j.plan["kind"], "plays": plays, "duck": duck}, "body": body(j.nxt, j.nxt_skip, J + L)}


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
