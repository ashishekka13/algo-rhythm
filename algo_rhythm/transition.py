"""The plan's transition algorithm (PLAN.html, section 09).

  1. Choose the point   32 beats of overlap: the outgoing excerpt's last 32
                        beats under the incoming excerpt's first 32.
  2. Match tempo        rubberband time-stretch. The master tempo ramps from
                        the outgoing BPM to the incoming BPM across the overlap.
  3. Align phase        each beat of both songs is mapped onto the same output
                        slot, then the incoming song is nudged by the measured
                        onset offset so the beats land together.
  4. Gain               equal-power (cos/sin) crossfade over the 32 beats.
  5. Bass swap          highpass on the outgoing song sweeps 20 -> 250 Hz over
                        the first 8 beats, so two basslines never play at once.
  6. Recover            the incoming song carries on alone at its own tempo.

Fallback: when tempos are too far apart (or the keys clash), the caller asks
for an echo-out instead: the outgoing song's last beat repeats, quieter and
thinner each time, while the incoming song starts on its first beat. The plan
suggests a hard cut here; the echo-out keeps the same "intentional" feel
without dead air.

Songs at double/half tempo of each other (e.g. 160 vs 80) blend by pairing two
beats of the faster song with one beat of the slower.
"""
import librosa
import numpy as np
import pyrubberband as pyrb
from scipy.signal import butter, sosfilt

OVERLAP_BEATS = 32
BASS_SWAP_BEATS = 8
ECHO_REPEATS, ECHO_FEEDBACK = 4, 0.5
SEAM_FADE_S = 0.005  # fade in/out of each echo repeat


def overlap_slots(tail_beats, head_beats):
    """Output beat boundaries for the overlap, with the tempo ramping from A's to B's."""
    bpm_a = 60 / np.diff(tail_beats).mean()
    bpm_b = 60 / np.diff(head_beats).mean()
    tempi = np.linspace(bpm_a, bpm_b, len(tail_beats) - 1)
    return np.concatenate([[0.0], np.cumsum(60 / tempi)]), float(bpm_a), float(bpm_b)


def warp(audio, beats, slots, sr):
    """Stretch `audio` so its beat k lands exactly on output slot k (steps 2 and 3)."""
    src = np.round((beats - beats[0]) * sr).astype(int)
    dst = np.round(slots * sr).astype(int)
    src[-1] = min(src[-1], len(audio))  # a one-sample rounding gap must not crash the stretcher
    clip = audio[: src[-1]]
    # crispness 6: measured on drum-heavy songs it keeps the highs (-0.5 dB vs -1.0 dB) and the hits sharp;
    # the "finer" R3 engine measured duller (-2 dB highs, smeared hits) and 5-10x slower.
    out = pyrb.timemap_stretch(clip, sr, list(zip(src, dst)), rbargs={"--crisp": "6"})
    if len(out) < dst[-1]:
        out = np.pad(out, ((0, dst[-1] - len(out)), (0, 0)))
    return out[: dst[-1]]


def snap_beats(audio, beats, sr, max_ms=35, hop=128):
    """Move each beat onto the drum hit next to it (within max_ms). Tag grids are smoothed and can sit 20-40 ms off
    the real hits in songs played with a human feel; warping hit-to-hit keeps the two songs locked for the whole blend."""
    low = librosa.onset.onset_strength(y=librosa.to_mono(audio.T), sr=sr, hop_length=hop, n_mels=32, fmax=2000)
    lim, out = int(max_ms / 1000 * sr / hop), beats.copy()
    floor = 1.5 * np.median(low)
    for k in range(1, len(beats) - 1):  # the first and last edges stay: they are the cut points
        c = int(round(beats[k] * sr / hop))
        lo, hi = max(0, c - lim), min(len(low), c + lim + 1)
        if hi > lo and low[lo:hi].max() > floor:
            out[k] = (lo + int(np.argmax(low[lo:hi]))) * hop / sr
    ok = np.all(np.diff(out) > 0.5 * np.median(np.diff(beats)))
    return out if ok else beats  # a snap that would squeeze two beats together is a wrong hit: keep the grid


