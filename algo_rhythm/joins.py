"""One join between two songs, worked out on the server: rendered with numpy (so it can be measured and checked) and
recorded (so algo_rhythm.recipe can tell the browser how to play the same join itself).

Both players use this: the offline engine (live.Engine, for the scripted checks) and the web server. The audio of
each excerpt is described by Excerpt.p, a small JSON-able dict, so a stateless server can rebuild exactly the audio
the previous request worked with (rebuild) instead of keeping it in memory between requests.

    a = joins.load(library, song, window)                      the opening song: the window as it is
    j = joins.render_join(library, songs, entry, a, 0, nxt_entry, alternatives, say)
    j.nxt, j.nxt_skip                                          the incoming song, and where it carries on after the join
    a2 = joins.rebuild(library, songs[i], j.nxt.p)            the same audio as j.nxt, from its description only
"""
import concurrent.futures
import copy
import json
import os

import numpy as np

from algo_rhythm import dj, filler, live, timing, transition

SR = dj.OUT_SR
_try_pool = concurrent.futures.ThreadPoolExecutor(max_workers=int(os.environ.get("TRY_THREADS", "4")),
                                                  thread_name_prefix="blend-try")


class Excerpt:
    """The part of a song that plays, as the mixer sees it: audio and vocals (stereo, SR), beat times (seconds from
    its first sample; the last entry is its end) and p, how it was made from the song file:
      file, window (start_s, end_s, start_beat, end_beat), lo (song sample of the window's first sample),
      gain (loudness), stem (vocals found), trim [beats, samples] dropped for a quiet head, stage [[n, dB, hold, ramp]]
      gain passes on the head, entry [beats, samples, vocal hold seconds or None] from an echo-out's entry."""
    __slots__ = ("audio", "beats", "voc", "p")

    def __init__(self, audio, beats, voc, p):
        self.audio, self.beats, self.voc, self.p = audio, beats, voc, p

    @property
    def offset(self):
        """Samples between the window's first sample and this excerpt's first sample."""
        return self.p["trim"][1] + (self.p["entry"][1] if self.p.get("entry") else 0)

    @property
    def src0(self):
        """Song time (seconds) of this excerpt's first sample."""
        return (self.p["lo"] + self.offset) / SR


def window_of(w):
    return {k: w[k] for k in ("start_s", "end_s", "start_beat", "end_beat")}


def load(library, song, window):
    """The window of `song` as the engine loads it: loudness-matched, faded in, nothing trimmed yet."""
    info = {}
    seg, beats, voc = dj.load_excerpt_vocals(library, song, window, info)
    return Excerpt(seg, beats, voc, {"file": song["file"], "window": window_of(window), "lo": info["lo"],
                                     "gain": info["gain"], "stem": info["stem"], "trim": [0, 0], "stage": [],
                                     "entry": None})


def _cut(ex, k, off):
    """Drop the first k beats (off samples) of ex, in place, the way trim_quiet_head and echo_entry do."""
    ex.audio = ex.audio[off:]
    ex.voc = ex.voc[off:] if ex.voc is not None else None
    beats = ex.beats[k:] - off / SR
    beats[-1] = len(ex.audio) / SR
    ex.beats = beats


def _hold(ex, hold):
    """Keep the singer out until `hold` seconds (echo_entry): the song plays, the voice comes in on a line start."""
    g = np.clip((np.arange(len(ex.audio)) / SR - hold) / live.ENTRY_FADE_S, 0, 1)[:, None]
    ex.audio, ex.voc = ex.audio - ex.voc * (1 - g), ex.voc * g


def prepare_incoming(ex, tail):
    """Start the incoming excerpt where it is loud enough and at the outgoing level (live.trim_quiet_head and
    live.stage_both), recording both in ex.p."""
    plan = {}
    extras = (ex.voc,) if ex.voc is not None else ()
    ex.audio, ex.beats, extras = live.trim_quiet_head(tail, ex.audio, ex.beats, SR, extras=extras, plan=plan)
    ex.audio, extras = live.stage_both(tail, ex.audio, SR, extras=extras, plan=plan)
    ex.voc = extras[0] if extras else None
    ex.p.update(trim=plan["trim"], stage=plan["stage"])
    return ex


def rebuild(library, song, p):
    """The excerpt that p describes, built again from the song file: the same audio, beats and vocals that the request
    which made p was working with (an MC line is not part of it: the browser plays that on its own)."""
    window = {**p["window"]}
    ex = load(library, song, window)
    k, off = p["trim"]
    if off:
        _cut(ex, k, off)
    for n, d, n_hold, n_ramp in p["stage"]:
        g = live.stage_gain(n, d, n_hold, n_ramp)[:, None]
        ex.audio = ex.audio.copy()
        ex.audio[:n] *= g
        if ex.voc is not None:
            ex.voc = ex.voc.copy()
            ex.voc[:n] *= g
    if p.get("entry"):
        k, off, hold = p["entry"]
        _cut(ex, k, off)
        if hold is not None and ex.voc is not None:
            _hold(ex, hold)
    ex.p = {**p}
    return ex


