# algo-rhythm

A live AI DJ for a Bollywood library (about 400 songs today). Every song is analysed once. The player then plays continuously through the speakers of the machine it runs on and mixes like a DJ. You steer it from a web page: **4 energy levels**, **song requests from the library**, and **Talk to the DJ** (plain words, in English or Hinglish, turned into DJ actions by Gemini).

> Status, numbers and open items: **`PROGRESS.md`**. Architecture diagrams: the "algo-rhythm HLD" artifact.

---

## 1. Rules for anyone working on it

- **Claude cannot hear audio.** "How it sounds" is measured, or judged by the user. The user's ears are the real test.
- **The user controls playback.** Never press Play. Before restarting the server, run `curl -s localhost:8765/state` and only restart if `status` is not `playing`.
- **Gemini: `gemini-3.5-flash-lite` only** for text, through Vertex AI (billed). The gateway caps text requests at 200 a day (`GEMINI_DAILY_CAP`) as a cost guard.
- **No AI call inside the audio path.** Gemini runs offline (song details, transition review), on a Talk to the DJ command (it only sets goals), and once per party in the background for the MC's line pool; rendering is rules and signal processing.
- **No downloads.** Songs come only from the library.
- Tests use their own history file and temp databases; the real play history is never touched.
- Version control is a local git repository only: commit each working change, never push or add a remote.

## 2. Run it

```bash
cd ~/Downloads/song-matcher
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # once; also: brew install rubberband

.venv/bin/python -m algo_rhythm.server library_100          # http://127.0.0.1:8765 (starts idle; closing the page stops it)
```

Pages: `/` admin console, `/guest` (now playing, up next, requests), `/presenter` (big screen, read-only), `/docs` (API).

The server loads the library once at start. **Restart it to pick up new songs or code.**

### Configuration (environment, see `.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `LIBRARY_DIR` | the path given on the command line | Local library folder, or the local cache when audio comes from a bucket |
| `DATABASE_URL` | unset = read the library's JSON files | `postgresql+psycopg://…` (Cloud SQL) or `sqlite:///…` |
| `STORAGE_URL` | unset = audio already local | `gs://bucket/prefix` or `file:///abs/path`; files are fetched into `LIBRARY_DIR` on first use |
| `GEMINI_API_KEY` | `~/.config/song-matcher/gemini_key` | Use Secret Manager in GCP |
| `GEMINI_BACKEND` | `vertex` | `vertex`: Vertex AI express-mode key, global endpoint. `studio`: an AI Studio key (generativelanguage.googleapis.com) |
| `VERTEX_LOCATION`, `GOOGLE_CLOUD_PROJECT` | unset | Use the regional Vertex endpoint of that project instead of the global one |
| `GEMINI_MODEL`, `GEMINI_DAILY_CAP` | `gemini-3.5-flash-lite`, 200 | Text model and the daily cap on text requests |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | Search by meaning (Vertex `:predict`, 768 dimensions); not counted in the cap |
| `HOST`, `PORT`, `LOG_LEVEL` | `127.0.0.1`, 8765, `INFO` | Logs are one JSON object per line |
| `GOOGLE_TTS_API_KEY`, `MC_VOICE` | unset, `en-IN-Wavenet-C` | AI MC voice: Cloud Text-to-Speech (key, or Application Default Credentials), else macOS `say` |

### Deploy with GCP

The player must run on the machine connected to the speakers (the venue laptop). GCP holds the data:

1. **Cloud SQL (PostgreSQL 16):** create a database; point `DATABASE_URL` at it (Cloud SQL Auth Proxy on the laptop, or a private IP).
2. **Cloud Storage:** create a bucket; set `STORAGE_URL=gs://BUCKET/library`. Authenticate with `gcloud auth application-default login` or a service account key.
3. **Secret Manager:** store the Gemini key; export it as `GEMINI_API_KEY` on the laptop.
4. **Load the library once** (tables are created on first connect; no migrations yet):
   ```bash
   DATABASE_URL=... STORAGE_URL=gs://BUCKET/library .venv/bin/python -m algo_rhythm.importer library_100 --upload
   ```
5. **Run the player** on the laptop with the same variables: `python -m algo_rhythm.server`.

`Dockerfile` builds the same app for Linux (pass `--device /dev/snd` for sound) and for one-off jobs on Cloud Run Jobs (the importer, `algo_rhythm.enrich`). On macOS run it natively: Docker cannot reach the Mac's speakers. `docker-compose.yml` runs a local PostgreSQL for development.

### Offline jobs

```bash
.venv/bin/python -m algo_rhythm.tagger library_100 --jobs 10    # analyse new songs (skips up-to-date ones)
.venv-stems/bin/python algo_rhythm/stems.py library_100        # Demucs vocals for new songs
.venv/bin/python -m algo_rhythm.enrich library_100             # Gemini song details for songs that lack them
.venv/bin/python -m algo_rhythm.search library_100             # embed song descriptions that lack a vector
.venv/bin/python -m algo_rhythm.importer library_100           # copy files into the database (idempotent)
.venv/bin/python -m algo_rhythm.art library_100 --dry-run     # song art: what it would make and cost (drop --dry-run to make it; ~$0.04/image)
.venv/bin/python -m algo_rhythm.review [--party N] [--limit 15]  # Gemini scores each join's clip (database mode)
.venv/bin/python -m algo_rhythm.analytics export [--project P]  # tables as JSON lines; with --project, load into BigQuery (sql/analytics.sql)
```

## 3. Folder layout

