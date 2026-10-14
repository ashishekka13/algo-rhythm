# Blind A/B harness: how to use it and how to add an experiment

`ab_harness.py` renders the same transition in two ways ("variants"), plays them to you blind, and logs your
votes with every measured feature so the weights can be refitted later without listening again.

## Run it
```bash
.venv/bin/python ab_harness.py serve library_100            # page at http://127.0.0.1:8766 (live DJ stays on 8765)
.venv/bin/python ab_harness.py report library_100           # win rates, 95% CI, binomial test, by tempo gap / type
.venv/bin/python ab_harness.py fit library_100              # logistic fit of votes vs features (does not change weights)
.venv/bin/python ab_harness.py selftest library_100         # blindness, order, loudness, report and isolation checks
```
Keys on the page: `Space` start/pause, `X` / `Y` replay, `1` X better, `2` Y better, `3` no difference,
`4` both bad, `S` skip. You can vote at any time. Which variant was which is shown after each vote.

## Files
- `library_100/_mixes/ab/<experiment>/trials.json`: the pairs, their features, and the clip ids for each variant.
  The page never receives this file.
- `library_100/_mixes/ab/<experiment>/clips/<random id>.flac`: 12 s of song A, the transition, then 12 s of
  song B. Every clip is set to -18 LUFS.
- `library_100/_mixes/ab_votes.csv`: one row per vote. It holds the pair's features (`tempo_gap`, `chroma_corr`,
  `feel_diff`, `camelot_steps`, tempo flags, `pair_score` and its terms, window lengths), plus `x_*` / `y_*`
  measurements of each clip (drift, flam, clash, masking, loudness jump, dip, bass pile-up, stretch, nudge, vocal
  overlap, QA result).
- Nothing here reads or writes `_mixes/live_history.json`.

## Add an experiment
1. If the thing under test is not already an argument of `transition.transition`, add it there as a keyword
   argument whose default keeps today's behaviour (as `overlap_beats=32` does).
2. Register the experiment in `EXPERIMENTS` in `ab_harness.py`:
   ```python
   "bridge_vs_echo": {"variants": {"drum_bridge": {"kind": "bridge"}, "echo_out": {"kind": "echo"}},
                      "pairs": "blend", "n": 30},
   ```
   Each variant is a dict of keyword arguments passed to `transition.transition`. The first variant listed is
   the one the report states win rates for.
3. If the experiment needs other pairs (for example "only pairs that fail every blend gate"), give it a new
   `"pairs"` value. Then add a branch in `pick_pairs` that builds that pair list. The even spread over tempo gap
   and the limit of two uses per song stay as they are.
4. If a variant is not a blend, change the "fell back to an echo-out: not a fair test" filter in `render_pair` for
   that experiment.
5. Render it, then run the self-test:
   ```bash
   .venv/bin/python ab_harness.py render library_100 <name> --jobs 8
   .venv/bin/python ab_harness.py selftest library_100
   ```

## Known limits
- Variants that change the transition's length (for example 32 vs 16 beats) give clips of different lengths. The
  labels stay hidden, but the clip length shows in the player and the difference can be heard. This is part of
  what is being tested, not a leak of the label.
- The drift / flam / clash / masking columns are measured the way `feature_study.py` measures them, on the planned
  windows before the quiet-start trim. The QA columns are measured on the clip you actually hear.
- A 40-trial session is closer to 45-60 minutes than 20, because each clip is 33-45 s and both clips play. You can
  vote before a clip ends.
