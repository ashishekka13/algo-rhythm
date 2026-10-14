# Tagging contract v2.0

**For:** the agent that analyzes and tags a music library (around 200 songs) so `dj.py` can build mixes from it.
**Your job:** run the tagger over every song, prove the output is valid, and report what needs a human's attention. You don't produce tag values yourself. Every value comes from `tagger.py`, so the same file always gets the same tags.

---

## 1. Context

`dj.py` builds a party mix by repeatedly choosing the next song. It decides using only the tag files, without listening to any audio. It reads:

- tempo and key, to judge whether two songs can be blended
- energy, to follow the night's energy curve
- timbre vectors, to judge whether two songs sound alike
- phrase scores and beat times, to cut each song at its best section on a phrase boundary and line the beats up during a transition

Wrong tags therefore produce audibly wrong mixes. A tempo off by 20% makes a stretched blend warble, and a bad beat grid puts the drums out of time. That's why the contract favours **flagging** a doubtful value over guessing.

## 2. Inputs

- **The library folder** (`LIBRARY`): any folder of audio files. Subfolders are fine (e.g. `Artist/Album/track.mp3`).
- **Formats:** `.mp3`, `.wav`, `.flac`, `.ogg`. Anything else is ignored.
- **Ignored paths:** anything inside a folder or file whose name starts with `.` or `_`, and the reserved folder `LIBRARY/tags/`.
- **The code:** this repository (`tagger.py`, `validate_tags.py`, `tag_schema.json`, `requirements.txt`).

## 3. Setup (once per machine)

