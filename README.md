# algo-rhythm

A live AI DJ for a Bollywood library, as a web app. Every song is analysed once and stored in the database. A party
is started on the device connected to the speakers: **that browser plays and mixes the music**, from a plan the
server makes (which song next, which part of it, and a recipe for each join). Guests join with the party code on
their phones to request songs, vote and react; a big screen shows what is playing. You steer it with **6 vibes**
(an energy level and a mood), **song requests** and **Talk to the DJ** (English or Hinglish, turned into DJ actions
by Gemini).

> Status, numbers and open items: **`PROGRESS.md`**.

---

## 1. Rules for anyone working on it

- **Claude cannot hear audio.** "How it sounds" is measured, or judged by the user. The user's ears are the real test.
- **The user controls playback.** Never press Play on a party the user is listening to.
- **Gemini: `gemini-3.5-flash-lite` only** for text, through Vertex AI (billed). The gateway caps text requests at 200 a day (`GEMINI_DAILY_CAP`) per process as a cost guard.
- **No AI call inside the audio path.** Gemini runs offline (song details, transition review), on a Talk to the DJ command (it only sets goals), and for the MC's lines and voice; mixing is rules and signal processing.
- **No downloads.** Songs come only from the library (the originals in the bucket).
- Tests use temp databases and the synthetic library; the real data is never touched.
- Version control: git, pushed to https://github.com/ashishekka13/algo-rhythm. Commit each working change.

## 2. Run it

### On a laptop (SQLite, the library folder as storage)

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"     # once; also: brew install rubberband
DATABASE_URL=sqlite:///$PWD/local.db .venv/bin/python -m algo_rhythm.server LIBRARY   # imports LIBRARY once if the database is empty
```

Open http://127.0.0.1:8765, press **Start a party**: that tab is the host and plays the music. Guests open
`/p/CODE` (the code is on the host page and the big screen, `/p/CODE/screen`). A tagged library is needed: `LIBRARY/tags`
and `LIBRARY/stems` (see Offline jobs), or the synthetic one for trying it out:
`python -m tools.make_test_track --library test_library --vocals && python -m algo_rhythm.tagger test_library`.

### On Google Cloud

| Piece | What |
|---|---|
| Cloud Storage `gs://song-mixer-511123-algo-rhythm` | `library/` the originals (input); `web/` the library the browser plays: canonical MP3s, vocal stems, art, MC lines |
| Cloud SQL (PostgreSQL 16) | songs, analyses, Gemini details, features, embeddings (shared by every party); parties and their whole session state; plays, requests, votes, reactions, AI cache |
| Cloud Run service `algo-rhythm` | the web server, stateless (at most one instance for now: the WebSocket hub is in-process) |
| Cloud Run Jobs `algo-rhythm-songs / -details / -art` | build the library from the originals: analysis + Demucs on CPU (10 tasks), Gemini details + search vectors, images |
| Secret Manager | `database-url`, `gemini-api-key` |

`deploy/gcp.sh` does it step by step (`check`, `apis`, `registry`, `sql`, `secrets`, `account`, `build`, `server`, `cors`,
`jobs`, `run-songs`, `run-details`, `run-art`); each step is safe to run again and the billed ones say so.

### Configuration (environment, see `.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | required for the server | `postgresql+psycopg://…` (Cloud SQL; `?host=/cloudsql/PROJECT:REGION:INSTANCE` on Cloud Run) or `sqlite:///…` |
| `STORAGE_URL` | unset = audio is in `LIBRARY_DIR` | `gs://bucket/prefix` or `file:///abs/path`; files are fetched into `LIBRARY_DIR` when the server needs them |
| `LIBRARY_DIR` | the path given on the command line | Local library folder, or the cache for the bucket |
| `SOURCE_URL` | jobs only | `gs://bucket/prefix` of the original audio |
| `GEMINI_API_KEY` | `~/.config/song-matcher/gemini_key` | Secret Manager on GCP |
| `GEMINI_BACKEND` | `vertex` | `vertex`: Vertex AI express-mode key. `studio`: an AI Studio key |
| `GEMINI_MODEL`, `GEMINI_DAILY_CAP` | `gemini-3.5-flash-lite`, 200 | Text model and the daily cap on text requests |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | Search by meaning |
| `HOST`, `PORT`, `LOG_LEVEL` | `127.0.0.1`, 8765, `INFO` | Logs are one JSON object per line |
| `MC_TTS`, `MC_VOICE` | `gemini`, `Aoede` | AI MC voice (Gemini text-to-speech on the same key) |

