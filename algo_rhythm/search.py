"""Search by meaning: one description per song (title, film, singers, year, mood...), embedded once with the Gemini
embedding model (genai.embed); a query is embedded too and compared by cosine similarity with numpy.

    python -m algo_rhythm.search LIBRARY [--force]      embed songs that have no vector yet

Vectors live in LIBRARY/_mixes/embeddings.json keyed by audio sha256 (file mode) or in the song_embeddings table
(DATABASE_URL). live.build() attaches them to each song as s["vec"]; songs without one are simply not found.
"""
import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

from algo_rhythm import genai
from algo_rhythm.config import settings
from algo_rhythm.live_plan import LEVELS

LEVEL_NAMES = list(LEVELS)  # low, medium, high, max


def song_level(s):
    """The song's energy as one of the 4 levels (its quartile in this library)."""
    return LEVEL_NAMES[min(3, int(s["energy_pct"] * 4))]


def describe(s):
    """The text embedded for a song: what a listener might say about it."""
    llm = s.get("llm") or {}
    title, _, film = s["name"].partition(" - ")
    parts = [f"Song: {title}", f"Film: {film}" if film else "Non-film song",
             "Singers: " + ", ".join(llm["singers"]) if llm.get("singers") else "",
             f"Year: {llm['year']}" if llm.get("year") else "", f"Language: {llm.get('language', '')}",
             f"Style: {llm.get('style', '')}", f"Mood: {llm.get('lyric_mood', '')}",
             "Occasion: " + ", ".join(llm.get("occasion", [])), f"Moment: {llm.get('moment', '')}",
             f"Energy: {song_level(s)}"]
    return ". ".join(p for p in parts if p and not p.endswith(": "))


def _file(library):
    return Path(library) / "_mixes" / "embeddings.json"


def attach(library, songs):
    """Set s["vec"] (numpy, unit length) on every song that has a stored vector. Returns how many have one."""
    if settings().database_url:
        from algo_rhythm import repository
        stored = repository.load_embeddings()  # {file: vector}
        found = {s["file"]: stored.get(s["file"]) for s in songs}
    else:
        p = _file(library)
        rows = json.loads(p.read_text()) if p.exists() else {}
        found = {s["file"]: (rows.get(s["sha256"]) or {}).get("vector") for s in songs}
    for s in songs:
        v = found[s["file"]]
        s["vec"] = None if v is None else np.asarray(v, float) / (np.linalg.norm(v) or 1)
    return sum(s["vec"] is not None for s in songs)


def build(library, songs, force=False, embed=None):
    """Embed every song without a vector for the current model (all of them with force). Returns how many."""
    embed = embed or (lambda texts: genai.embed(texts, task_type="RETRIEVAL_DOCUMENT"))
    model = genai.embed_model()
    db_mode = bool(settings().database_url)
    if db_mode:
        from algo_rhythm import repository
        have = repository.load_embeddings(model)
        todo = [s for s in songs if force or s["file"] not in have]
    else:
        p = _file(library)
        rows = json.loads(p.read_text()) if p.exists() else {}
        todo = [s for s in songs if force or (rows.get(s["sha256"]) or {}).get("model") != model]
    for i in range(0, len(todo), genai.EMBED_BATCH):  # the free tier embeds 100 texts a minute: save each batch
        part = todo[i:i + genai.EMBED_BATCH]
        texts = [describe(s) for s in part]
        try:
            vecs = embed(texts)
        except genai.QuotaError:
            print("embedding quota for this minute used up: waiting 60 s", file=sys.stderr)
            time.sleep(60)
            vecs = embed(texts)
        if db_mode:
            repository.save_embeddings({s["file"]: v for s, v in zip(part, vecs)}, model)
        else:
            rows.update({s["sha256"]: {"model": model, "text": t, "vector": [round(x, 6) for x in v]}
                         for s, t, v in zip(part, texts, vecs)})
            p.write_text(json.dumps(rows))
    return len(todo)


