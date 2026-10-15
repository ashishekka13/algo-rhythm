# algo-rhythm release notes

## How versions work

- `main` is what production runs. Every merge of `staging` into `main` is a release and gets the next version
  number: a git tag `vX.Y` on the merge commit, deployed to production (`TAG=<commit> deploy/gcp.sh server`).
- **Minor** (v1.1 → v1.2): a release of new features and fixes from `staging`.
  **Patch** (v1.1 → v1.1.1): an urgent fix to production on its own.
  **Major** (v1 → v2): a change that breaks something users or other systems rely on.
- New work goes on `staging` and runs on the staging service first. Its notes collect under **Unreleased** below;
  at the merge, that heading becomes the new version and date.

---

## v1.1 (Unreleased, on staging)

Staging: https://algorythm-web-staging-685886597239.asia-south1.run.app (revision `algorythm-web-staging-00006`,
commit `e985a13`). Changes since v1.0:

### New: staging environment
- A separate Cloud Run service, `algorythm-web-staging`, deployed from the `staging` branch.
- Its own database, `algorhythm_web_staging`: a copy of production's song library and host accounts, without
  production's parties. Refresh it with `STAGE=staging deploy/gcp.sh staging-db`.
- `deploy/gcp.sh` takes `STAGE=staging` for the server and CORS. Bucket CORS updates now add to the allowed
  origins instead of replacing them. The analysis jobs stay production-only.

### Host page performance
- **Low-power mode, on by default:** the host page no longer runs the light-garden animation, blurred background,
  glass blur or other animations. Main-thread CPU while playing dropped from 72% to 2–5% of a core.
- A **Low power** switch in the header turns the visuals back on; each browser remembers the choice. The big screen
  keeps its full visuals.
- **Guest phones run light too, always:** no light-garden animation, blurred background, glass blur or moving art.
  The page's CPU in Chromium at phone size dropped from 76% to 1% of a core, and the jitter is gone. The busy spinner
  and the listening dot still animate.

### Song selection: nights no longer start alike
- **The opening song varies:** a weighted pick among the good openers (within 0.25 of the best), not always the
  same one.
- **Near-ties for the next song vary too;** a clearly better transition still always wins.
- **A new party knows its host's last 7 days** and holds back songs played recently.
- On the real library: 40 new parties opened with 8 different songs. One host's 5 parties in a row repeated only
  2 songs out of 25, and transitions stayed as smooth (average match 0.66, against 0.60 before).

### Big screen
- **QR on big screen** switch on the host page shows or hides the join QR code and party code on the big screen,
  live (within about 0.1 s). Only the host can change it.

### Guest and host fixes
- **Talk to the DJ responds on phones:**
  - Send shows a spinner and "Sending", and extra taps don't send the message twice.
  - Speak shows when it's listening.
  - Every button visibly reacts to a tap.
- **No more long waits when Gemini is busy:** the server answers within 8 s, using the simple keyword rules if
  needed. The reply says why ("the AI is busy / took too long / is unavailable"), and the message stays in the box
  to send again.
- **Play/Pause stays in one place:** it no longer changes size or moves when music starts, pauses or resumes.
- **Guests are asked their name** when they join. It shows with their song requests; returning guests aren't asked
  again.
- **Reloading the host page mid-party works:** no more "Could not load audio … decodeAudioData" error. The main
  button says **Play here** and resumes from where the party is.

### AI MC
- **Knows where the party is:**
  - The host's city and country are guessed from their connection (free DB-IP City Lite database; the IP itself
    isn't kept).
  - Well-known cities are named ("What's up Bengaluru"), otherwise the country.
  - The host can change it in **MC location** on the host page.
- **Fresh lines written for the moment** by Gemini: place, energy and mood, a rising vibe, and unlike recent lines.
  Each line is checked: no song names, no other places, no names in general lines, nothing rude, at most 10 words.
- **Line library with reuse:**
  - Every spoken line is kept (`mc_lines`) with how it may be reused: anywhere, only at that place, or (dedications)
    only in its own party.
  - A party never hears a line twice, and a host doesn't hear the same line again within 7 days (`mc_plays`).
  - About a third of lines are new, the rest are reused recordings: less cost and waiting, and the MC keeps talking
    when Gemini is busy.
- **Dedications:**
  - "Dedicate it to Ishan" in Talk to the DJ (guests too) becomes a short MC line, said by name over that song's
    start.
  - If the song starts with singing and there's no gap, the MC says "That one was for Ishan!" at the next
    transition.
- **Fix:** one failed speech request no longer silences the MC until the next deploy; the voice is tried again
  after 5 minutes.