### Offline jobs

On Google Cloud these are the Cloud Run Jobs (`python -m algo_rhythm.pipeline songs | details | art`). On a laptop:

```bash
.venv/bin/python -m algo_rhythm.tagger LIBRARY --jobs 3         # analyse new songs (skips up-to-date ones)
.venv-stems/bin/python -m algo_rhythm.stems LIBRARY            # Demucs vocals (.vocals.mp3) for new songs
.venv/bin/python -m algo_rhythm.enrich LIBRARY                 # Gemini song details (no LIBRARY: the database)
.venv/bin/python -m algo_rhythm.search LIBRARY                 # embed song descriptions that lack a vector
.venv/bin/python -m algo_rhythm.importer LIBRARY [--upload]    # copy files into the database (idempotent)
.venv/bin/python -m algo_rhythm.art LIBRARY --dry-run          # song art: what it would make and cost
.venv/bin/python -m algo_rhythm.review [--party N] [--limit 15]  # Gemini scores each join's clip (database mode)
.venv/bin/python -m algo_rhythm.analytics export [--project P]  # tables as JSON lines; with --project, load into BigQuery
```

## 3. Folder layout

```
algo-rhythm/
  README.md  PROGRESS.md  TAGGING_CONTRACT.md  AB_HARNESS.md
  pyproject.toml  Dockerfile (server)  Dockerfile.jobs  cloudbuild.yaml  deploy/gcp.sh  docker-compose.yml  .env.example
  algo_rhythm/
    server.py        FastAPI: pages, the party API, /media, the WebSocket; stateless
    party.py         a party's session in the database: start, advance (the next join), sync, requests, votes, talk, views
    hub.py           WebSocket hub: pushes each party's view on change and once a second
    library.py       the library one server instance caches (read-only), planners per request, media URLs, assets
    joins.py         one join: rendered (numpy, rubberband) to check it, and recorded; excerpts rebuilt from a description
    recipe.py        a join as Web Audio "plays" for the browser, and a numpy reference player with the browser's rules
    live.py          the session (what plays next) and the offline engine (scripted checks, no speakers)
    live_plan.py     beam-search planner toward the chosen level; requests
    dj.py            pair scores, which part of a song plays, QA, library loading, analysis caches
    transition.py    blend, vocal swap, echo-out, 20 ms sung-segment detector
    media.py         the one encoding every browser-played file uses
    pipeline.py      Cloud Run Jobs: originals -> library in the database and the bucket
    tagger.py  stems.py  tag_schema.json      audio analysis, Demucs vocals
    genai.py  enrich.py  search.py  talk.py  mc.py  art.py  review.py  analytics.py  filler.py  lyrics.py
    config.py  db.py  repository.py  storage.py  importer.py
    static/live.html (start page, host, guest, big screen), static/player.js (the browser's mixer), static/visuals.js, static/ab.html
  tools/             validate_tags, ab_harness, feature_study, explain_mix, make_test_track (synthetic library), lyrics
  tests/             test_recipe, test_web, test_feedback, test_pipeline, test_data, test_talk, ..., ui/ (Chromium, jsdom)
```

## 4. How it works

### 4.0 The web app: who does what

