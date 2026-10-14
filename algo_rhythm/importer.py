"""Load a file-mode library into the database: every tag, Gemini answer, _mixes analysis cache and embedding.
Safe to rerun (rows are updated in place). --upload also copies the audio and its stems to STORAGE_URL.

    python -m algo_rhythm.importer LIBRARY_DIR [--database-url URL] [--upload]
"""
import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from algo_rhythm import db, dj, repository, storage, tagger

def loadable(library, t):
    """Would dj.song_from accept this tag (ignoring duplicates)? Mirrors its checks."""
    return (str(t.get("schema_version", "")).startswith("2.") and (library / t["file"]).exists()
            and "too_few_phrases" not in t.get("quality", {}).get("flags", []))


def import_library(s, library, upload=False):
    caches = {k: json.loads(p.read_text()) if (p := library / "_mixes" / f"{k}.json").exists() else {}
              for k in repository.FEATURE_KINDS}
    stems = sorted((library / "stems").iterdir()) if (library / "stems").is_dir() else []
    tags = [(f, json.loads(f.read_text())) for f in sorted((library / "tags").glob("*.json"))
            if not f.name.startswith("_")]
    # sha256 is unique: of two copies of the same audio keep the one dj.load_library would play (first loadable one)
    tags.sort(key=lambda ft: not loadable(library, ft[1]))
    emb = library / "_mixes" / "embeddings.json"  # search.py: description vectors keyed by sha256
    vectors = json.loads(emb.read_text()) if emb.exists() else {}
    stats = {"songs": 0, "archived": 0, "duplicates": 0, "ai": 0, "features": 0, "embeddings": 0, "uploaded": 0}
    for f, t in tags:
        song = s.scalar(select(db.Song).where(db.Song.file == t["file"]))
        owner = s.scalar(select(db.Song.file).where(db.Song.sha256 == t["sha256"]))
        if owner and owner != t["file"]:
            print(f"skip  {t['file']}: same audio as {owner}")
            stats["duplicates"] += 1
            continue
        song = song or db.Song(file=t["file"])
        song.sha256, song.duration_s = t["sha256"], t["duration_s"]
        e = library / "enrich" / f.name
        a = json.loads(e.read_text()) if e.exists() else None
        song.name = dj.display_name(t, a)
        song.status = "active" if (library / t["file"]).exists() else "archived"  # run it on the full local library
        s.add(song)
        s.flush()
        s.merge(db.SongAnalysis(song_id=song.id, schema_version=str(t.get("schema_version")), tag=t))
        if a:
            s.merge(db.SongAI(song_id=song.id, model=a.get("model"), prompt_version=None, answer=a))
            stats["ai"] += 1
        if t["sha256"] in vectors:
            v = vectors[t["sha256"]]
            s.merge(db.SongEmbedding(song_id=song.id, model=v["model"], dim=len(v["vector"]), vector=v["vector"]))
            stats["embeddings"] += 1
        for kind, cache in caches.items():
            if t["sha256"] in cache:
                s.merge(db.SongFeature(song_id=song.id, kind=kind, data=cache[t["sha256"]]))
                stats["features"] += 1
        if upload and song.status == "active":
            key = tagger.tag_key(t["file"])
            for p in [library / t["file"], *(p for p in stems if p.name.startswith(key + "."))]:
                storage.upload(p, p.relative_to(library).as_posix())
                stats["uploaded"] += 1
        stats["songs"] += 1
        stats["archived"] += song.status == "archived"
    s.commit()
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("library", type=Path)
    ap.add_argument("--database-url", help="default: DATABASE_URL")
    ap.add_argument("--upload", action="store_true", help="also copy audio and stems to STORAGE_URL")
    args = ap.parse_args(argv)
    library = args.library.resolve()
    if not (library / "tags").is_dir():
        sys.exit(f"{library} has no tags/ folder: not a tagged library")
    with repository.session(args.database_url) as s:
        stats = import_library(s, library, args.upload)
    print(f"imported {stats['songs']} songs ({stats['archived']} archived: audio missing), "
          f"{stats['duplicates']} duplicates skipped, {stats['ai']} Gemini answers, {stats['features']} analyses, "
          f"{stats['embeddings']} embeddings"
          + (f", {stats['uploaded']} files uploaded" if args.upload else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
