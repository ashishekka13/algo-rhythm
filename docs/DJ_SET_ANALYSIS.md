# DJ set analysis: DJ Xirex live vs algo-rhythm (2026-10-11)

Set: "Bollywood Throwbacks, Desi Hip Hop & Baile Funk Mix | DJ Xirex Live at The Beer Cartel, Hyderabad"
(https://youtu.be/G55cWLos73s), 60:15, 58 tracks timestamped in the description.

Labels: [M] measured from the audio, [T] from the tracklist/description, [G] Gemini listening to a clip (3 clips, noisy),
[I] inferred. Work files: scratchpad `djset/` (not kept in the repo).

## What the DJ does (numbers)

| Measure | DJ Xirex | algo-rhythm today |
|---|---|---|
| Songs per hour [T] | 57.8 | about 45 (80 s preferred play) [I] |
| Play time per song [T] | median 59.5 s, 10-90% 39-97 s; 16 of 58 under 50 s | 50-140 s (`dj.FLEX_MIN_S` 50) |
| Tempo [M] | one tempo held for 18-25 min (132, then 130); 47/57 changes keep the tempo within 2%; a few slow ramps (+3 to +9 BPM over 10-40 s); drops to 103 for the close | each song at its own tempo; tempo moves only inside a blend |
| Transition type [G 3/3, I] | mostly beat-matched hard cuts / drop-swaps on a phrase line, chorus into verse or chorus into chorus | 32-beat blends (about 96% planned, 87-100% in rendered sessions), echo-out otherwise |
| Bass cut around the change [M] | bass more than 12 dB down for 4 s or more at 30/57 changes (median 4.7 s) | 8-beat bass swap inside the blend |
| Key [M, weak] | not chosen by key (32 of 57 pairs are 2-3 Camelot steps apart) | pitch-profile gate on blends |
| Language mix [T] | about 33 Hindi film, 17 desi hip-hop/Punjabi, 8 English (14%), English sometimes back to back | English only on a strong blend, at most 1 in 5 |
| Blocks [T] | 4 style/era blocks of 10-25 min | vibe held until changed |
| Energy [M] | starts at club tempo (no slow warm-up); a lower singalong "reset" every 8-12 min, right before tempo moves | level held until changed |
| Tricks [T] | word play ("Mirchi" into DIVINE), acapella over another beat, own mashups, original then remix | none of these |
| Effects [M, G] | no echo-outs, sweeps, spinbacks or risers confirmed | echo-out, drum fills (off) |

Overlap length could not be measured: on this live recording, the change detectors could not tell a cut from a
32-beat fade in a test with known joins.

## Technique gaps

| DJ technique | Do we do it | What to build | Effort | Impact |
|---|---|---|---|---|
| Beat-matched cut on a phrase line | No (blend or echo-out only) | `kind="cut"`: A stops on a phrase line between sung lines with bass rolled off, B lands on its hook/drop downbeat at A's tempo; up to ~12% tempo gap | M | H |
| One master tempo for long stretches | No | session "deck tempo": stretch every song to it (rubberband), at most ~12-15%; move it by slow ramps or at a cut | M | H |
| Short tease plays (16-45 s) | No | hook-only windows (1-2 phrases) when both joins are cuts, at most 1 in 4 songs | S | M |
| Energy waves and style blocks | Partly (vibes) | singalong reset every ~10 min; hold a style/era block for 6-15 songs, switch through a bridge song | M | M |
| Word play / theme links | No | transcribe vocal stems once; bonus when B's first line shares a keyword with A's last; cut on the word | L | H |
| Acapella over a beat / mashups | No (rule: never two voices at once) | mashup mode: B's vocals over A's instrumental for 16-32 bars; needs the user's OK | L | M-H |
| Original then remix of the same song | No | "version" field in tags; allow the pair back to back | S + data | M |
| English songs in pairs | No (`INTL_GAP` forbids) | allow a same-artist or original-then-flip pair | S | L |
| MC on the mic at a join | Partly (`mc.py`) | allow a line at reset and tempo-jump joins | S | L |

## Top 5 to build next

1. Beat-matched phrase cut (`transition.py` `kind="cut"`, chosen in `dj.transition_kind`), reusing `live.echo_exit`
   for where A stops and `live.echo_entry` for where B starts.
2. Session deck tempo: one tempo held per level, songs scored on the stretch they need.
3. Short hook-only windows for cut joins (toward ~58 songs/hour).
4. Waves and blocks in the planner (`live_plan.Planner._score`).
5. Lyric word links from transcribed vocal stems.
