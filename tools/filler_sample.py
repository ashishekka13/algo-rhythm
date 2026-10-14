"""Listening sample for DJ fillers (algo_rhythm.filler) before they go live.

Picks 3 song pairs the planner would echo out (big tempo gap, straight vs swung, dance -> dance), renders each join
three ways (A current echo-out, B + snare_roll fill, C + dhol fill), plus 8 s solo fills at 100 and 128 BPM.

    python -m tools.filler_sample [LIBRARY]     (default: ../song-matcher/library_100; writes LIBRARY/_mixes/filler_sample/)
"""
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

from algo_rhythm import dj, filler, live

SR = dj.OUT_SR
EDGE_S = 10
DANCE = {"dance_pop", "club_edm", "item_number", "hiphop"}


def db(x):
    return 20 * np.log10(np.sqrt(np.mean(x.mean(axis=1) ** 2)) + 1e-9)


def safe(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:28]


def pick_pairs(songs):
    """(category, a, b, reason) for the three listening cases, deterministic."""
    ok = [s for s in songs if not s["tempo_uncertain"] and (s.get("vibe") or {}).get("vocal_source") == "demucs"]
    big, feel, dance = [], [], []
    for a in ok:
        for b in ok:
            if a is b or abs(a["energy_pct"] - b["energy_pct"]) > 0.15:
                continue
            kind, why = dj.transition_kind(a, b)
            if kind != "echo":
                continue
            gap = dj.tempo_gap(a["tempo"]["bpm"], b["tempo"]["bpm"])
            fgap = abs(a.get("triplet", 0.5) - b.get("triplet", 0.5))
            if gap > 0.15:
                big.append((abs(gap - 0.22), a["name"], a, b, why))
            elif "rhythm feel" in why:
                feel.append((-fgap, a["name"], a, b, why))
            style = lambda s: (s.get("llm") or {}).get("style")
            if style(a) in DANCE and style(b) in DANCE and min(a["energy_pct"], b["energy_pct"]) > 0.6 \
                    and abs(a["energy_pct"] - b["energy_pct"]) < 0.08 and "tempo gap" in why and gap <= 0.15:
                dance.append((abs(a["energy_pct"] - b["energy_pct"]), a["name"], a, b, why))
    out, used = [], set()
    for cat, lst in (("big tempo gap", big), ("straight vs swung", feel), ("dance -> dance", dance)):
        for _, _, a, b, why in sorted(lst, key=lambda c: c[:2]):
            if a["name"] not in used and b["name"] not in used:
                out.append((cat, a, b, why))
                used |= {a["name"], b["name"]}
                break
    return out


def render(library, a, b):
    """The live echo-out path (live._render_join, echo branch, planned windows only).
    Returns (before, overlap, nxt, nxt_skip, beats, nxt_beats)."""
    wa = dj.candidate_windows(a, live.EXCERPT_S, (), force_first=True, flex=True)[0]
    wb = dj.candidate_windows(b, live.EXCERPT_S, (), force_first=True, flex=True)[0]
    seg, beats, seg_voc = dj.load_excerpt_vocals(library, a, wa)
    nxt, nxt_beats, voc = dj.load_excerpt_vocals(library, b, wb)
    extras = (voc,) if voc is not None else ()
    nxt, nxt_beats, extras = live.trim_quiet_head(seg, nxt, nxt_beats, SR, extras=extras)
    nxt, extras = live.stage_both(seg, nxt, SR, extras=extras)
    nxt_voc = extras[0] if extras else None
    before, overlap, nxt_skip, info, cost, nxt, _ = live.echo_out(seg, beats, nxt, nxt_beats, seg, SR, seg_voc, nxt_voc)
    return before, overlap, nxt, nxt_skip, beats, nxt_beats


