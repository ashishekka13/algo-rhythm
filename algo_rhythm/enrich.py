"""Ask Gemini how each song FEELS (dance-floor energy, occasion, familiarity, lyric mood) and what it is called
(clean title, film, singers). Text only: song names and tempo go out, no audio. Results are cached in
LIBRARY/enrich/<key>.json and the planner blends the energy with the measured one (dj.finalize). Songs without a
file keep their measured energy. Songs that already have the other details only get the name fields asked for
(about 45 songs per request); a song whose audio is a copy of another is asked about once.

    export GEMINI_API_KEY=...        (or put the key alone in ~/.config/song-matcher/gemini_key, chmod 600)
    .venv/bin/python -m algo_rhythm.enrich LIBRARY [--force] [--model gemini-3.5-flash-lite]
    DATABASE_URL=... .venv/bin/python -m algo_rhythm.enrich         the songs in the database (song_ai), no files
"""
import argparse
import json
import sys
from pathlib import Path

from algo_rhythm import genai
from algo_rhythm.genai import QuotaError

MODEL = genai.model_name()
BATCH = 40  # the free tier allows only ~20 requests a day per model
PROMPT_VERSION = "enrich-v4"  # v4: rows numbered, answers carry the number (match)
NAME_BATCH = 45

PROMPT = """You are an experienced Bollywood DJ. For each song, judge how it feels on a dance floor.
energy (integer 1-4):
 1 = low: soft, sad or romantic listening songs, nobody dances.
 2 = medium: warm groove, light swaying or sing-along.
 3 = high: party song, most people dance (e.g. a popular dance number).
 4 = max: peak-hour banger that lifts the whole room (wedding sangeet or club peak, e.g. London Thumakda, Badtameez Dil, Malhari).
Judge from what you know about the actual song, not the tempo alone; tempo is given only as a hint.
familiarity (1-5): how widely known it is among Indian listeners. occasion: up to 3 short words (wedding, road trip, club, late night...).
lyric_mood: one or two words. confidence (0-1): how sure you are you know this exact song; use under 0.5 if unsure.
{names}For mixing, also give (pick exactly one value from each list):
 style: ballad | sufi | punjabi | dance_pop | club_edm | item_number | folk | qawwali | rock | hiphop | indie_acoustic
 rhythm: straight (4/4 even eighths) | triplet (6/8, 12/8, dadra or swung shuffle) | dhol (bhangra chaal) | garba | free (no steady beat)
 sound: acoustic | orchestral | electronic | mixed
 language: hindi | punjabi | english | mixed | other
 voice: male | female | duet | group
 year: release year (integer, best guess)
 moment: warmup | groove | peak | singalong | slow_dance | closing  (where a DJ would play it in a night)
Songs, numbered (name is "Song - Movie"; file names may be long YouTube titles). Give each answer the song's number as
id, and its name exactly as given:
"""
NAMES = """Also name each song the way fans know it:
 title: the song title only, in normal capitalisation (no film, "From ...", "Official Video", "Lyrical", YouTube id).
 film: the film or album it is from; "" for a non-film single. singers: the main singers (list of names).
If you do not know the song, take the title from the file name and use "" and [].
"""
PROMPT = PROMPT.replace("{names}", NAMES)
NAMES_PROMPT = ("You are an experienced Bollywood DJ. For each song file below give id (its number), name (exactly as "
                "given), title, film and singers.\n" + NAMES + "Songs, numbered (file names; they may be long YouTube titles):\n")

STYLES = ["ballad", "sufi", "punjabi", "dance_pop", "club_edm", "item_number", "folk", "qawwali", "rock", "hiphop",
          "indie_acoustic"]
RHYTHMS = ["straight", "triplet", "dhol", "garba", "free"]
MOMENTS = ["warmup", "groove", "peak", "singalong", "slow_dance", "closing"]
FIELDS = ["style", "rhythm", "sound", "language", "voice", "year", "moment"]  # schema 2 answers; older files lack them
NAME_FIELDS = ["title", "film", "singers"]  # enrich-v3
NAME_PROPS = {"title": {"type": "STRING"}, "film": {"type": "STRING"}, "singers": {"type": "ARRAY", "items": {"type": "STRING"}}}
NAMES_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "required": ["id", "name"] + NAME_FIELDS,
                                           "properties": {"id": {"type": "INTEGER"}, "name": {"type": "STRING"}, **NAME_PROPS}}}
SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "required": ["id", "name", "energy", "familiarity", "occasion",
          "lyric_mood", "confidence"] + FIELDS + NAME_FIELDS, "properties": {
    **NAME_PROPS, "id": {"type": "INTEGER"}, "name": {"type": "STRING"}, "energy": {"type": "INTEGER"}, "familiarity": {"type": "INTEGER"},
    "occasion": {"type": "ARRAY", "items": {"type": "STRING"}}, "lyric_mood": {"type": "STRING"},
    "confidence": {"type": "NUMBER"},
    "style": {"type": "STRING", "enum": STYLES}, "rhythm": {"type": "STRING", "enum": RHYTHMS},
    "sound": {"type": "STRING", "enum": ["acoustic", "orchestral", "electronic", "mixed"]},
    "language": {"type": "STRING", "enum": ["hindi", "punjabi", "english", "mixed", "other"]},
    "voice": {"type": "STRING", "enum": ["male", "female", "duet", "group"]}, "year": {"type": "INTEGER"},
    "moment": {"type": "STRING", "enum": MOMENTS}}}}


def ask(model, rows):
    return genai.generate("enrich", PROMPT_VERSION,
                          [{"text": PROMPT + "\n".join(f"{k}. {r['name']} ({r['bpm']:.0f} BPM)" for k, r in enumerate(rows, 1))}], SCHEMA,
                          timeout=300, model=model)


def ask_names(model, rows):
    return genai.generate("enrich-names", PROMPT_VERSION, [{"text": NAMES_PROMPT + "\n".join(f"{k}. {r['name']}" for k, r in enumerate(rows, 1))}],
                          NAMES_SCHEMA, timeout=300, model=model)


def clean_names(a):
    return {"title": str(a["title"]).strip()[:80], "film": str(a["film"]).strip()[:80],
            "singers": [str(x).strip()[:40] for x in a["singers"] if str(x).strip()][:4]}


def clean(a):
    return {"energy": min(4, max(1, int(a["energy"]))), "familiarity": min(5, max(1, int(a["familiarity"]))),
            "occasion": [str(o)[:30] for o in a["occasion"]][:3], "lyric_mood": str(a["lyric_mood"])[:30],
            "confidence": min(1.0, max(0.0, float(a["confidence"]))),
            **{k: a[k] for k in FIELDS if k in a}, **clean_names(a)}


def match(rows, raw):
    """Answers keyed by the lowercased name we sent, found by the number each answer carries (the prompt numbers the
    rows: the model may reword a long file name and reorder its reply, so neither the name nor the position is a
    safe key). Dropped: an answer whose number is missing, out of range or given twice, and one whose name is
    exactly another row's name (it was given the wrong number). A dropped song is asked about again next run."""
    names = [r["name"].strip().lower() for r in rows]
    by_id = {}
    for a in raw:
        k = a.get("id")
        if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= len(rows):
            continue
        by_id.setdefault(k, []).append(a)
    answers = {}
    for k, found in by_id.items():
        if len(found) != 1:
            continue
        said = str(found[0].get("name", "")).strip().lower()
        if said != names[k - 1] and said in names:
            continue
        answers[names[k - 1]] = found[0]
    return answers


def merged(old, a, names_only, model):
    """One answer on top of what a song already has (save(), without the file)."""
    if names_only:
        return {**old, **clean_names(a)}
    new = {**clean(a), "model": model}
    if old.get("source") == "tracklist":  # the DJ list's phase wins
        new.update(energy=old["energy"], confidence=old["confidence"], source="tracklist")
    return new