```bash
cd <repo>
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Tagging does not need `rubberband`; only rendering a mix does.

## 4. Procedure

1. **Check the inputs.** Count the audio files, confirm the folder can be written to, and note any files that look broken (zero bytes, wrong extension).
2. **Run the tagger.** On one machine:
   ```bash
   .venv/bin/python tagger.py LIBRARY --jobs 6
   ```
   Use `--jobs` = CPU cores − 2. Each worker uses roughly 1–1.5 GB of RAM, so lower `--jobs` if memory is short.
3. **For several agents in parallel**, split the file list into **disjoint** shards, one per agent, with paths relative to `LIBRARY`:
   ```bash
   .venv/bin/python tagger.py LIBRARY --jobs 4 --files "a/one.mp3" "b/two.mp3" ...
   ```
   Every tag file is written atomically (to a temp file, then renamed), and the output is deterministic. Even an accidental overlap between shards therefore can't corrupt anything, but don't plan on overlap.
4. **It's resumable.** Re-running skips any song whose tag file already matches the current `schema_version` and audio `sha256`. Use `--force` only when you were told to re-tag.
5. **Validate.** Run this once all shards are finished:
   ```bash
   .venv/bin/python validate_tags.py LIBRARY
   ```
   The run must end with `PASS`. The validator also writes `LIBRARY/tags/_report.json`.
6. **Retry failures once.** Re-run `tagger.py` with `--files` set to just the failed songs. If a song still fails, leave its error file in place and report it. Don't try to fix the audio.
7. **Report back** (format in section 9).

**Expected time:** about 6–20 s of CPU per song, depending on length. 200 songs of typical length on an 8-core machine with `--jobs 6` should take roughly 10–15 minutes.

## 5. Output layout

| Path | What it is |
|---|---|
| `LIBRARY/tags/<key>.json` | One tag file per song (schema in section 6). `<key>` is the path relative to `LIBRARY`, with `/` replaced by `__` and the extension removed. Example: `Artist/Album/Song.mp3` → `Artist__Album__Song.json`. |
| `LIBRARY/tags/_errors/<key>.json` | Written when a song fails: `{file, error, traceback}`. Deleted automatically once that song tags successfully. |
| `LIBRARY/tags/_report.json` | The validator's report. |

Every audio file must end up with **exactly one** outcome: a valid tag file, or an error file.

## 6. Tag file reference

The machine-readable schema is in `tag_schema.json`. "Used by the mixer for" says why each field matters, so you can judge how serious a bad value is.

| Field | Type / unit | Used by the mixer for |
|---|---|---|
| `schema_version` | `"2.0"` | Rejects tag files from other schema versions |
| `file` | path relative to `LIBRARY` | Finding the audio to render |
| `sha256` | hex | Detecting audio that changed after tagging, and de-duplicating |
| `title`, `artist`, `metadata_source` | string, string or null, `file_tags` or `filename` | Display; **`artist`** enforces "same artist not within N minutes". `null` means unknown, and the rule is then skipped. |
| `duration_s` | seconds | Range checks |
| `tempo.bpm` | BPM, fitted to the beat grid | **Can these two songs blend?** Double/half tempo counts as compatible. |
| `tempo.kick_band_bpm` | BPM | A second tempo estimate taken from the kick drum. A disagreement raises `tempo_disagreement`, and the mixer then never stretches that song. |
| `tempo.confidence`, `beat_strength`, `grid_shift_ms`, `half_double`, `beat_count` | diagnostics | Review only |
| `beats` | seconds, ascending, ms precision | **Cutting excerpts and lining up beats in transitions.** Must be monotonic and match `tempo.bpm`. |
| `key.camelot` | `1A`–`12B` | **Harmonic compatibility** (same key 1.0, neighbours or relative key 0.9, two steps 0.75, clash 0.4). Clashes of more than 2 steps get an echo-out instead of a blend. |
| `key.name`, `confidence`, `margin`, `runner_up` | diagnostics | Review. A low `margin` means the key is uncertain. |
| `loudness.lufs`, `peak_dbfs` | LUFS, dBFS | Gain staging. Every excerpt is played at −14 LUFS. |
| `energy.score` | 0–1, independent of loudness | **The energy curve.** The mixer ranks songs by this within the library, so only relative order matters. |
| `energy.*` (others) | diagnostics | Review |
| `structure.intro_end_s`, `outro_start_s`, `phrase_anchor_s` | seconds | Measuring energy, and anchoring the 16-beat phrase grid |
| `phrases.start_beats` | beat indices, consecutive, 16 apart | **The only legal places to cut a song.** |
| `phrases.energy`, `repetition`, `score` | 0–1, one per phrase | **Choosing the excerpt.** score = ½ energy + ½ repetition (choruses are loud and repeat). |
| `hooks` | up to 3 windows `{start_beat, end_beat, start_s, end_s, score}`, ~45 s, whole 32-beat blocks, non-overlapping | Precomputed best sections for review. The mixer recomputes windows at its own length from `phrases`. |
| `vectors.mfcc_mean`, `mfcc_std`, `spectral_contrast` | 13 + 13 + 7 floats | **"Does it sound alike"** (timbre similarity) |
| `vectors.chroma` | 12 floats, sum 1 | Harmony fingerprint (reserved) |
| `vocal_ratio`, `embedding` | **always `null` in v2** | Used automatically if a future tagger fills them. Don't fill them yourself. |
| `quality.flags` | list (see section 8) | Excluding or downgrading songs; the review list |

## 7. Hard requirements (the validator enforces these)

1. Every tag file matches `tag_schema.json`.
2. `sha256` matches the audio file now on disk.
3. `beats` are strictly increasing, end within the song, and their median spacing matches `tempo.bpm` within 3%.
4. `key.camelot` is the correct Camelot code for `key.name`.
5. Structure times are in order: 0 ≤ intro end ≤ outro start ≤ duration.
6. Phrase arrays are the same length, and their starts are consecutive 16-beat blocks inside the beat grid.
7. The `too_few_phrases` flag is set exactly when a song has fewer than 4 phrases.
8. Hooks fall inside the grid, are whole 32-beat blocks, and exist whenever the song has at least 4 phrases.
9. Every audio file has a tag file or an error file, and no tag file points at missing audio.

## 8. Quality flags

Flags are **information, not errors**. The mixer handles each one as listed; your job is to list them in your report.

| Flag | Meaning | What the mixer does |
|---|---|---|
| `tempo_disagreement` | The full-mix and kick-drum tempo estimates differ by more than 3% (excluding clean double/half differences) | Never stretch-blends this song; always echo-out |
| `tempo_octave_ambiguous` | BPM above 150 or below 75; might be double or half the felt tempo | Nothing special; tempo matching already treats double/half as compatible |
| `low_tempo_confidence` | Weak, irregular pulse (e.g. ballads, rubato) | Nothing special |
| `low_key_margin` | The top two keys nearly tied | Nothing special |
| `too_few_phrases` | Under 64 beats of grid, so it can't host two 32-beat overlaps | **Excluded from mixes** |
| `long_intro` | Intro longer than 30 s (often a video skit before the music starts) | Nothing special; excerpts come from the hook anyway |
| `clipped_source` | Decoded audio peaks above 0 dBFS (a heavily limited or lossy master) | Gain is capped to avoid clipping |
| `duration_out_of_range` | Shorter than 90 s or longer than 12 min | Nothing special |

## 9. Rules

- **Don't edit tag values by hand, and don't invent them.** If a value looks wrong, report it with your reasoning. Corrections belong in `tagger.py`, so they apply to every song.
- **Don't modify, convert, rename or move audio files** after tagging. The tags are tied to the exact file through `sha256` and `file`.
- **Don't add files to the library** or download audio.
- **Don't use an LLM to produce or adjust tag values.** The values must be reproducible.
- Leave `vocal_ratio` and `embedding` as `null`.

## 10. Report back

When you finish, reply with:

```
LIBRARY: <path>
Audio files: N | valid tags: N | failed: N | validator: PASS/FAIL
Wall time: Xm on N cores (--jobs J)
Failed (after one retry): <file>: <error first line>   (one per line, or "none")
Flagged for review: <count per flag>, then <file>: <flags> for tempo_disagreement and too_few_phrases
Anything unusual: <e.g. many files with no artist tag, suspicious duplicates, unreadable formats>
```

## 11. Versioning

`schema_version` is `"2.0"`. Adding an optional field is a minor version (`2.1`), which the mixer still accepts. Renaming, removing or changing the meaning of a field is a major version, and the mixer rejects tag files whose version doesn't start with `2.`
