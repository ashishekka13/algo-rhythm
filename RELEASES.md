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

### Under the hood
- New tables `mc_lines` and `mc_plays`.
- New endpoints: `POST /api/p/CODE/location` and `POST /api/p/CODE/screen`. `/talk` takes the guest's `name`.
- Gemini prompt versions `talk-v4` (dedications) and `mc-line-v1`.
- New tests: `test_variety`, `test_mc_lines`, plus additions to `test_bugs`, `test_web`, `test_mc` and the jsdom
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
