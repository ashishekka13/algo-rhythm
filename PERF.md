# algo-rhythm — performance and cleanup work

## Status (2026-10-15, against the web app; the file:line refs below are from the 2026-10-13 desktop snapshot)

The web rewrite moved the hot path: the live render is now `/advance` (`party.py` → `joins.render_join`), and its
budget is the time the current song plays (about 50-140 s), not `AHEAD_S = 10`.

**Phase 0, measured in the cloud (Cloud Run 2 vCPU server; jobs 4 vCPU, no GPU):**

| Stage | Measured |
|---|---|
| `/advance` (a join) | 0.1-139 s per request, median about 21 s; 58, 90 and 139 s seen: the 139 s one can outlast a song |
| songs job (canonical MP3, Demucs on CPU, tagging, features, upload) | 148-457 s per song, median 228 s (91 songs) |
| library load at startup | 1.5-2 s for 89 songs (not the startup cost: that is the cold start and imports) |
| enrichment | not measured yet |

Every `/advance` that renders now logs `advance CODE timing {json}`: stages (claim, rebuild, render, render_fallback,
mc, recipe, attach, clip) and counted calls (rubberband runs, grid_slip, blend and echo tries). Query:
`gcloud logging read 'resource.labels.service_name="algo-rhythm-web" AND textPayload:"timing"'`.

| Item | Status |
|---|---|
| Phase 0 | partly done (table above); per-stage numbers come from the timing log |
| 1.1 grid_slip when the guard is off | **still true** (`transition.py`, every blend; read only behind `GRID_SLIP_GUARD = False` in `joins.py`). Locally it costs more than all six rubberband runs of a blend (1.4 s vs 0.7 s) |
| 1.2 validate_tags hashing | still true; local tool, off the cloud path: low |
| 2.1 Demucs before the tagger | **done**: the songs job separates first and the tagger takes the stem path |
| 2.2 CUDA branch | still true (`stems.py`); matters only on a GPU job; log the device anyway |
| 2.3 cheaper nn_filter | not reached: every song in the cloud pipeline gets a stem |
| 3.1 rubberband churn | **worse**: a vocal-swap blend stretches 6 times (mix, stems, instrumentals), up to 4 windows |
| 3.2 startup | premise changed: library load is 2 s; cost is the cold start (min-instances 0) |
| Phase 4 | `swap_gains` fallback unused; the echo branch of `transition.transition` was reachable and crashed the recipe (**fixed**: such a join becomes an echo-out); `filler.py` to label; `transition_reviews` read by the analytics export only; `word_link` used by its test only |
| Bug 1 has_vibe all() | still true |
| Bug 2 enrich positional match | still true |
| Bug 3 instrumental floor | still true; the fix needs a re-tag run |
| Bug 4 stem clipping | changed: stems are MP3 via `media.encode`, which hard-clips at ±1; add a peak log |
| Bug 5 errors stop the music | **fixed for the web app**: a failed render retries as an echo-out, then the song is dropped and the claim freed at once (it used to stall for `RENDER_STALE_S` while the music ran out); a failed MC line no longer loses the join |
| Bug 6 Python 3.14 | still open in `pyproject.toml` (images use 3.11) |

Next, in order: the audio-neutral speedups (stretches in parallel, grid_slip off with the guard, the clip and the
MC voice off the critical path), then bugs 1, 2, 6; then the A/B experiments and bug 3.

---

Hand this to the build agent. It is ordered. Do the phases in order and do not skip Phase 0.

Source: audit of the 2026-10-13 zip. Every file:line below is from that snapshot — confirm each
still matches before editing. Timings marked "measured" were run on Apple silicon, Python 3.12.

---

## Rules for this work

1. **Measure before and after, on the same library.** An optimisation without a before/after number
   is not done. Report both.
2. **One change at a time.** If throughput changes, we must know which change did it.
3. **Never trade audio quality for speed.** Specifically — see the red lines below.
4. If a task's premise turns out to be wrong on the real box, **stop and report** rather than
   inventing a different optimisation.

### Red lines — do not do these, however tempting

- **Do not chunk the time-stretch.** `transition.warp` (`transition.py:43-54`) issues ONE
  `pyrb.timemap_stretch` call with all 33 beat anchors. Splitting it into segments and
  concatenating would be faster and would introduce a phase discontinuity at every seam. It will
  improve the drift metric and sound worse. This is the single most important constraint in the file.
- **Do not add any threshold that rejects a pair.** ~8% of pairs already clear the blend gates; the
  rest fall back to echo-out. Any new gate makes the headline defect worse. Speed is never a reason
  to reject a join.
