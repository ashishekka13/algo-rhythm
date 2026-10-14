# algo-rhythm: progress

Updated 2026-10-10 evening. The approach is in `README.md`; this file is the status.

## 0. Latest (2026-10-10, night): productionised
- **Name:** algo-rhythm; Python package `algo_rhythm`.
- **Library:** about 400 songs: songs released before 2015 moved to `library_100/_retired/` (manifest kept); the trendy list added.
- **Removed:** the downloader. Requests come only from the library.
- **Server:** FastAPI (`algo_rhythm.server`) with JSON logs, `/healthz`, `/docs`; three views (admin, guest, presenter) in the new design.
- **Talk to the DJ:** text in English or Hinglish; Gemini through one gateway (`genai.py`); keyword rules when Gemini is unavailable.
- **Data:** PostgreSQL or SQLite when `DATABASE_URL` is set (songs, analyses, Gemini answers, parties, plays, requests, AI cache and call log); audio from a GCS bucket via `STORAGE_URL`.
- **Lyrics:** transitions that cut a lyric mid-line fell from 15% to 3% (20 ms sung segments, lyric-safe echo-outs).
- **Checks:** all 8 pass. Untested here: real Cloud SQL, a real bucket, the Docker build.

## 1. State
| Item | State |
|---|---|
| Live server | http://127.0.0.1:8765, restarted on the latest code with **851 songs** |
| Library | 100 original songs + 57 downloads (52 replaced originals, your page downloads) + 694 from the two DJ lists (305 by phase, 389 by era); 58 GB with vocal tracks |
| Gemini details | all 851 songs (`gemini-3.5-flash-lite`); list 1's phases kept as energy |
| Checks | `check_live.py` PASS; page tests 24/24 PASS; `validate_tags.py` PASS |
| Deploy zip | `~/Downloads/song-matcher-deploy.zip` (code only, 134 KB; no audio, venvs or mixes) |

## 2. What was done, in order
1. **Tagging + matching:** audio tags per `TAGGING_CONTRACT.md`, a pair score and matrix, an offline mix builder.
2. **Live system:** 4 energy levels, requests, play history, a web page, speakers, Play/Pause on the page.
3. **Vocals:** Demucs vocal tracks; a vocal swap so two singers never overlap.
4. **Downloader:** search, rank, download (FLAC), separate, tag.
5. **More songs:** 52 songs added for connectivity, later all replaced with the original film recordings (Gemini picks among YouTube Music's official uploads).
6. **Gemini:** energy and song details; London Thumakda now sits in high.
7. **Feature study:** rendered 220 blends and measured them. Pitch-profile match and rhythm feel replaced the Camelot rule as blend gates; "tempo uncertain" songs echo-out.
8. **Mixing fixes from your feedback:**
   - songs leave where the singer is quiet
   - beats are snapped to real drum hits
   - the best bit of the next song comes first, otherwise the next verse
   - every song reaches a real verse
   - lyrics are never faded (cut in a breath, enter at a line start)
   - songs play 50 to 140 s
   - a fitting request takes the very next slot
   - faster climb to max
9. **Page:** "Under the hood" (why each song was chosen), "Start over", a full redesign with the design skills you asked for.
10. **Your two lists:** 420 unique of "1,000" (574 were Vol repeats) and 500 by era; 694 added after removing 12 duplicates; the rest were already present, not found (mostly made-up "edits"), or blocked by YouTube.

## 3. Measured results worth sharing
| Question | Result |
|---|---|
| Does the Camelot key predict chord clash? | Barely. Pitch-profile correlation does (54% vs 18% clash). |
| What predicts beats drifting? | Straight vs swung feel (41-58% vs 22-24%); tempo-flagged songs (73% vs 30%). |
| Beat snapping | Misalignment 5.8 → 3.6 ms (median) |
| Exits mid-line | 8 of 9 → about 3 of 12 (phrase level); with the line-aware swap, 1 of 10 blends had to cut without a full breath |
| Incoming lyric faded in | 0 of 10 blends in the last test session |
| Request moved up | A fitting request plays right after the current song (was 4th) |
| Gemini listening (lite, noisy) | sessions scored 3.0 → 3.5 → 3.2 of 5 across changes and different songs |
| Demucs | 10-15 s per song on the GPU |

## 4. Honest limits
- I cannot hear the music. The numbers above are measurements, and the Gemini scores are a rough signal.
- About 30% of blends still drift by more than 60 ms somewhere in the overlap.
- Only about 9% of song pairs can blend; the rest use echo-outs.
- Downloads come from YouTube (lossy source; against its terms; personal use).
- Some list entries could not be found because they are not real releases. 19 were blocked by YouTube; rerunning `tracklist_fetch.py` retries them.

## 5. Open items
1. **Your listening feedback** on the new mixing (note the two song names when a transition sounds wrong).
2. Shorter overlaps for less certain pairs (to cut the remaining drift).
3. Retry the YouTube-blocked songs.
4. Optional: a data zip (tags, Gemini answers, rhythm cache, a few MB) so a deployed copy does not need re-tagging.
