"""Runnable checks for the live DJ: scripted sessions (no speakers, faster than real time), then assertions.

    .venv/bin/python check_live.py library_100
"""
import os
import sys
import tempfile
from pathlib import Path

from algo_rhythm import dj
from algo_rhythm import live



def run(library, script, seconds, name, level="medium", out_dir=None):
    out = (out_dir or library / "_mixes") / f"{name}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    sess = live.simulate(library, script, seconds, out, level=level)
    songs, tl = sess.songs, [t for t in sess.timeline if t["start_s"] != float("inf")]  # last one may not be on the air yet
    print(f"\n--- {name}")
    for t in tl:
        e = t["entry"]
        s = songs[e["song"]]
        print(f"{t['start_s']:7.1f}s  {s['name'][:34]:34s} E {s['energy_pct']:.2f} {e['transition_in']:5s} "
              f"match {e['match']:.2f}{'  <- REQUEST' if e['request'] else ''}{'' if e['window']['score'] >= 0 else ''}")
    return sess, out


def now_at(tl, t):
    return max(i for i, x in enumerate(tl) if x["start_s"] <= t)


def transition_problems(sess, out):
    songs, tl = sess.songs, [t for t in sess.timeline if t["start_s"] != float("inf")]
    rows = [{"file": songs[t["entry"]["song"]]["file"], "position_s": t["start_s"],
             "transition_in": t["entry"]["transition_in"], "transition_info": t["entry"].get("transition_info", {}),
             "bpm": songs[t["entry"]["song"]]["tempo"]["bpm"], "terms": {"vibe": 1, "join": 1, **t["entry"]["terms"]},
             "energy_pct": songs[t["entry"]["song"]]["energy_pct"], "target_energy": songs[t["entry"]["song"]]["energy_pct"],
             "window": t["entry"]["window"]} for t in tl]
    keys = ("loudness", "bass", "stretch", "nudge", "peak", "dead-air", "vocal", "dip")
    return [f"{c['check']} = {c['value']} (limit {c['limit']})" for c in dj.qa(out, rows)["checks"]
            if not c["ok"] and any(k in c["check"] for k in keys)]


def scenario_a(library):
    """Level change + a request: the original flow."""
    sess, out = run(library, [{"t": 90, "vibe": "max"}, {"t": 200, "request": "Tera Ban Jaunga"}], 800, "live_a")
    songs, tl, bad = sess.songs, [t for t in sess.timeline if t["start_s"] != float("inf")], []
    natural = [t["entry"]["window"]["natural_start"] for t in tl]
    infos = [t["entry"].get("transition_info", {}) for t in tl[1:]]
    swapped = [i for i in infos if i.get("vocal_swap")]
    if swapped:
        print(f"vocal swap used in {len(swapped)} of {sum(i.get('type') == 'blend' for i in infos)} blends: "
              f"{sum(i['vocal_overlap_s_without'] for i in swapped):.1f} s of two voices at once avoided, "
              f"{sum(i['vocal_overlap_s'] for i in swapped):.1f} s left")
    # A cut is a normal DJ move for a big energy change (the listener asked for one, or a request pulls the set far),
    # so only transitions with a modest energy step are judged: those should mostly be blends.
    en_of = lambda i: songs[tl[i]["entry"]["song"]]["energy_pct"]
    steady = [i for i in range(1, len(tl)) if abs(en_of(i) - en_of(i - 1)) <= 0.2]
    echoes = sum(tl[i]["entry"]["transition_in"] == "echo" for i in steady)
    all_echoes = sum(t["entry"]["transition_in"] == "echo" for t in tl[1:])
    req = [i for i, t in enumerate(tl) if t["entry"]["request"]]
    print(f"entering from their own start: {sum(natural)}/{len(natural)}; echo-outs {all_echoes}/{len(tl) - 1} overall, "
          f"{echoes}/{len(steady)} among transitions with an energy step of 0.2 or less")
    if not natural[0]:
        bad.append("A: opening song did not start from its beginning")
    if echoes > 0.4 * len(steady):
        bad.append(f"A: too many echo-outs in the steady part ({echoes} of {len(steady)})")
    if not req:
        bad.append("A: request never played")
    elif not 1 <= req[0] - now_at(tl, 200) <= 4:
        bad.append(f"A: request played {req[0] - now_at(tl, 200)} songs later, wanted 1-4")
    return bad + [f"A: {p}" for p in transition_problems(sess, out)]