def gain_at(p, t):
    """The gain p applies to the song (and its vocals) at excerpt time t (seconds, numpy array): loudness, the fade-in,
    the head's gain staging. The vocal hold of an echo entry is not in it (recipe adds that to the vocals only)."""
    t = np.asarray(t, float)
    entry_off = p["entry"][1] if p.get("entry") else 0
    x = t + (p["trim"][1] + entry_off) / SR          # seconds from the window's first sample
    fade_n = int(dj.FADE_IN_S * SR)
    g = p["gain"] * np.clip(x * SR / max(1, fade_n - 1), 0, 1)
    u = (t + entry_off / SR) * SR                    # samples from the trimmed start, where the gain passes begin
    for n, d, n_hold, n_ramp in p["stage"]:
        c = np.where(u < n_hold, 1.0, 1 - (u - n_hold) / max(1, n_ramp - 1))
        g = g * np.where((u >= 0) & (u < n), 10 ** (d * np.clip(c, 0, 1) / 20), 1.0)
    return g


def gain_corners(p):
    """Excerpt times (seconds) where gain_at changes slope: the end of the fade-in, each gain pass's hold and end."""
    entry_off = p["entry"][1] if p.get("entry") else 0
    out = [(int(dj.FADE_IN_S * SR) - 1) / SR - (p["trim"][1] + entry_off) / SR]
    for n, _, n_hold, _ in p["stage"]:
        out += [(n_hold - entry_off) / SR, (n - entry_off) / SR]
    return out


class Join:
    """A rendered join: play before[skip:], then overlap, then nxt.audio from nxt_skip. plan says how it was made."""
    __slots__ = ("before", "overlap", "nxt", "nxt_skip", "info", "plan")

    def __init__(self, before, overlap, nxt, nxt_skip, info, plan):
        self.before, self.overlap, self.nxt, self.nxt_skip, self.info, self.plan = before, overlap, nxt, nxt_skip, info, plan