### New: song trivia
- **One multiple-choice question per song on the big screen; guests answer on their phones for coins.** The host
  turns it on with the **Trivia** switch (off by default).
- **The round:**
  - the question opens 30 s into the song and takes answers for 20 s;
  - a 10 s reveal shows the right option, how many got it, a "Did you know?" fact and the top-5 leaderboard;
  - it follows the playhead, so pausing pauses the round;
  - songs whose part is too short get no round.
- **Questions:** about 1 in 4 from our own song data (who sings it, the year), 3 in 4 lesser-known facts from
  Gemini, written ahead of time by the `trivia` job. No AI runs during a party. Gemini's questions are checked and
  dropped if Gemini isn't sure, if the answer is on screen, or if the question gives it away.
- **Blind re-check of every Gemini fact:** Gemini answers the question again without seeing the answer, 3 times with
  the options in a new order each time. The fact is kept only if it's right every time and never flagged as
  ambiguous. This catches wrong and ambiguous questions; in a first sample, about a quarter were one or the other.
  `--refresh` writes and checks the facts again for every song.
- **Scoring on the server:**
  - 10 coins for a right answer, +5 within the first 5 s;
  - one answer per guest per question, and late answers are refused;
  - the right option never reaches a screen before the reveal, and a guest's coins don't show right or wrong until
    then;
  - coins are per party, shown on each guest's phone.
- **Smooth on phones:**
  - the question is a sheet that slides up over the page, so nothing underneath moves;
  - its buttons are made once per question, so a tap can't land on a replaced button;
  - the countdown runs on the phone and goes down a second at a time;
  - answers no longer push an update to every screen;
  - a refused answer stays locked, with the reason shown.
- New tables `song_trivia` and `trivia_answers`; a `trivia` job (production, plus a staging copy); endpoints
  `POST /api/p/CODE/trivia` (host switch), `POST /api/p/CODE/answer` and `GET /api/p/CODE/coins`.

### Performance and capacity
- **No more slow memory growth:**
  - Files the server makes (transition clips, MC voices, overlap audio) are deleted locally once uploaded.
  - Songs fetched from the bucket are kept up to 1 GB, least recently used out first. On Cloud Run the disk is the
    instance's memory, so a long night used to fill it until the instance crashed.
- **Faster transitions:**
  - A blend's stretches (2, or 6 with a vocal swap) run side by side.
  - When the planned part of a song fails its checks, the other parts are tried side by side, and the first that
    passes in order still wins. The audio is bit-identical to before.
  - Locally the 5-transition test set took 13.9 s instead of 19.1 s.
- **Production server:** gen2 runtime, 4 vCPU, 250 open connections per instance (was 40), two transitions prepared
  at once. Measured on 4 vCPU: median 7.2 s per transition, 90th percentile 18.6 s, worst 42 s (was 12.7, 43 and 71 s).
  Staging stays small: one instance, 2 vCPU.
- **Fewer database calls:** the host's and guests' live views are built from one read per second per party. The
  connection pool is sized for a database that production and staging share.
- **Ready for more than one instance:** a change on one instance reaches screens on another at once through
  PostgreSQL `LISTEN/NOTIFY` (0.02 s in the two-process test). The instance limit stays at 1 until the database tier
  is raised (`db-f1-micro` allows 25 connections, about 22 already in use).

### Under the hood
- New tables `mc_lines` and `mc_plays`.
- New endpoints: `POST /api/p/CODE/location` and `POST /api/p/CODE/screen`. `/talk` takes the guest's `name`.
- Gemini prompt versions `talk-v4` (dedications) and `mc-line-v1`.
- New tests: `test_variety`, `test_mc_lines`, `test_instances`, `test_trivia`, plus additions to `test_bugs`, `test_web`, `test_mc` and the jsdom
  and Chromium UI tests.

---

## v1.0 (2026-10-15)

The first release of the web app (production revision `algo-rhythm-web-00011`, commit `f731797`):
- A stateless server on Cloud Run with Cloud SQL. The host's browser plays and mixes, and each transition is
  rendered by the server with pitch-preserving stretching.
- Live updates over WebSockets, a QR code to join, and host accounts (sign-in to host; new accounts wait for
  approval).
- Gemini through a service account, Cloud Run jobs for analysis, song details and art.
- The Bugs.md fixes:
  - faster, lighter transition renders (window-only reads, window cache, two renders at a time, `grid_slip` only
    when needed);
  - per-song vibe ranks;
  - Gemini answers matched by row number;
  - an absolute floor for vocal activity and for voice gates.