- **Do not lower audio quality settings** (`--crisp 6`, sample rates, FLAC depth) to gain speed
  without an explicit A/B.

---

## Phase 0 — find out what is actually slow (do this first)

"Everything is hanging" covers at least four independent systems. Do not optimise blind.

Instrument and report wall-clock for each, on the GCP box, with the real library:

| Stage | How to time it |
|---|---|
| Server startup | time from process start to the `"ready: open http://..."` log line |
| Tagging | per-song wall clock, split by whether a Demucs stem existed |
| Demucs separation | per-song wall clock, and **log which device was selected** |
| Live render | per-join render time, and how often it exceeds the `AHEAD_S = 10` budget (`live.py:34`) |
| Enrichment | per-batch wall clock and the number of Gemini retries |

**Deliverable:** a table of the five numbers plus machine spec (vCPU, RAM, GPU present or not).
Everything after this is prioritised by that table. Report it before starting Phase 1.

---

## Phase 1 — pure waste (zero behaviour change, safe to ship immediately)

### 1.1 Stop computing `grid_slip_ms` when the guard is off

`transition.py:329` computes `grid_slip_ms` on **every blend** — two 4-second windowed
cross-correlation sweeps. It is consumed only at `live.py:770`, which is gated on
`GRID_SLIP_GUARD`, currently `False` (`dj.py:73`). So the result is discarded every time, inside
the live render budget.

Make the computation conditional on the flag. Keep the code path intact so the flag still works.

**Acceptance:** blends render identically (byte-compare a rendered session before/after); per-join
render time drops; `GRID_SLIP_GUARD = True` still behaves as before.

### 1.2 Stop re-hashing the library on every validation

`tools/validate_tags.py:29` re-hashes every audio file by default — O(total library bytes). A
`--no-hash` flag already exists. Make skipping the default, or hash only files whose mtime or size
changed since the tag was written.

**Acceptance:** `validate_tags.py` on the full library completes in seconds; a deliberately
corrupted file is still caught when hashing is requested.

---

## Phase 2 — the big one: tagging cost

### 2.1 Run Demucs *before* the tagger (highest value, no code change)

**This is likely your main hang.** `vocal_frames()` (`tagger.py:294-301`) uses
`librosa.decompose.nn_filter` with cosine distance, which materialises a full frame-to-frame
affinity matrix. Measured:

| Audio | Frames | nn_filter | Affinity matrix |
|---|---|---|---|
| 30 s | 646 | 0.66 s | 3 MB |
| 60 s | 1,292 | 0.75 s | 13 MB |
| 120 s | 2,584 | 2.19 s | 53 MB |

Quadratic. Extrapolated to a 5-minute song: **~14 s CPU and ~330 MB RAM per song.** With
`--jobs 6` that is ~2 GB of simultaneous allocation — enough to swap or OOM a modest VM, which
looks exactly like hanging.

The alternative path, `vocal_frames_from_stem()` (`tagger.py:311-316`), is plain RMS:
**0.011 s** on the same 120 s input. Roughly a thousand times cheaper.

Today the order invites paying the expensive path for nothing: tag → separate → re-tag (the tagger
re-tags when a stem appears, `tagger.py:504`). **Separate first, then tag once.** No code change,
just pipeline order — but verify the tagger does pick the stem path on a first tag.

**Acceptance:** on a sample of 20 songs, per-song tagging time with stems present vs absent,
reported as a table. Peak RSS per worker reported for both.

### 2.2 Add a CUDA branch to Demucs

`stems.py:77` selects `"mps" if torch.backends.mps.is_available() else "cpu"`. **There is no CUDA
branch.** On a GCP GPU instance, PyTorch never uses the GPU and separation silently runs on CPU.

Add CUDA detection, and **log the selected device at startup** so this can never be invisible again.

**Acceptance:** the device is logged; on a GPU box it reports `cuda`; per-song separation time
before/after.

### 2.3 Make `nn_filter` affordable for songs that genuinely have no stem

Only after 2.1 and 2.2. For songs where no stem can be produced, the proxy still costs ~14 s.
Options, in order of preference — pick one and justify it:

- Compute the proxy on a decimated frame grid (larger `VOCAL_HOP`) and interpolate back. The output
  is consumed at 16-beat phrase resolution (`dj.py:529-530`), so fine detail is discarded anyway.
- Cap the analysis to a bounded window rather than the whole track.
- Process in overlapping blocks with a bounded affinity matrix.

**Acceptance:** per-song cost and peak RSS before/after, plus a correlation of the new
`vibe.vocal_activity` against the old on 20 songs. If correlation is below 0.9, stop and report.