def scenario_b(library):
    """low -> max -> low: max must be held, and the second 'low' must bring fresh songs."""
    sess, out = run(library, [{"t": 150, "vibe": "max"}, {"t": 900, "vibe": "low"}], 1500, "live_b", level="low")
    songs, tl, bad = sess.songs, sess.timeline, []
    e = [songs[t["entry"]["song"]]["energy_pct"] for t in tl]
    lo, hi = now_at(tl, 150), now_at(tl, 900)
    arrive = next((j for j in range(lo, hi + 1) if e[j] >= 0.7), None)  # first song in the max band
    held = e[arrive:hi + 1] if arrive is not None else []
    print(f"max reached {arrive - lo if arrive is not None else 'never'} songs after the press; energies from there: {[round(x, 2) for x in held]}")
    if arrive is None or arrive - lo > 4:
        bad.append("B: max level was not reached within 4 songs of the press")
    elif min(held) < 0.7:
        bad.append(f"B: max level was not held (lowest energy {min(held):.2f}, wanted >= 0.70)")
    first_low = {tl[i]["entry"]["song"] for i in range(0, lo + 1)}
    second_low = [tl[i]["entry"]["song"] for i in range(hi + 3, len(tl))]
    repeats = [songs[i]["name"] for i in second_low if i in first_low]
    print(f"second low phase: {len(second_low)} songs, repeats of the first low phase: {repeats}")
    if len(second_low) < 2:
        bad.append("B: not enough songs after switching back to low to judge freshness")
    if repeats:
        bad.append(f"B: switching back to low replayed songs: {repeats}")
    return bad + [f"B: {p}" for p in transition_problems(sess, out)]


def scenario_d(library):
    """The page must show what is audible: 'now playing' changes in the MIDDLE of a transition, and a song that has
    been chosen but not rendered yet must never count as started."""
    sess = live.build(library, "medium", history=Path(tempfile.gettempdir()) / "check_live_d_history.json")
    sess.timeline = [{"entry": sess._entry_for_test(0), "start_s": 0.0},
                     {"entry": sess._entry_for_test(1), "start_s": 60.0, "overlap_s": 16.0},   # blend: 60 s .. 76 s
                     {"entry": sess._entry_for_test(2), "start_s": float("inf"), "overlap_s": 0.0}]  # chosen, not rendered
    name = lambda i: sess.songs[i]["name"]
    bad = []

    def at(ph):
        sess.playhead = lambda: ph
        return sess.view()
    v = at(30)
    if v["now"]["name"] != name(0) or v["mixing"] or [e["name"] for e in v["next"][:2]] != [name(1), name(2)]:
        bad.append(f"D: at 30 s expected song 0 playing, nothing mixing, songs 1 and 2 coming; got {v['now']['name']}, {v['mixing']}")
    v = at(62)  # transition has started, old song still louder
    if v["now"]["name"] != name(0) or not v["mixing"] or v["mixing"]["to"] != name(1):
        bad.append(f"D: at 62 s the OLD song should still be 'now' while mixing into song 1; got {v['now']['name']}, {v['mixing']}")
    if name(1) in [e["name"] for e in v["next"]]:
        bad.append("D: a song that is mid-transition must not also be listed as coming up")
    v = at(70)  # past the middle
    if v["now"]["name"] != name(1) or not v["mixing"]:
        bad.append(f"D: at 70 s (past the middle) song 1 should be 'now', still mixing; got {v['now']['name']}, {v['mixing']}")
    v = at(90)
    if v["now"]["name"] != name(1) or v["mixing"] or v["next"][0]["name"] != name(2):
        bad.append(f"D: at 90 s song 1 should play alone with song 2 next; got {v['now']['name']}, {v['next'][0]['name']}")
    v = at(500)  # song 2 is not rendered yet: it must NOT be shown as playing, however late it is
    if v["now"]["name"] != name(1):
        bad.append(f"D: a song that was never rendered was shown as playing: {v['now']['name']}")
    print("\n--- live_d (what the page shows): " + ("ok" if not bad else "\n".join(bad)))
    return bad