- **The server plans, the browser plays.** When a song goes on the air, the host's page asks `/advance`: the server
  picks the next song (the planner, as before), renders the join with numpy and rubberband to check it (the same QA
  as always: holes, bumps, two voices, other parts of the song auditioned) and answers with a **recipe**: the
  outgoing song until the join, the join's overlap (the server's own render, stored as an MP3 asset: the beat match
  there is a pitch-kept stretch, which the Web Audio API cannot do), then the incoming song, plus the MC's line.
  Every play runs at normal speed; `player.js` schedules them with the Web Audio API, a minute or more ahead.
- **The browser's join is the server's join.** `tests/test_recipe.py` (numpy) and `tests/ui/player_browser.py`
  (Chromium) check it: the songs either side sample for sample, the overlap the server's audio through the MP3, no
  click at either seam, 0 samples between Chromium's and the server's decoding.
- **Every song and stem is one MP3 encoding** (`media.py`), so every decoder places every sample the same way: a stem
  subtracts cleanly from its song in the browser, and the beat grids from the analysis hold.
- **Stateless:** a party's whole session (vibe, queue, timeline with each join's recipe, requests, the MC) is a JSON
  column; each request loads it under a row lock and saves it back. A slow join render does not hold the lock.
- **Live updates:** each screen has a WebSocket; the server pushes the view on every change and once a second. The
  host's player reports its playhead there; guests' screens follow it. Polling takes over while a socket is down.
- **No sign-in:** whoever starts a party gets a host key (kept in that browser); guests have only the code. Votes and
  reactions are rate limited in the database.

### 4.1 Analysis per song (no AI)
`tagger.py`, schema **2.5**: tempo and beat grid, key (Camelot), loudness, energy, 16-beat phrases with energy, repetition and hook score, timbre, pitch-class profile, a `vibe` block, and per phrase: level, drums, steadiness and **vocal activity from the Demucs vocal track**. Measured caches per song: **rhythm feel** (straight vs swung), **beat-grid slip** per phrase, and **sung segments** at 20 ms (where the singer sings, with separate open and close thresholds so a held note is not a gap).

### 4.2 Vocal separation
`stems.py`: Demucs `htdemucs`, vocals only, on the Apple GPU (about 10 to 15 s per song). The instrumental is `song - vocals`.

### 4.3 Gemini (`gemini-3.5-flash-lite`, through `genai.py`)
| Use | What it does | Evidence |
|---|---|---|
| Song details (`enrich.py`) | energy 1-4, confidence, familiarity, occasion, mood, style, rhythm, language, voice, year, moment in the night | Energy fixed obvious misses (London Thumakda moved to high). Style and rhythm did **not** predict mixing clashes, so style is only a small crowd-flow term. Year chose which older songs to archive. |
| Song names (`enrich.py`, enrich-v3) | clean title, film, singers; the display name everywhere is "Title - Film" (file name cleaned without AI when there is no answer) | 403/403 songs named in 10 requests (45 per request, name fields only for songs that had the rest). |
| Talk to the DJ (`talk.py`) | "build it up to a peak", "kuch romantic bajao" -> set level, request up to 3 library songs, skip, play, pause | Only songs named in the text plus the 20 closest in meaning go in the prompt. "Animal ka Bobby Deol wala gaana bajao" now requests Jamal Kudu first. Falls back to keyword rules when Gemini is unavailable. |
| Search by meaning (`search.py`, `genai.embed`) | one description per song (title, film, singers, year, language, style, mood, occasion, moment, level) embedded once; `/search` and the talk shortlist use it | "rain song" finds Barso, Baarish, Bheegi Bheegi. Vectors: `_mixes/embeddings.json` or `song_embeddings`. |

Energy used by the planner = measured energy rank blended with Gemini's class (weight 0.6 × its confidence). Every call goes through the gateway: JSON-schema checked, cached (in the database when there is one), capped per day, logged.

### 4.4 What decides a good blend (measured, not guessed)
`tools/feature_study.py` rendered 220 blends and measured them:

| Feature | Finding | Used as |
|---|---|---|
| Camelot key distance | Barely predicts chord clash | 30% of harmony |
| **Pitch-profile correlation** | Below 0.0 → 54% clash; above → 18% | 70% of harmony; hard gate |
| **Rhythm feel difference** | Above 0.3 → 41-58% beat drift; below → 22-24% | hard gate at 0.3 |
| "Tempo uncertain" flag | 73% drift vs 30% | hard gate |
| Tempo gap | Stretch beyond 10% | hard gate |
| Beat-grid slip | In 20 of 21 truly drifting pairs one song's tagged beats were wrong in the overlap | measured per render; acting on it is behind `GRID_SLIP_GUARD` (off) |

Only about 8% of pairs pass every gate; the rest join with an echo-out.

### 4.5 Which part of a song plays (`dj.candidate_windows`)
Any start, any length from **50 to 140 s** (about 80 s preferred), at least **4 sung phrases**. Scored on the best bit (usually the hook), a lead-in, an instrumental exit, steady drums at the edges, and whether the part **starts and ends between sung lines**. `tools/explain_mix.py` prints this scoring for a real 3-song mix.

### 4.6 Live planner (`live_plan.py`)
Plans the next 4 songs after the locked one. Levels low/medium/high/max = centres 0.125/0.375/0.625/0.875 of the energy rank, held until changed. Requests land 1 to 3 songs ahead; a request that fits the song on air takes the next slot. The last 12 songs cannot repeat; played songs get a penalty.

### 4.7 Engine (`live.py`, `transition.py`)
- **Blend:** 32 beats, each beat snapped to the real drum hit and warped onto a shared grid in one rubberband call; bass swap; **line-aware vocal swap** (the outgoing voice is cut where a line has ended and none starts within 400 ms; the incoming voice comes in just before a line starts; voices never fade).
- **Echo-out:** the outgoing song stops on a beat where the singer is between lines, the echo repeats only the music, and the incoming singer is held until a line starts.
- **Every render is measured** (stretch, nudge, loudness jump, bass pile-up, holes); failures audition other parts, then fall back to an echo-out.
- **Skip to transition** jumps to 10 s before the next join. **Closing the page** stops the server.
- Simulated audit: transitions cutting a lyric mid-line went from 15% to 3%.

### 4.8 API
Pages: `/`, `/p/CODE` (guest), `/p/CODE/host`, `/p/CODE/screen`. JSON under `/api` (see `/docs`): `POST /parties`;
`GET /p/CODE/state`; host only (header `X-Host-Key`): `/p/CODE/advance {swap?}`, `/sync {playhead, status}`,
`/transport {action: play|pause|resume|skip|restart|mc_on|mc_off}`, `/vibe {vibe|level}`, `/mood`, `/visuals`; anyone with
the code: `/request {file, name?}`, `/feedback {guest, target, value}` (the join only for the host), `/react {guest, emoji}`,
`/talk {text}` (guests: requests and mood only). `GET /api/songs`, `/api/search?q=`, `/media/<file>` (a signed bucket URL or
the file), WebSocket `/ws/p/CODE[?key=]`. JSON-only POSTs, 4 KB bodies, only library files are served.

### 4.9 Database
`songs`, `song_analysis` (the full tag), `song_ai` (Gemini answers), `song_features` (rhythm feel, grid slip, sung
segments), `song_embeddings`; `parties` (code, host key hash, the session as JSON), `plays` (one row per join with its
details, the planner's reasons and a FLAC clip of the server's render), `song_requests`, `feedback`, `guest_actions`,
`reactions`, `transition_reviews`, `assets` (MC lines, fills, art), `ai_cache`, `ai_calls`. Tables are created on first
connect; a column added later is added by `db.add_missing_columns`.