def clip(before, overlap, nxt, nxt_skip, land):
    """EDGE_S before the cut .. EDGE_S after the incoming first beat (`land` samples into the overlap); as heard."""
    x = np.concatenate([before[-EDGE_S * SR:], overlap, nxt[nxt_skip:nxt_skip + EDGE_S * SR]])
    return 0.97 * dj.soft_limit(x[:min(len(before), EDGE_S * SR) + land + EDGE_S * SR].copy())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    library = Path(argv[0] if argv else Path(__file__).resolve().parents[2] / "song-matcher/library_100").resolve()
    out = library / "_mixes" / "filler_sample"
    out.mkdir(parents=True, exist_ok=True)
    songs, _ = dj.load_library(library)
    readme = ["DJ filler listening sample (algo_rhythm.filler, OFF in the live DJ until FILLER=1).", "",
              f"Each clip: {EDGE_S} s before the cut, the join, {EDGE_S} s after the incoming song's first beat.",
              "A = today's echo-out. B = + snare_roll fill. C = + dhol fill. Fill = 8 beats (2 bars) starting on the",
              "cut beat, tempo ramping from the outgoing BPM to the incoming one (half/double time folded), kick + crash",
              "on the incoming first beat. Level: echo + fill sit 1 dB under the mean of the 2 s either side of the join",
              "(the fill alone is under the song; it rises across the 2 bars). All clips soft-limited x0.97 as live.",
              "", "Loudness (RMS dB, mono, measured): before = 2 s before the cut, fill = cut .. incoming first beat",
              "(A: first 2 s after the cut), after = 2 s from the incoming first beat.", ""]
    for k, (cat, a, b, why) in enumerate(pick_pairs(songs), 1):
        before, overlap, nxt, nxt_skip, beats, nxt_beats = render(library, a, b)
        e0 = min(len(before), EDGE_S * SR)
        bpm_a, bpm_b = 60 / np.median(np.diff(beats)), 60 / np.median(np.diff(nxt_beats))
        readme += [f"{k}. {a['name']}  ->  {b['name']}   [{cat}]",
                   f"   BPM {bpm_a:.1f} -> {bpm_b:.1f} (tags {a['tempo']['bpm']:.1f} -> {b['tempo']['bpm']:.1f}); "
                   f"feel {a.get('triplet', 0):.2f} -> {b.get('triplet', 0):.2f}; echo because: {why}"]
        for tag, style in (("A_current", None), ("B_snare_roll", "snare_roll"), ("C_dhol", "dhol")):
            if style:
                ov, skip = filler.over_echo(before, overlap, nxt, nxt_skip, beats, nxt_beats, SR, style)
                land = len(ov) - skip
            else:
                ov, skip, land = overlap, nxt_skip, 0
            x = clip(before, ov, nxt, skip, land)
            path = out / f"{k}_{safe(a['name'])}__{safe(b['name'])}__{tag}.wav"
            sf.write(path, x, SR, subtype="PCM_16")
            pre, post = db(x[e0 - 2 * SR:e0]), db(x[e0 + land:e0 + land + 2 * SR])
            mid = db(x[e0:e0 + land]) if land else db(x[e0:e0 + 2 * SR])
            row = f"   {tag:13s} before {pre:6.1f}  fill {mid:6.1f}  after {post:6.1f}   " \
                  f"(fill-before {mid - pre:+.1f} dB, after-fill {post - mid:+.1f} dB, fill {land / SR:.2f} s)  {path.name}"
            readme.append(row)
            print(row)
        readme.append("")
    for style in ("snare_roll", "dhol"):
        for bpm in (100, 128):
            x = filler.fill(bpm, bpm, filler.BEATS, SR, style)
            x = np.pad(x, ((0, max(0, 8 * SR - len(x))), (0, 0)))[:8 * SR]
            path = out / f"solo_{style}_{bpm}bpm.wav"
            sf.write(path, 0.97 * x, SR, subtype="PCM_16")
            readme.append(f"solo: {path.name}  (8 beats at {bpm} BPM, then kick + crash, padded to 8 s)")
    readme += ["", "What to listen for:",
               "- Does the fill start exactly on the cut beat, and does the incoming song's first beat hit with the crash?",
               "- Pair 1: does the tempo ramp feel like a natural speed-up/slow-down, or like the drummer dragging/rushing?",
               "- Pair 2: does a straight fill bridge a swung song (or the reverse) or does it fight the groove?",
               "- Pair 3: dance -> dance: does the fill keep the floor moving, or is the 2-bar gap too long?",
               "- Level: the fill should sit under the song, not jump out; the snare build should rise, not blast.",
               "- Sound: do the synthesised drums sound cheap next to real recordings (solo_* files)?",
               "- Which style suits Bollywood better: B (snare roll) or C (dhol)? Or neither (keep A)?"]
    (out / "README.txt").write_text("\n".join(readme) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
