"""Show how the DJ picks the part of each song it plays, on a short real session, and render that session to listen to.

    .venv/bin/python explain_mix.py LIBRARY "Kala Chashma" [--songs 3] [--level max]

Writes LIBRARY/_mixes/listen/explain/<start>.wav (the mix) and <start>.md (the report): for every song, each
candidate part with its score broken down, which part was played and why, and how the join into it was made.
Uses its own play history, so the real one is untouched.
"""
import argparse
import sys
from pathlib import Path

from algo_rhythm import dj
from algo_rhythm import live
from algo_rhythm import tagger

# the weights inside dj.candidate_windows, by name, so the report can show each part's score as a sum
PARTS = {"hook": 0.3, "drums_in": 0.15, "drums_out": 0.15, "quiet_in": 0.1, "quiet_out": 0.3, "has_best": 0.2, "lead_in": 0.15}


def terms(w):
    return {"hook": w["score"], "drums_in": w["in_q"], "drums_out": w["out_q"], "quiet_in": 1 - w["in_vocal"],
            "quiet_out": 1 - w["out_vocal"], "has_best": w["has_best"], "lead_in": w["lead_in"]}


def mmss(s):
    return f"{int(s // 60)}:{int(s % 60):02d}"


def song_block(song, played, entry, info, prev_name):
    cands = dj.candidate_windows(song, live.EXCERPT_S, [], keep=6, flex=True)
    if played not in cands:
        cands.append(played)
    best = song["phrases"]["start_beats"]
    lines = [f"## {song['name']}",
             f"{song['tempo']['bpm']:.0f} BPM, key {song['key']['camelot']}, energy {song['energy_pct']:.2f}, "
             f"{len(best)} phrases of {tagger.PHRASE_BEATS} beats.", ""]
    if entry.get("why"):
        lines += [f"**Why this song:** {entry['why']} (planner score {entry['score']:.2f})", ""]
    lines += ["**Candidate parts** (score = sum of weight × value; the length pull adds up to 0.08):", "",
              "| Part | Length | " + " | ".join(f"{k} ×{v}" for k, v in PARTS.items()) + " | Score |",
              "|---" * (len(PARTS) + 3) + "|"]
    for w in sorted(cands, key=lambda w: -sum(PARTS[k] * v for k, v in terms(w).items())):
        t = terms(w)
        mark = " **← played**" if w is played or w["start_s"] == played["start_s"] and w["end_s"] == played["end_s"] else ""
        lines.append(f"| {mmss(w['start_s'])}–{mmss(w['end_s'])}{mark} | {w['end_s'] - w['start_s']:.0f} s | "
                     + " | ".join(f"{t[k]:.2f}" for k in PARTS) + f" | {sum(PARTS[k] * v for k, v in t.items()):.2f} |")
    lines.append("")
    if prev_name:
        kind = info.get("type", entry["transition_in"])
        lines.append(f"**Join from '{prev_name}':** {kind}")
        if kind == "blend":
            lines += [f"- {info.get('overlap_s')} s overlap, {info.get('bpm_out')} → {info.get('bpm_in')} BPM, "
                      f"beat nudge {info.get('phase_nudge_ms')} ms, grid slip {info.get('grid_slip_ms')} ms (limit {dj.GRID_MAX_SLIP_MS})"]
            if info.get("vocal_swap"):
                lines.append(f"- outgoing singer cut at {info['vocal_out_at_s']} s into the overlap"
                             f"{' (mid-line: no breath found)' if info['out_midline'] else ' (in a breath)'}; "
                             f"incoming singer enters at {info['vocal_in_at_s']} s"
                             f"{' (mid-line)' if info['in_midline'] else ' (at a line start)'}; "
                             f"two voices together: {info['vocal_overlap_s']} s (would have been {info['vocal_overlap_s_without']} s)")
        lines.append("")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("library", type=Path)
    ap.add_argument("start")
    ap.add_argument("--songs", type=int, default=3)
    ap.add_argument("--level", default="max")
    args = ap.parse_args(argv)
    library = args.library.resolve()
    out = library / "_mixes" / "listen" / "explain" / f"{args.start.replace(' ', '_')}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    seconds = 140 * args.songs  # long enough for the songs to come in; trimmed below
    sess = live.simulate(library, [], seconds, out, level=args.level, start=args.start)
    tl = [t for t in sess.timeline if t["start_s"] != float("inf")][:args.songs]
    lines = [f"# How the parts were picked: {args.songs} songs from '{args.start}' at level {args.level}", "",
             f"Mix: `{out.name}`. Song start times in the mix: "
             + ", ".join(f"{sess.songs[t['entry']['song']]['name'][:30]} {mmss(t['start_s'])}" for t in tl), ""]
    prev = None
    for t in tl:
        e = t["entry"]
        song = sess.songs[e["song"]]
        lines += song_block(song, e["window"], e, e.get("transition_info", {}), prev)
        prev = song["name"]
    md = out.with_suffix(".md")
    md.write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"\nmix: {out}\nreport: {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