def run_db(model=MODEL, force=False):
    """Database mode: ask about every active song without an answer (song_ai) and store the answers there, with each
    song's display name. Returns (saved, asked)."""
    from sqlalchemy import select
    from algo_rhythm import db, dj, repository
    with repository.session() as s:
        rows = s.execute(select(db.Song.id, db.Song.file, db.SongAnalysis.tag).join(db.SongAnalysis)
                         .where(db.Song.status == "active")).all()
        ai = {a.song_id: a.answer for a in s.scalars(select(db.SongAI))}
    todo, names = [], []
    for sid, file, tag in rows:
        old = ai.get(sid) or {}
        row = {"id": sid, "name": Path(file).stem, "bpm": tag["tempo"]["bpm"], "tag": tag, "old": old}
        if force or "rhythm" not in old:
            todo.append(row)
        elif "title" not in old:
            names.append(row)
    if not todo and not names:
        print("nothing to do: every song already has an answer")
        return 0, 0
    genai.auth_headers()
    done = 0
    jobs = [(todo[i:i + BATCH], ask) for i in range(0, len(todo), BATCH)]
    jobs += [(names[i:i + NAME_BATCH], ask_names) for i in range(0, len(names), NAME_BATCH)]
    for n, (part, fn) in enumerate(jobs, 1):
        try:
            answers = match(part, fn(model, part))
        except QuotaError as e:
            print(str(e), file=sys.stderr)
            break
        except Exception as e:  # keep what is already saved; rerun continues
            print(f"batch {n} failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        with repository.session() as s:
            for r in part:
                a = answers.get(r["name"].strip().lower())
                if a is None:
                    print(f"no answer for {r['name']}", file=sys.stderr)
                    continue
                new = merged(r["old"], a, fn is ask_names, model)
                s.merge(db.SongAI(song_id=r["id"], model=model, prompt_version=PROMPT_VERSION, answer=new))
                song = s.get(db.Song, r["id"])
                song.name = dj.display_name(r["tag"], new)
                done += 1
                print(f"{r['name']}  ->  {song.name}")
            s.commit()
    return done, len(todo) + len(names)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("library", type=Path, nargs="?", help="a library folder; without one, the database (DATABASE_URL)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args(argv)
    if args.library is None:
        done, total = run_db(args.model, args.force)
        print(f"\n{done}/{total} saved to the database")
        return 0 if done == total else 1
    lib, out = args.library.resolve(), args.library.resolve() / "enrich"
    out.mkdir(exist_ok=True)
    todo, names, copies = [], [], {}  # copies: sha256 -> keys of every tag with that audio
    for f in sorted((lib / "tags").glob("*.json")):
        if f.name.startswith("_"):
            continue
        t = json.loads(f.read_text())
        first = t["sha256"] not in copies
        copies.setdefault(t["sha256"], []).append(f.stem)
        if not first:
            continue
        row = {"key": f.stem, "name": Path(t["file"]).stem, "bpm": t["tempo"]["bpm"], "sha": t["sha256"]}
        old = json.loads((out / f.name).read_text()) if (out / f.name).exists() else {}
        if args.force or "rhythm" not in old:
            todo.append(row)
        elif "title" not in old:
            names.append(row)
    if not todo and not names:
        print("nothing to do: every song already has an answer")
        return 0
    try:
        genai.auth_headers()
    except genai.GenAIError as e:
        sys.exit(str(e))
    done = 0
    jobs = [(todo[i:i + BATCH], ask) for i in range(0, len(todo), BATCH)]
    jobs += [(names[i:i + NAME_BATCH], ask_names) for i in range(0, len(names), NAME_BATCH)]
    for n, (rows, fn) in enumerate(jobs, 1):
        try:
            answers = match(rows, fn(args.model, rows))
        except QuotaError as e:
            print(str(e), file=sys.stderr)
            break
        except Exception as e:  # keep what is already saved; rerun continues
            print(f"batch {n} failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        for r in rows:
            a = answers.get(r["name"].strip().lower())
            if a is None:
                print(f"no answer for {r['name']}", file=sys.stderr)
                continue
            for key in copies[r["sha"]]:
                save(out / f"{key}.json", a, fn is ask_names, args.model)
            done += 1
            print(f"{r['name']}  ->  {a['title']} | {a['film']} | {', '.join(a['singers'])}")
    total = len(todo) + len(names)
    print(f"\n{done}/{total} saved to {out}")
    return 0 if done == total else 1


def save(path, a, names_only, model):
    """Write one answer, keeping what the file already has: name fields are added; a full answer replaces the
    rest, except that the DJ tracklist's energy wins."""
    old = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps(merged(old, a, names_only, model), indent=1))


if __name__ == "__main__":
    sys.exit(main())
