"""DJ fillers: a short synthesised drum fill laid over an echo-out join (OFF unless FILLER=1).

The fill starts on the outgoing song's cut beat, its tempo ramps from the outgoing BPM to the incoming BPM (the
"tempo tweak" that bridges a gap too big to blend), and its last downbeat (kick + crash) is the incoming song's
first beat. Drums are made with numpy, no sample files. Styles: "snare_roll" (8ths -> 16ths build, with a riser)
and "dhol" (bhangra-ish bass + slap pattern).
"""
import os

import numpy as np
from scipy.signal import butter, sosfilt

from algo_rhythm import dj

ENABLED = os.environ.get("FILLER") == "1"
STYLE = os.environ.get("FILLER_STYLE", "snare_roll")
BEATS = 8           # 2 bars
REL_DB = -6         # fill RMS vs the outgoing song's last 4 s (fill() default; over_echo's floor)
UNDER_DB = 1        # over_echo: echo + fill sit this far under the level either side of the join
TAIL_S = 1.5        # the crash rings this long past the last downbeat
_RNG = np.random.default_rng(7)


def grid(bpm_from, bpm_to, beats):
    """Beat times (s) of the fill: beats+1 entries, the tempo ramping per beat; the last one is the incoming downbeat."""
    tempi = np.linspace(bpm_from, bpm_to, beats)
    return np.concatenate([[0.0], np.cumsum(60 / tempi)])


def _env(n, sr, tau):
    return np.exp(-np.arange(n) / sr / tau)


def _band(x, lo, hi, sr):
    return sosfilt(butter(2, [lo, hi], "bandpass", fs=sr, output="sos"), x)


def _sweep(sr, f0, f1, tau_f, tau_a, dur):
    """Sine whose pitch falls from f0 to f1 (time constant tau_f), decaying with tau_a."""
    n = int(dur * sr)
    f = f1 + (f0 - f1) * _env(n, sr, tau_f)
    return np.sin(2 * np.pi * np.cumsum(f) / sr) * _env(n, sr, tau_a)


def kick(sr):
    x = _sweep(sr, 160, 48, 0.03, 0.22, 0.4)
    click = _band(_RNG.standard_normal(int(0.004 * sr)), 1500, 6000, sr) * 0.3
    x[:len(click)] += click
    return x


def snare(sr):
    n = int(0.25 * sr)
    return 0.8 * _band(_RNG.standard_normal(n), 1200, 8000, sr) * _env(n, sr, 0.07) + 0.5 * _sweep(sr, 220, 185, 0.02, 0.05, 0.25)


def hat(sr):
    n = int(0.08 * sr)
    return 0.5 * _band(_RNG.standard_normal(n), 7000, 16000, sr) * _env(n, sr, 0.02)


def crash(sr, dur=TAIL_S + 0.5):
    n = int(dur * sr)
    return np.stack([_band(_RNG.standard_normal(n), 3500, 16000, sr) * _env(n, sr, 0.55) for _ in range(2)], axis=1)


def dhol_bass(sr):  # dagga: low, round, a little skin noise
    n = int(0.35 * sr)
    return _sweep(sr, 130, 75, 0.04, 0.16, 0.35) + 0.15 * _band(_RNG.standard_normal(n), 100, 800, sr) * _env(n, sr, 0.03)


def dhol_slap(sr):  # tilli: sharp, high, short
    n = int(0.12 * sr)
    return 0.6 * _sweep(sr, 520, 430, 0.01, 0.04, 0.12) + 0.5 * _band(_RNG.standard_normal(n), 1800, 6000, sr) * _env(n, sr, 0.025)


def _hits(style, beats):
    """(position in beats, sound, gain) for every hit before the final downbeat. Gain rises across the fill."""
    out = []
    for k in range(beats):
        g = 0.45 + 0.55 * k / max(1, beats - 1)
        if style == "snare_roll":  # first half 8ths, second half 16ths; a soft kick on every beat
            sub = 2 if k < beats // 2 else 4
            out += [(k + j / sub, snare, g * (1.0 if j == 0 else 0.75)) for j in range(sub)]
            out.append((k, kick, 0.5 * g))
        elif style == "dhol":  # dha on the beat, ge on the last 16th, tilli on the off-beat; the last beat rolls
            out += [(k, dhol_bass, g), (k + 0.5, dhol_slap, 0.8 * g), (k + 0.75, dhol_bass, 0.6 * g),
                    (k + 0.5, hat, 0.4 * g)]
            if k == beats - 1:
                out += [(k + 0.25, dhol_slap, 0.7 * g), (k + 0.75, dhol_slap, 0.9 * g)]
        else:
            raise ValueError(f"unknown filler style {style!r}")
    return out