```
song-matcher/
  README.md  PROGRESS.md  TAGGING_CONTRACT.md  AB_HARNESS.md
  pyproject.toml  Dockerfile  docker-compose.yml  .env.example
  algo_rhythm/
    server.py        FastAPI: pages, JSON API, health check, Talk to the DJ, close-on-tab watchdog
    live.py          session (what plays next), engine (renders each join ~10 s ahead), speaker sink
    live_plan.py     beam-search planner toward the chosen level; requests
    dj.py            pair scores, which part of a song plays, QA, library loading, analysis caches
    transition.py    blend, vocal swap, echo-out, 20 ms sung-segment detector
    tagger.py  stems.py  tag_schema.json      audio analysis, Demucs vocals
    genai.py         the one Gemini gateway: schema check, cache, daily cap, retries, call log
    enrich.py        Gemini song details (energy, style, year, occasion...) and clean title, film, singers
    search.py        search by meaning: song descriptions embedded once, cosine similarity with numpy
    talk.py          Talk to the DJ: words -> level, requests, skip, play, pause; rules fallback
    mc.py            AI MC: when to speak, hype lines (Gemini once per party), text to speech, placement between sung lines
    config.py  db.py  repository.py  storage.py  importer.py   settings, tables, data access, bucket, import
    static/live.html (admin, guest and presenter views), static/ab.html (blind A/B page)
  tools/             validate_tags, ab_harness, feature_study, explain_mix, make_test_track
  tests/             check_live, test_server, test_db_mode, test_data, test_talk, ui/ui_test.js
  lists/             the DJ tracklists the library was built from
  library_100/       the library (not in git): audio, tags/, stems/, enrich/, _mixes/, _retired/
```

## 4. How it works

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
`GET / /guest /presenter /state /songs /healthz /docs`; `POST /vibe {level}`, `/request {file}`, `/feedback {guest, target: song|transition, value: up|down}` (guest page; one vote per guest per play, 10 a minute), `/transport {action: play|pause|restart|skip|bye|mc_on|mc_off}`, `/talk {text}`. Bound to 127.0.0.1 by default; JSON-only POSTs; Host header check; 4 KB bodies; the client never names a path outside the library list. **No sign-in yet.**

### 4.9 Database (when `DATABASE_URL` is set)
`songs`, `song_analysis` (the full tag), `song_ai` (Gemini answers), `song_features` (rhythm feel, grid slip, sung segments), `song_embeddings` (pgvector `vector` column on PostgreSQL when the `pgvector` package and extension are there, JSON otherwise), `parties`, `plays` (one row per song played, with its transition details and the planner's reasons), `song_requests`, `ai_cache`, `ai_calls`, `feedback` (guest votes), `transition_reviews` (Gemini's score of each join). Every join is also saved as a short FLAC (8 s either side of the overlap) in `_mixes/clips/<party>/<position>.flac`, noted on its play. A database write that fails is logged and never stops the music.

## 5. Decisions the user made
- 4 energy levels, held until changed. Laptop speakers. The user controls playback. No sign-in for now.
- Best bit of the next song first; every song reaches a proper verse; parts of 50 to 140 s.
- Never two voices at once; never fade or cut a lyric.
- Requests from the library only, 1 to 3 songs out. No downloader.
- Gemini allowed, `gemini-3.5-flash-lite` only. Talk to the DJ is text only.
- GCP for the data (Cloud SQL, Cloud Storage); the user wires up the project.

## 6. Tests and checks

| Command | Covers |
|---|---|
| `python -m tests.check_live library_100` | scripted sessions without speakers: level changes, requests 1-4 out, max reached and held, now-playing logic, vocal alignment |
| `python -m tests.test_server library_100` | routes, guards (host, content type, body size), close-on-tab, Talk to the DJ wiring, guest and presenter pages |
| `python -m tests.test_db_mode library_100` | import into a temp database, a short session, plays and requests recorded |
| `python -m tests.test_feedback library_100` | clips of each join, guest votes (upsert, rate limit, song vs mix), Gemini review with a fake answer, analytics export |
| `python -m tests.test_data` | database load equals file load for every song; storage round trip |
| `python -m tests.test_mc` | AI MC: scheduling, line pool, Cloud TTS (fake HTTP), macOS `say`, placement and ducking |
| `python -m tests.test_talk` | Talk to the DJ with fake Gemini answers, rules fallback, gateway cache, cap, schema checks |
| `python -m tests.test_search` | ranking, level filter, embedding cache, talk shortlist (fake embedder); one real query check |
| `python -m tools.validate_tags library_100` | every tag file matches the contract |
| `python -m tools.ab_harness selftest library_100` | blind A/B page: blindness, order, loudness |
| `NODE_PATH=<jsdom dir> node tests/ui/ui_test.js` | 47 page checks across admin, guest and presenter, including the search box |

## 7. Troubleshooting
| Symptom | Fix |
|---|---|
| "Player was updated, reload" | Reload the page. |
| New songs don't appear | Restart the server (when not playing). |
| Talk to the DJ says it used simple rules | Gemini is unavailable or the daily cap is used up; it resets the next day. A 403 usually means the key does not match `GEMINI_BACKEND`. |
| SSL certificate error | `truststore` is used; never disable verification. |
| Segfault in `beat_track` | A copied venv; recreate `.venv` with python3.11. |

## 8. Known limits
- Only about 8% of song pairs can blend; most joins are echo-outs. Next step: a shorter blend or a cut on the downbeat for pairs just outside the gates.
- Not yet tested against a real Cloud SQL instance or a `gs://` bucket (SQLite and local folders were tested); the Docker image was not built here.
- No sign-in: anyone who can reach the port can control the music.
- The 25 duplicate audio files in the library are skipped by the importer.