def render_join(library, songs, entry, a, skip, nxt_entry, alternatives=lambda limit: [], say=lambda msg: None):
    """Render the transition from excerpt `a` (playing from sample `skip`; its planner entry `entry`) into nxt_entry,
    measuring it. If a blend has a hole, a bump or two voices, audition other parts of the song (alternatives(limit)
    gives other windows, best first); only if none works, fall back to an echo-out. nxt_entry's window and
    transition_in are updated to what was used. Returns a Join."""
    seg, beats, seg_voc = a.audio, a.beats, a.voc
    song = songs[nxt_entry["song"]]
    original = nxt_entry["window"]
    name = song["name"]

    loaded = {}

    def load_in(w):
        """The incoming part for window w, ready to mix. Each window is decoded once per call: the echo-out fallback
        auditions the same windows the blend tried. A fresh Excerpt each time (callers replace its fields), sharing
        the arrays (nothing writes into them)."""
        k = json.dumps(window_of(w), sort_keys=True)
        if k not in loaded:
            with timing.count("load_window"):
                loaded[k] = prepare_incoming(load(library, song, w), seg)
        ex = loaded[k]
        return Excerpt(ex.audio, ex.beats.copy(), ex.voc, copy.deepcopy(ex.p))

    nxt = load_in(original)
    kind = nxt_entry["transition_in"]
    if kind == "blend":
        step_a, _ = transition.beat_steps(beats, nxt.beats)
        if beats[-(transition.OVERLAP_BEATS * step_a + 1)] * SR < skip:
            kind = nxt_entry["transition_in"] = "echo"  # excerpt too short to hold a blend
    if kind == "blend":
        why = ""
        tries = [original] + alternatives(3)
        limit = len(tries) - 1  # the last try is taken even if a line is cut, rather than an echo-out

        def attempt(n, w):
            """(why it fails or None, the join's parts) for part w of the song (try n)."""
            nx = nxt if n == 0 else load_in(w)
            plan = {}
            with timing.count("blend_try"):
                before, overlap, nxt_skip, info = transition.transition(seg, beats, nx.audio, nx.beats, SR, "blend",
                                                                        seg_voc, nx.voc, plan=plan)
            if plan.get("kind") != "blend":  # the incoming excerpt is too short to hold the overlap at this tempo ratio
                return "short", None
            if info.get("vocal_swap"):
                overlap = live.level_swap(before[skip:], overlap, nx.audio, nxt_skip, SR, plan=plan)
            clash = min(entry["window"]["out_vocal"], w["in_vocal"])
            if clash > dj.QA_MAX_VOCAL_CLASH and not info.get("vocal_swap"):  # with the swap, voices cannot overlap
                return "two voices would overlap", None
            if dj.GRID_SLIP_GUARD and info.get("grid_slip_ms", 0) > dj.GRID_MAX_SLIP_MS:
                return "one song's beats slipped off the grid", None
            if info.get("in_midline") and n < limit:
                return "the next song's singer would come in mid-line", None
            if not live.blend_is_clean(before[skip:], overlap, nx.audio, nxt_skip, info, SR):
                return "the blend did not line up", None
            if live.dip_db(before[skip:], overlap, nx.audio, nxt_skip, SR) > max(
                    0.75 * dj.QA_MAX_DIP_DB, live.own_dip_db(before[skip:], SR) + live.OWN_DIP_MARGIN_DB):
                return "the blend had a hole in it", None
            return None, (before, overlap, nx, nxt_skip, info, plan)

        # the planned part first (most joins end here); if it fails, the other parts side by side, and the first that
        # passes in their order wins: the same choice as trying them one by one, in about the time of one
        results = {0: _try_pool.submit(timing.bound(attempt), 0, tries[0])}
        n = 0
        for n in range(len(tries)):
            if n == 1:
                results.update({k: _try_pool.submit(timing.bound(attempt), k, tries[k]) for k in range(1, len(tries))})
            why, got = results[n].result()
            if why == "short":
                why = "the next song's part was too short for a blend"
                break
            if why is None:
                before, overlap, nxt, nxt_skip, info, plan = got
                nxt_entry["window"] = tries[n]
                if n:
                    say(f"into '{name}': used a different part of the song for a smoother mix")
                for f in results.values():
                    f.cancel()  # the ones not started yet; those running finish on their own
                return Join(before, overlap, nxt, nxt_skip, info, plan)
        nxt_entry["window"] = original  # nothing worked: echo out into the planned part
        nxt = load_in(original)
        kind = nxt_entry["transition_in"] = "echo"
        say(f"into '{name}': {why}{' (tried other parts too)' if n else ''}, used an echo-out")
    # An echo-out is a hard cut, so it matters even more where the incoming song starts: audition parts too.
    cands = [nxt_entry["window"]] + alternatives(3)
    best = None
    for w in cands:
        nxt_entry["window"] = w
        cand = load_in(w)
        plan = {}
        with timing.count("echo_try"):
            before, overlap, nxt_skip, info, cost, audio, voc, nbeats = live.echo_out(
                seg, beats, cand.audio, cand.beats, live.before_tail(seg, skip), SR, seg_voc, cand.voc, plan=plan)
        cand.audio, cand.voc, cand.beats = audio, voc, nbeats
        cand.p["entry"] = plan["entry"]
        if best is None or cost < best[0]:
            best = (cost, w, cand, before, overlap, nxt_skip, info, plan)
        if cost <= 1:
            break
    _, w, nxt, before, overlap, nxt_skip, info, plan = best
    nxt_entry["window"] = w
    if w is not cands[0]:
        say(f"into '{name}': used a different part of the song for a cleaner cut")
    if info["in_skip_s"]:  # the song starts later than its window: keep the page's length and the plays log true
        nxt_entry["window"] = {**w, "start_s": w["start_s"] + info["in_skip_s"]}
    if filler.ENABLED:  # FILLER=1: a drum fill bridges the cut; the incoming song lands on its last downbeat
        fplan = {}
        f_overlap, f_skip = filler.over_echo(before, overlap, nxt.audio, nxt_skip, beats=beats, nxt_beats=nxt.beats,
                                             sr=SR, plan=fplan)
        # echo_out chose its echo strength without the fill: keep the fill only if the join passes the final QA
        # loudness check (same windows: 8 s before, the first 4 beats, the 8 s from there) and has no hole
        x = np.concatenate([before[-8 * SR:], f_overlap, nxt.audio[f_skip:f_skip + 10 * SR]])
        t0 = min(8 * SR, len(before))
        t1 = t0 + int(4 * 60 / song["tempo"]["bpm"] * SR)
        db = lambda v: 20 * np.log10(np.sqrt(np.mean(v.mean(axis=1) ** 2)) + 1e-9)
        jump = min(db(x[t0:t1]) - db(x[:t0]), db(x[t0:t1]) - db(x[t1:t1 + 8 * SR]), key=abs)
        if abs(jump) <= 0.85 * dj.QA_LOUDNESS_JUMP_DB and live.dip_db(before, f_overlap, nxt.audio, f_skip, SR) <= 0.75 * dj.QA_MAX_DIP_DB:
            overlap, nxt_skip, info = f_overlap, f_skip, {**info, "filler": filler.STYLE}
            plan.update(fill=fplan["fill"], fill_delay=fplan["delay"])
    return Join(before, overlap, nxt, nxt_skip, info, plan)