def near_tempo(bpm_from, bpm_to):
    """The incoming tempo at the octave nearest the outgoing one (80 -> 160 ramps 80 -> 80, half time)."""
    return min((bpm_to * r for r in (1, 0.5, 2)), key=lambda b: abs(np.log(b / bpm_from)))


def fill(bpm_from, bpm_to, beats=BEATS, sr=dj.OUT_SR, style=STYLE, ref=None, rel_db=REL_DB):
    """Stereo fill: len = grid end + TAIL_S. Level: RMS = RMS(ref) + rel_db (ref = the outgoing audio), else -20 dBFS.
    Soft-limited, peak <= 1."""
    g = grid(bpm_from, near_tempo(bpm_from, bpm_to), beats)
    n_body = int(round(g[-1] * sr))
    x = np.zeros((n_body + int(TAIL_S * sr), 2))
    at = lambda pos: int(round(np.interp(pos, np.arange(len(g)), g) * sr))  # beat position -> sample, on the ramp
    pan = {hat: (0.7, 1.0), dhol_slap: (0.85, 1.0)}
    for pos, sound, gain in _hits(style, beats) + [(beats, kick, 0.8)]:
        s = sound(sr) * gain
        i = at(pos)
        m = min(len(s), len(x) - i)
        x[i:i + m] += s[:m, None] * np.array(pan.get(sound, (1.0, 1.0)))
    c = crash(sr)[: len(x) - n_body]
    x[n_body:n_body + len(c)] += 0.25 * c
    if style == "snare_roll":  # riser: high noise swelling into the downbeat
        r = _band(_RNG.standard_normal(n_body), 2000, 12000, sr) * np.linspace(0, 1, n_body) ** 3 * 0.35
        x[:n_body] += r[:, None]
    rms = lambda v: np.sqrt(np.mean(v ** 2)) + 1e-9
    target = rms(ref) * 10 ** (rel_db / 20) if ref is not None else 0.1
    x *= target / rms(x[:n_body])
    return dj.soft_limit(x)


def over_echo(before, overlap, nxt, nxt_skip, beats, nxt_beats, sr=dj.OUT_SR, style=STYLE, n_beats=BEATS, plan=None):
    """Lay a fill over an echo-out from live.echo_out: (before, overlap, nxt_skip) -> (overlap, nxt_skip).
    The echo tail (overlap - nxt[:nxt_skip]) stays where it is, on the cut; the fill starts there too and the
    incoming song is delayed so its first beat lands on the fill's last downbeat.
    plan: a dict that gets "fill" (the fill's audio, starting on the cut) and "delay" (samples the song waits)."""
    tail = overlap - nxt[:nxt_skip]
    bpm_from = 60 / float(np.median(np.diff(beats)))
    bpm_to = 60 / float(np.median(np.diff(nxt_beats)))
    land = int(round(grid(bpm_from, near_tempo(bpm_from, bpm_to), n_beats)[-1] * sr))
    # The song has stopped under the fill, so a fill at REL_DB alone leaves a 4-5 dB hole (measured). Level the fill
    # so echo + fill sit UNDER_DB below the mean of the 2 s either side; the fill alone never above that.
    rms = lambda v: np.sqrt(np.mean(v.mean(axis=1) ** 2)) + 1e-9
    side = (20 * np.log10(rms(before[-2 * sr:])) + 20 * np.log10(rms(nxt[:2 * sr]))) / 2
    target = 10 ** ((side - UNDER_DB) / 20)
    ref = before[-4 * sr:]
    need = np.sqrt(max(target ** 2 - rms(tail[:land]) ** 2, (target * 10 ** (REL_DB / 20)) ** 2))
    f = fill(bpm_from, bpm_to, n_beats, sr, style, ref=ref, rel_db=20 * np.log10(need / rms(ref)))
    delay = max(0, land - int(round(nxt_beats[0] * sr)))
    n = max(len(tail), len(f), delay + nxt_skip)
    out = np.zeros((n, 2))
    out[:len(tail)] += tail
    out[:len(f)] += f
    out[delay:n] += nxt[:n - delay]
    if plan is not None:
        plan.update(fill=f, delay=int(delay))
    return out, n - delay