def scenario_e():
    """The vocals must get exactly the same trimming and gain as the main audio, or the swap would drift out of step."""
    import numpy as np
    sr = 44100
    rng = np.random.default_rng(0)
    quiet_start = np.concatenate([0.02 * rng.standard_normal((3 * sr, 2)), 0.3 * rng.standard_normal((40 * sr, 2))])  # quiet, then loud
    tail = 0.3 * rng.standard_normal((20 * sr, 2))
    beats = np.append(np.arange(0, 43, 0.5), len(quiet_start) / sr)
    head, new_beats, extras = live.trim_quiet_head(tail, quiet_start, beats, sr, extras=(quiet_start.copy(),))
    head, extras = live.stage_both(tail, head, sr, extras=extras)
    bad = []
    if len(head) >= len(quiet_start):
        bad.append("E: the quiet start was not trimmed (test setup problem)")
    if not np.array_equal(head, extras[0]):
        bad.append("E: vocals and main audio were treated differently by trimming/gain staging")
    if abs(new_beats[-1] - len(head) / sr) > 1e-9:
        bad.append("E: last beat does not sit on the last sample after trimming")
    print("\n--- live_e (vocals stay in step with the audio): " + ("ok" if not bad else "; ".join(bad)))
    return bad


def scenario_f():
    """Echo-out timing: the cut stays near the end on a bar line, a long wait for the incoming singer becomes a later
    start (music leads in about a bar), and the beats and vocals stay in step with the trimmed audio."""
    import numpy as np
    sr, p = 22050, 0.5  # 120 bpm
    rng = np.random.default_rng(1)
    voice = lambda n: 0.3 * rng.standard_normal((n, 2))
    beats = np.arange(0, 40.001, p)  # 80 beats; the last one is the excerpt end
    # outgoing: sung through except a gap 37.5-39.7 s: beats 38.0, 38.5, 39.0 are clean, 38.0 is the bar line
    seg_voc = voice(40 * sr)
    seg_voc[int(37.5 * sr):int(39.7 * sr)] = 0
    cut, info = live.echo_exit(seg_voc.copy(), seg_voc, beats, sr)
    bad = [] if abs(cut / sr - 38.0) < 0.01 else [f"F: the cut should be on the bar line at 38.0 s, got {cut / sr:.2f} s"]
    # outgoing sung straight through, softest at 28 s (32 beats back) and a little softer at 36 s: never cut 12 s early
    seg_voc = voice(40 * sr)
    seg_voc[int(27.9 * sr):int(28.1 * sr)] *= 0.05
    seg_voc[int(35.9 * sr):int(36.1 * sr)] *= 0.1
    cut, info = live.echo_exit(seg_voc.copy(), seg_voc, beats, sr)
    if cut / sr < 40 - live.ECHO_LOOK_BEATS * p - 0.01:
        bad.append(f"F: a sung-through echo-out cut {40 - cut / sr:.1f} s before the end")
    # incoming: mid-line at its start (0-2 s), next line at 20 s: start on the bar line about 1 bar before it
    nxt_voc = np.zeros((60 * sr, 2))
    nxt_voc[:2 * sr], nxt_voc[20 * sr:23 * sr] = voice(2 * sr), voice(3 * sr)
    nxt = nxt_voc + 0.1 * rng.standard_normal(nxt_voc.shape)
    nb = np.arange(0, 60.001, p)
    out, out_voc, out_beats, info = live.echo_entry(nxt, nxt_voc, nb, sr)
    if info["vocal_in_at_s"] > live.ECHO_HOLD_MAX_BEATS * p or abs(info["in_skip_s"] - 18.0) > 0.01:
        bad.append(f"F: incoming should start at 18.0 s with a short hold, got {info}")
    if len(out) != len(out_voc) or abs(out_beats[-1] - len(out) / sr) > 1e-9 or out_beats[0] != 0:
        bad.append("F: incoming audio, vocals and beats are out of step after the later start")
    print("\n--- live_f (echo-out timing): " + ("ok" if not bad else "; ".join(bad)))
    return bad


def main(library):
    library = Path(library).resolve()
    problems = scenario_e() + scenario_f() +scenario_d(library) + scenario_a(library) + scenario_b(library)
    print("\nPASS" if not problems else "\nFAIL\n  " + "\n  ".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "library_100"))