def phase_offset(a, b, sr, max_ms=60, hop=64):
    """Samples to delay `b` by so its onsets line up with `a`'s (~1.5 ms resolution).

    The beat grids alone leave 15-30 ms of error, well over the plan's 8 ms.
    """
    ea = librosa.onset.onset_strength(y=librosa.to_mono(a.T), sr=sr, hop_length=hop)
    eb = librosa.onset.onset_strength(y=librosa.to_mono(b.T), sr=sr, hop_length=hop)
    n = min(len(ea), len(eb))
    ea, eb = ea[:n] - ea[:n].mean(), eb[:n] - eb[:n].mean()
    lim = int(max_ms / 1000 * sr / hop)
    lags = np.arange(-lim, lim + 1)
    xc = [np.dot(ea[max(0, l) : n + min(0, l)], eb[max(0, -l) : n - max(0, l)]) for l in lags]
    return int(lags[int(np.argmax(xc))] * hop)


def grid_slip_ms(x, slots, sr, win_s=4.0):
    """How far `x` wanders off the beat grid `slots` across the overlap (spread of its 4 s window offsets from a click
    at each slot). The warp puts every tagged beat on its slot, so a large slip means the tag's beat list itself is
    wrong there (a missed or extra beat, a jump to the off-beat): measured 60-180 ms in those songs, under 15 ms otherwise."""
    clk = np.zeros((len(x), 1))
    for t in slots[:-1]:
        k = int(t * sr)
        clk[k:k + 64] = 1.0
    w = int(win_s * sr)
    offs = [phase_offset(x[k:k + w], clk[k:k + w], sr, max_ms=90) / sr * 1000 for k in range(0, len(x) - w, w // 2)]
    return float(np.ptp(offs)) if offs else 0.0


def shift(x, samples):
    """Delay (positive) or advance (negative) `x`, keeping its length."""
    if samples > 0:
        return np.concatenate([np.zeros((samples, x.shape[1])), x[:-samples]])
    if samples < 0:
        return np.concatenate([x[-samples:], np.zeros((-samples, x.shape[1]))])
    return x


def sweeping_highpass(x, cutoff_at, sr, block=512):
    """Highpass whose cutoff changes block by block; filter state carries across blocks."""
    out = np.empty_like(x)
    zi = np.zeros((1, 2, x.shape[1]))
    for i in range(0, len(x), block):
        sos = butter(2, cutoff_at(i / sr), "highpass", fs=sr, output="sos")
        out[i : i + block], zi = sosfilt(sos, x[i : i + block], axis=0, zi=zi)
    return out


def blend(a, b, slots, sr):
    """Equal-power crossfade with the bass swap (steps 4 and 5)."""
    t = np.linspace(0, 1, len(a))[:, None]
    swap_end = slots[BASS_SWAP_BEATS]

    def cutoff(sec):  # log sweep 20 -> 250 Hz, then hold at 250
        return 20 * (250 / 20) ** min(1.0, sec / swap_end)

    a = sweeping_highpass(a, cutoff, sr)
    return a * np.cos(np.pi / 2 * t) + b * np.sin(np.pi / 2 * t)


def swap_gains(n):
    """Vocal swap without stems timing: the outgoing voice is gone after the first quarter of the overlap, the
    incoming one arrives after the middle, so the quarter in between is instrumental only."""
    t = np.linspace(0, 1, n)
    return np.clip(1 - t / 0.25, 0, 1), np.clip((t - 0.5) / 0.25, 0, 1)


def _levels(v, sr, hop_s=0.05):
    h = int(hop_s * sr)
    m = v.mean(axis=1) if v.ndim > 1 else v
    return np.array([20 * np.log10(np.sqrt(np.mean(m[k:k + h] ** 2)) + 1e-9) for k in range(0, len(m), h)]), h


VOX_HOP = 0.02      # vocal envelope step (s)
LINE_GAP_S = 0.3    # a silence this long between two sung segments is a gap between lines
CUT_GUARD_S = 0.4   # never cut a voice within this long before a line starts (the breath belongs to that line)
LEAD_S = 0.05       # let an incoming line in this much before its detected onset, so its first consonant is whole


def vocal_ref_db(v, sr):
    """The voice's own loud-singing level: 95th percentile of its 20 ms levels (above -60 dB)."""
    db = _vocal_db(v, sr)
    return float(np.percentile(db[db > -60], 95)) if (db > -60).any() else -20.0


def _vocal_db(v, sr):
    m = v.mean(axis=1) if v.ndim > 1 else v
    h = int(VOX_HOP * sr)
    n = len(m) // h
    return 20 * np.log10(np.sqrt((m[:n * h].reshape(n, h) ** 2).mean(axis=1)) + 1e-9)


def sung_segments(v, sr, ref=None, open_rel=-18, close_rel=-26, merge_s=0.15, min_s=0.12):
    """Where the voice sings, as (start, end) seconds, from a 20 ms envelope of the vocal stem. Hysteresis: a segment
    opens above ref+open_rel and closes only below ref+close_rel, so a dip inside a held note is not a gap; dips under
    merge_s are joined, blips under min_s dropped. ref = the voice's loud level (default: measured on v itself)."""
    db = _vocal_db(v, sr)
    ref = vocal_ref_db(v, sr) if ref is None else ref
    segs, on, start = [], False, 0
    for k, x in enumerate(db):
        if not on and x > ref + open_rel:
            on, start = True, k
        elif on and x < ref + close_rel:
            on = False
            segs.append([start, k])
    if on:
        segs.append([start, len(db)])
    out = []
    for a, b in segs:
        if out and (a - out[-1][1]) * VOX_HOP < merge_s:
            out[-1][1] = b
        else:
            out.append([a, b])
    return [(a * VOX_HOP, b * VOX_HOP) for a, b in out if (b - a) * VOX_HOP >= min_s]


def clean_cut(segs, t, guard_s=CUT_GUARD_S):
    """Can a voice be cut at t without chopping a word? Not inside a sung segment, and no line starting within
    guard_s after t."""
    return not any(a < t < b for a, b in segs) and not any(t < a <= t + guard_s for a, _ in segs)


def line_starts(segs, gap_s=LINE_GAP_S):
    """Onsets of segments that follow a real gap (or open the excerpt): where a line begins."""
    return [a for k, (a, _) in enumerate(segs) if k == 0 or a - segs[k - 1][1] >= gap_s]


GATE_FADE_S = 0.04  # a voice is cut or let in over this long


def line_swap_gains(av, bv, sr, fade_s=GATE_FADE_S, ref_a=None, ref_b=None):
    """Vocal swap timed to the words, the way a DJ rides the vocal: the outgoing voice is cut where a line has ended
    and none starts within CUT_GUARD_S (never mid-word, never into the breath before a line), and the incoming voice
    is let in at full level just before one of its lines starts, so no lyric is faded in or out.
    Returns (g_out, g_in, info); info says when a mid-line cut could not be avoided."""
    sa, sb = sung_segments(av, sr, ref_a), sung_segments(bv, sr, ref_b)
    dur, limit = len(av) / sr, 0.6 * len(av) / sr  # the outgoing voice must be gone by 60% of the overlap
    cuts = ([0.0] if clean_cut(sa, 0.0) else []) + [b + 0.05 for a, b in sa if clean_cut(sa, b + 0.05)]
    cuts = [c for c in cuts if c <= limit]
    out_midline = not cuts
    if cuts:
        t_out = cuts[0]
    else:  # no clean cut: the softest moment (between words), never simply at the start
        db, h = _levels(av, sr)
        k = int(limit / (h / sr))
        soft = np.convolve(db[:k], np.ones(3) / 3, "same")
        t_out = (int(np.argmin(soft[2:])) + 2) * h / sr if k > 4 else 0.0
    # the incoming voice starts at the first line that begins after the outgoing one has gone
    starts = [a for a in line_starts(sb) if a >= t_out]
    t_in = max(t_out, starts[0] - LEAD_S) if starts else None
    in_midline = t_in is None and any(b > t_out for a, b in sb)  # singing through, with no line start in the overlap
    t = np.arange(len(av)) / sr
    ramp = lambda at: np.clip((t - at) / fade_s, 0, 1)
    g_out = 1 - ramp(t_out)
    t_gate_in = t_in if t_in is not None else (dur if not in_midline else t_out)
    g_in = ramp(t_gate_in)
    return g_out, g_in, {"vocal_out_at_s": round(t_out, 2), "vocal_in_at_s": round(t_in, 2) if t_in is not None else None,
                         "out_midline": out_midline, "in_midline": bool(in_midline),
                         "vocal_out_at_s_exact": float(t_out), "vocal_in_at_s_exact": float(t_gate_in)}


def vocal_overlap_s(av, bv, g_out, g_in, sr, floor_db=-32, follow=True):
    """Seconds in which both voices are audible at once (each above floor_db). follow=True: the voices also follow
    the crossfade (a plain blend); False: they only follow their swap gains (blend_swap)."""
    t = np.linspace(0, 1, len(av))
    out = av.mean(axis=1) * g_out * (np.cos(np.pi / 2 * t) if follow else 1)
    inc = bv.mean(axis=1) * g_in * (np.sin(np.pi / 2 * t) if follow else 1)
    db = lambda x: 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)
    w = sr // 2
    return 0.5 * sum(db(out[k:k + w]) > floor_db and db(inc[k:k + w]) > floor_db for k in range(0, len(av) - w, w))


def blend_swap(a, b, av, bv, slots, sr, g_out=None, g_in=None, instrumentals=False):
    """The blend, with the vocal swap. a, b are full mixes and av, bv their vocals; instrumental = mix - vocals.
    The instrumentals crossfade as usual; the voices do not follow the crossfade: each is at full level until it is
    cut (outgoing) or from the moment it comes in (incoming), so words are never faded.
    instrumentals=True: a and b are already the instrumentals. Use it when they were time-stretched: a stretched mix
    minus a separately stretched stem is not an instrumental (measured: the leftover was louder than the voice)."""
    if instrumentals:
        a, b = a + av, b + bv
    t = np.linspace(0, 1, len(a))[:, None]
    swap_end = slots[BASS_SWAP_BEATS]
    if g_out is None:
        g_out, g_in = swap_gains(len(a))
    g_out, g_in = g_out[:, None], g_in[:, None]

    def cutoff(sec):
        return 20 * (250 / 20) ** min(1.0, sec / swap_end)

    out = sweeping_highpass(a - av, cutoff, sr) * np.cos(np.pi / 2 * t)  # same bass swap as `blend`
    inc = (b - bv) * np.sin(np.pi / 2 * t)
    return out + av * g_out + inc + bv * g_in


def highpass(x, hz, sr):
    return sosfilt(butter(2, hz, "highpass", fs=sr, output="sos"), x, axis=0)


def echo_tail(a, period, sr, repeats=ECHO_REPEATS, feedback=ECHO_FEEDBACK, src_period=None):
    """Repeat the final beat every `period` s, each repeat quieter and thinner, so the bass leaves first.
    src_period: that beat's own length when the repeats follow the incoming song's grid instead; the beat is
    time-stretched to `period`, so each repeat's hit lands on an incoming beat. (Cutting or padding it left a hole or a
    clipped hit in every repeat: measured 100% digital silence in the 20 ms before each seam for a 10% slower song.)"""
    n = int(period * sr)
    src = a[-int((src_period or period) * sr):]
    if src_period and abs(src_period - period) > 1e-3:
        src = pyrb.time_stretch(src, sr, src_period / period)
    src = np.pad(src[:n], ((0, max(0, n - len(src))), (0, 0)))
    edge = min(int(SEAM_FADE_S * sr), n // 4)  # each repeat starts and ends at zero: no click where repeats meet
    ramp = np.linspace(0, 1, edge)[:, None]
    src = src.copy()
    src[:edge] *= ramp
    src[n - edge:] *= ramp[::-1]
    tail = np.zeros((n * repeats, a.shape[1]))
    for k in range(1, repeats + 1):
        tail[(k - 1) * n : k * n] += feedback**k * highpass(src, min(4000, 250 * 1.6 ** (k - 1)), sr)
    return tail * np.linspace(1, 0, len(tail))[:, None]


def beat_steps(a_beats, b_beats):
    """(step_a, step_b): use every 2nd beat of a song running at double the other's tempo."""
    ratio = np.diff(a_beats).mean() / np.diff(b_beats).mean()  # period ratio = bpm_b / bpm_a
    if ratio > 1.5:   # b is about twice as fast
        return 1, 2
    if ratio < 1 / 1.5:
        return 2, 1
    return 1, 1


def transition(a, a_beats, b, b_beats, sr, kind="blend", a_voc=None, b_voc=None, overlap_beats=OVERLAP_BEATS, plan=None):
    """Join excerpt `a` into excerpt `b` with a "blend" (the plan) or an "echo" out.

    `a_beats` / `b_beats` are beat boundaries in seconds from each excerpt's
    start; the last entry of each is the excerpt's end.

    `a_voc` / `b_voc` (optional) are the vocals of each excerpt (same length and timing). When both are given the
    blend does a vocal swap so two voices never sing together.

    Returns (a_before, overlap, b_skip_samples, info): play `a_before`, then
    `overlap`, then `b` from `b_skip_samples` onward.

    plan: a dict that gets what the web player needs to make the same blend itself (algo_rhythm.recipe): where the
    overlap starts in `a` (a0, samples), the beat maps (a: tail seconds of `a`, b: head seconds of `b`, both onto
    slots, seconds into the overlap), b's nudge (samples, + = later), the bass-swap length and the voice gates.
    """
    step_a, step_b = beat_steps(a_beats, b_beats)
    tail = a_beats[-(overlap_beats * step_a + 1)::step_a]
    head = b_beats[: overlap_beats * step_b + 1 : step_b]
    if kind == "blend" and (len(tail) < overlap_beats + 1 or len(head) < overlap_beats + 1):
        kind = "echo"  # excerpt too short to hold the overlap at this tempo ratio

    if kind == "echo":
        p_a, p_b = float(np.median(np.diff(a_beats))), float(np.median(np.diff(b_beats)))
        tail_audio = echo_tail(a, p_b * step_b / step_a, sr, src_period=p_a)  # repeats on b's grid: b starts under them
        n = min(len(tail_audio), len(b))
        overlap = b[:n] + tail_audio[:n]
        return a, overlap, n, {"type": "echo-out"}

    tail = tail[0] + snap_beats(a[int(round(tail[0] * sr)):int(round(tail[-1] * sr))], tail - tail[0], sr)
    head = snap_beats(b[:int(round(head[-1] * sr))], head, sr)
    slots, bpm_a, bpm_b = overlap_slots(tail, head)
    info = {"bpm_out": round(bpm_a * step_a, 1), "bpm_in": round(bpm_b * step_b, 1),
            "beat_pairing": f"{step_a}:{step_b}"}

    a0 = int(round(tail[0] * sr))
    a_w = warp(a[a0:], tail, slots, sr)
    b_w = warp(b, head, slots, sr)
    nudge = phase_offset(a_w, b_w, sr)
    b_w = shift(b_w, nudge)
    info.update(type="blend",
                overlap_s=round(float(slots[-1]), 1),
                grid_slip_ms=round(max(grid_slip_ms(a_w, slots, sr), grid_slip_ms(b_w, slots, sr)), 1),
                phase_nudge_ms=round(nudge / sr * 1000, 1))
    if a_voc is not None and b_voc is not None:
        av_w, bv_w = warp(a_voc[a0:], tail, slots, sr), shift(warp(b_voc, head, slots, sr), nudge)
        g_out, g_in, lines = line_swap_gains(av_w, bv_w, sr, ref_a=vocal_ref_db(a_voc, sr), ref_b=vocal_ref_db(b_voc, sr))
        info.update(vocal_swap=True, **lines,
                    vocal_overlap_s=vocal_overlap_s(av_w, bv_w, g_out, g_in, sr, follow=False),
                    vocal_overlap_s_without=vocal_overlap_s(av_w, bv_w, np.ones(len(av_w)), np.ones(len(av_w)), sr))
        # each instrumental is stretched once, as itself (the browser subtracts before it changes the speed, too)
        ai_w = warp(a[a0:] - a_voc[a0:], tail, slots, sr)
        bi_w = shift(warp(b - b_voc, head, slots, sr), nudge)
        mixed = blend_swap(ai_w, bi_w, av_w, bv_w, slots, sr, g_out, g_in, instrumentals=True)
    else:
        mixed = blend(a_w, b_w, slots, sr)
    if plan is not None:
        plan.update(kind="blend", a0=a0, tail=[float(x) for x in tail], head=[float(x) for x in head],
                    slots=[float(x) for x in slots], nudge=int(nudge), swap_end_s=float(slots[BASS_SWAP_BEATS]),
                    n=len(mixed), b_skip=int(round(head[-1] * sr)),
                    gates=None if not info.get("vocal_swap") else {
                        "out_s": info["vocal_out_at_s_exact"], "in_s": info["vocal_in_at_s_exact"], "fade_s": GATE_FADE_S})
    info.pop("vocal_out_at_s_exact", None)
    info.pop("vocal_in_at_s_exact", None)
    return a[:a0], mixed, int(round(head[-1] * sr)), info