## 5. Decisions the user made
- 4 energy levels, held until changed. The user controls playback. No sign-in for now.
- A web app on Google Cloud: stateless server, Cloud SQL, the browser plays and mixes; WebSockets now, at most one
  server instance; LISTEN/NOTIFY when it has to scale (2026-10-14).
- No pitch change in a mix: the server renders each join's overlap with a pitch-keeping stretch and the browser plays
  it (2026-10-15; the first web version changed speed in the browser, and the pitch wobbled through blends).
- Best bit of the next song first; every song reaches a proper verse; parts of 50 to 140 s.
- Never two voices at once; never fade or cut a lyric.
- Requests from the library only, 1 to 3 songs out. No downloader.
- Gemini allowed, `gemini-3.5-flash-lite` only. Talk to the DJ is text only.
- GCP for the data (Cloud SQL, Cloud Storage); the user wires up the project.

## 6. Tests and checks

The synthetic library (`python -m tools.make_test_track --library test_library --vocals`, then
`python -m algo_rhythm.tagger test_library --jobs 3`) has 16 tracks, 14 with a sung line and its exact stem.

| Command | Covers |
|---|---|
| `python -m tests.test_recipe` | a recipe is the join the server rendered (blend with vocal swap, blend 2:1 without stems, echo-out); rebuild exact |
| `python -m tests.test_web` | the API end to end: party, host key, play, advance, sync, a request moved up, votes, reactions, talk, media rules, WebSockets, restart |
| `python -m tests.test_feedback` | clips of each join, votes (upsert, rate limit, song vs mix), Gemini review with a fake answer, analytics export |
| `python -m tests.test_pipeline` | the Cloud Run jobs on local folders (Demucs replaced by the true vocals), two tasks, a rerun, a party from the result |
| `python -m tests.ui.player_browser` | Chromium: player.js renders a join like the reference; a live party (start, advance, skip through a join, pause, a guest) |
| `NODE_PATH=<jsdom@22>/node_modules node tests/ui/ui_test.js` | 122 page checks on the party routes |
| `python -m tests.check_live library_100` | scripted offline sessions on the real library (level changes, requests, vocal alignment) |
| `python -m tests.test_data`, `test_mc`, `test_mood`, `test_filler`, `test_lyrics`, `test_art` | database/storage round trips, the MC, moods, fills, lyric links, art |
| `python -m tests.test_talk`, `test_search` | Talk to the DJ and search; they expect the real library (100+ songs, real vectors) |
| `python -m tools.validate_tags LIBRARY` | every tag file matches the contract |

## 7. Troubleshooting
| Symptom | Fix |
|---|---|
| "Player was updated, reload" | Reload the page. |
| The host page says this browser did not start the party | Only the browser that pressed Start a party holds the host key. Start a new party there, or open the guest page. |
| After a reload the music does not start | Browsers need a tap before sound: press **Play here**. It picks up where the party is. |
| Talk to the DJ says it used simple rules | Gemini is unavailable or the daily cap is used up. A 403 usually means the key does not match `GEMINI_BACKEND`. |
| Audio does not load on Cloud Run | Run `deploy/gcp.sh cors` after the first deploy (the bucket must allow the service's origin). |
| New songs do not appear | Servers check the database every 5 minutes; a new deploy picks them up at once. |
| Google sign-in errors locally | `GOOGLE_APPLICATION_CREDENTIALS` must point at a file that exists, or be unset. |

## 8. Known limits
- Only about 8% of song pairs can blend; most joins are echo-outs. Next step: a shorter blend or a cut on the downbeat.
- One server instance (the WebSocket hub is in-process): fine for a party or a few; `hub.py` says what changes for more.
- The host's browser must stay open and awake: it is the speaker. Phones may sleep; a laptop is the safe host.
- A decoded song takes about 85 MB in the browser; the player keeps only the songs its joins need (about 3).
- No sign-in: anyone with a party code can request, vote and react; only the host's browser can steer.
- A blend that was nudged by up to 25 ms jumps that much at its end (an old quirk of the join, in both players).