---

## Phase 3 — live render path

Only if Phase 0 shows joins exceeding the 10 s budget.

### 3.1 Reduce subprocess churn in warping

Each blend attempt spawns **2–4 `rubberband` processes** (outgoing, incoming, and both vocal stems
when present, `transition.py:323-332`), and pyrubberband round-trips through temp WAV files each
time. `_render_join` retries up to 4 candidate windows (`live.py:757`), so a difficult join can
spawn up to **16 subprocesses**. `echo_out` separately auditions 3 feedback/repeat combinations
(`live.py:609`) across up to 4 windows, each calling `pyrb.time_stretch` (`transition.py:268`).

Investigate, in this order:
1. Can the cheap rejection checks run *before* warping, so failing candidates never pay for it?
   Several checks in `blend_is_clean` (`live.py:628-642`) need the rendered audio, but the tempo
   and excerpt-length checks do not.
2. Can the python-rubberband binding be replaced with an in-process library call, avoiding the
   temp-file round trip entirely?
3. Is the 4-candidate retry earning its cost? Report how often candidates 2, 3 and 4 are reached
   and how often each succeeds.

**Do not** reduce the retry count purely to save time — that pushes joins onto the echo path.
Report the numbers and let a human decide.

### 3.2 Server startup

`live.build` (`live.py:917-928`) loads every tag, attaches every embedding and reads the art index
before serving. Separately, numba JIT warmup is significant — **measured ~40 s to first
`ready` log with only 16 songs**, so most of that is not the library.

Report the split between JIT warmup and library loading. If JIT dominates, consider warming it in a
background thread so `/healthz` answers sooner. If library loading dominates, consider loading
embeddings and art lazily.

---

## Phase 4 — dead weight (safe, reduces what a reader has to understand)

Confirm each is truly unreachable before deleting; if unsure, leave it and say so.

- `transition.swap_gains` (`transition.py:131-135`) — `blend_swap` is always called with explicit
  gains from `line_swap_gains`.
- The echo branch inside `transition.transition` (`transition.py:309-314`) — `_render_join` only
  ever calls it with `kind="blend"` (`live.py:763`); all echo work goes through `live.echo_out`.
- `filler.py` — gated off behind `FILLER=1` (`filler.py:15`) and called only from
  `tools/filler_sample.py`. Keep the file, but say plainly in the README that it is an unshipped
  experiment.
- `transition_reviews` — written by `review.py:95-98`; **no reader found**. Either wire it into
  something or document it as write-only telemetry.
- `lyrics.word_link` (`lyrics.py:109`) — no caller found in `dj.py` or `live.py`. Confirm whether
  it is live; if not, label it.

---

## Correctness bugs found alongside (not performance, but cheap and real)

Fix these only after the performance work, or if they turn out to be the cause.

1. **One song degrades the whole library.** `dj.py:161` — `has_vibe = all(vibes)`. A single song
   missing vibe data makes *every* song fall back to `0.5` defaults for valence, danceability and
   acousticness, flattening the vibe, ease and smooth scoring terms. Should be per-song.
2. **Tags can be written to the wrong songs.** `enrich.py:95-101` matches answers by name, then
   falls back to **positional zip when the counts match**. A reordered reply of the right length
   silently mis-assigns mood and energy. Remove the positional fallback; drop unmatched rows.
3. **Instrumental tracks get fabricated vocal data.** `tagger.py:313-314` measures singing relative
   to the track's own 90th percentile with no absolute floor, so separation noise on an instrumental
   reads as confident singing. Add an absolute dB floor.
4. **Demucs stems can clip.** `stems.py:95` writes PCM_16 after de-standardising by `ref.std()`;
   values can exceed ±1.0 with no check. The tagger then measures vocal activity on the clipped
   signal. Add a peak check and either normalise or warn.
5. **Engine errors stop the music.** `live.py:707-715` re-raises after logging; the thread dies and
   audio stops. Consider recovering to the next song instead.
6. **Python 3.14 segfaults.** `librosa.peak_pick` is numba-compiled and crashes with SIGSEGV on
   3.14 + current numpy. Pin `requires-python = ">=3.11,<3.14"` in `pyproject.toml`. Verified: the
   same library tags cleanly on 3.12.

---

## Reporting format

For each task:

```
## Task
<id and one line>

## Premise check
<was the file:line still as described? was the premise true on this box?>

## What I changed
<one line; or "nothing — premise was wrong, here is what I found">

## Numbers
| metric | before | after |

## Audio safety
<for anything touching the render path: evidence that output is unchanged,
 or an A/B experiment registered and rendered>

## What I did NOT do
```