def nearest(query_text, songs, k=20, level=None, embed=None):
    """Indices of the k songs whose description is closest in meaning to `query_text`, best first.
    level: only songs at that energy level. Songs without a vector are skipped; none at all = [].
    The query is embedded once (genai.embed caches it per text). Raises GenAIError when it cannot be embedded.
    ponytail: a numpy scan over every vector, fine for a few thousand songs. At scale ask PostgreSQL instead:
      SELECT song_id FROM song_embeddings ORDER BY vector <=> :query_vector LIMIT :k   (with an HNSW index)."""
    idx = [i for i, s in enumerate(songs) if s.get("vec") is not None and (level is None or song_level(s) == level)]
    if not idx or not query_text.strip():
        return []
    embed = embed or (lambda texts: genai.embed(texts, task_type="RETRIEVAL_QUERY"))
    q = np.asarray(embed([query_text.strip()])[0], float)
    sims = np.stack([songs[i]["vec"] for i in idx]) @ (q / (np.linalg.norm(q) or 1))
    return [idx[j] for j in np.argsort(-sims, kind="stable")[:k]]


FUZZY = 0.85         # a typed word of 3+ letters matches a name word with its first letter and this similar
                     # ("galan" ~ "gallan", "chasma" ~ "chashma"); 0.8 let "malhar" match "maar" and "chik" "Ruchika"
ENOUGH_NAMES = 1     # any name match: no songs similar in meaning, so a name search never waits on the embedding
                     # call (measured 0.65 s normally, 11.8 s on a slow network)


def _words(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def name_score(q, s):
    """How well the typed text names song s: 3 = the text is in its name, 2 = every typed word starts a word of its
    title, film or singers ("chik" -> Chikni, "arijit" -> its singer), 1 = the same allowing typos, 0 = no."""
    if q.lower() in s["name"].lower():
        return 3
    llm = s.get("llm") or {}
    words = _words(" ".join([s["name"], llm.get("title", ""), llm.get("film", ""), *llm.get("singers", [])]))
    typed = _words(q)
    if not typed or not words:
        return 0
    if all(any(w.startswith(t) for w in words) for t in typed):
        return 2
    near = lambda t: len(t) >= 3 and any(w[0] == t[0] and difflib.SequenceMatcher(None, t, w[:len(t) + 1]).ratio() >= FUZZY
                                         for w in words)
    return 1 if all(any(w.startswith(t) for w in words) or near(t) for t in typed) else 0


def name_hits(query, songs):
    """Indexes of the songs the typed text names, best first; the same song name listed once."""
    q = " ".join(str(query).split())[:100]
    if not q:
        return []
    scored = sorted(((-sc, i) for i, s in enumerate(songs) if (sc := name_score(q, s))))
    out, seen = [], set()
    for _, i in scored:
        if songs[i]["name"].lower() not in seen:
            seen.add(songs[i]["name"].lower())
            out.append(i)
    return out


def find(query, songs, k=12, embed=None):
    """The song-search box: songs the text names first (name_hits), then, when there are few, the closest by meaning
    (3+ characters). If the query cannot be embedded, only the name matches."""
    q = " ".join(str(query).split())[:100]
    hits = name_hits(q, songs)
    if len(q) >= 3 and len(hits) < ENOUGH_NAMES:
        names = {songs[i]["name"].lower() for i in hits}
        try:
            for i in nearest(q, songs, k, embed=embed):
                if i not in hits and songs[i]["name"].lower() not in names:
                    names.add(songs[i]["name"].lower())
                    hits.append(i)
        except genai.GenAIError:
            pass
    return hits[:k]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("library", type=Path)
    ap.add_argument("--force", action="store_true", help="embed every song again")
    args = ap.parse_args(argv)
    from algo_rhythm import repository
    library = args.library.resolve()
    songs, _ = repository.load_songs(library)
    n = build(library, songs, args.force)
    print(f"embedded {n} songs; {attach(library, songs)}/{len(songs)} have a vector ({genai.embed_model()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
