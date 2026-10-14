"""Export the database to newline-delimited JSON, one file per table, and optionally load it into BigQuery.

    python -m algo_rhythm.analytics export [--out DIR] [--project P] [--dataset algo_rhythm]

Tables: plays (song, energy, transition type, match, planner terms, clip), feedback, transition_reviews,
song_requests, ai_calls, songs (with Gemini's song details). Each load replaces the BigQuery table.
Example queries: sql/analytics.sql.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from algo_rhythm import db, repository
from algo_rhythm.config import settings


def _plain(row, cols):
    """A table row as a JSON-ready dict: datetimes as ISO text."""
    out = {}
    for c in cols:
        v = getattr(row, c)
        out[c] = v.isoformat() if isinstance(v, datetime) else v
    return out


def _flat(prefix, d):
    """{"a": 1, "b": [..]} -> {"prefix_a": 1, "prefix_b": [..]}; nested objects become JSON text (BigQuery STRING)."""
    return {f"{prefix}_{k}": json.dumps(v) if isinstance(v, dict) else v for k, v in (d or {}).items()}


def rows(s):
    """{table name: [row dicts]} for every exported table."""
    songs = {}
    for song, ai in s.execute(select(db.Song, db.SongAI).outerjoin(db.SongAI, db.SongAI.song_id == db.Song.id)):
        ans = {k: v for k, v in (ai.answer if ai else {}).items() if k != "model"}
        songs[song.id] = {**_plain(song, ("id", "file", "name", "duration_s", "status", "created_at")),
                          "gemini_model": ai.model if ai else None, **_flat("gemini", ans)}
    plays = []
    for p in s.scalars(select(db.Play).order_by(db.Play.id)):
        why, t = p.why or {}, p.transition or {}
        plays.append({**_plain(p, ("id", "party_id", "position", "song_id", "start_s", "end_s", "clip", "played_at")),
                      "song_name": songs.get(p.song_id, {}).get("name"), "level": why.get("level"),
                      "energy": why.get("energy"), "transition_type": t.get("type") if t else "start",
                      "match": why.get("match"), "score": why.get("score"), "why": why.get("why"),
                      **_flat("term", why.get("terms")), "transition": json.dumps(t) if t else None})
    plain = lambda model, cols: [_plain(r, cols) for r in s.scalars(select(model).order_by(model.id))]
    return {
        "plays": plays,
        "feedback": plain(db.Feedback, ("id", "party_id", "position", "guest", "target", "value", "created_at")),
        "transition_reviews": plain(db.TransitionReview, ("id", "play_id", "model", "prompt_version", "score", "smooth",
                                                          "issues", "summary", "created_at")),
        "song_requests": plain(db.SongRequest, ("id", "party_id", "text", "song_id", "status", "created_at")),
        "ai_calls": plain(db.AICall, ("id", "task", "model", "prompt_version", "cached", "ok", "latency_ms", "error",
                                      "created_at")),
        "songs": list(songs.values()),
    }


def export(out):
    """Write <out>/<table>.json (newline-delimited JSON); returns {table: row count}."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with repository.session() as s:
        tables = rows(s)
    for name, rs in tables.items():
        (out / f"{name}.json").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rs))
    return {name: len(rs) for name, rs in tables.items()}


def load_bigquery(out, project, dataset):
    """Load every <out>/<table>.json into project.dataset.<table> (schema auto-detected, table replaced).
    UNTESTED here: there were no GCP credentials on the machine it was written on."""
    from google.cloud import bigquery
    client = bigquery.Client(project=project)
    client.create_dataset(f"{project}.{dataset}", exists_ok=True)
    config = bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON, autodetect=True,
                                    write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
    for f in sorted(Path(out).glob("*.json")):
        if f.stat().st_size == 0:  # autodetect needs at least one row
            print(f"{f.stem}: no rows, not loaded")
            continue
        with f.open("rb") as fh:
            client.load_table_from_file(fh, f"{project}.{dataset}.{f.stem}", job_config=config).result()
        print(f"{f.stem}: loaded into {project}.{dataset}.{f.stem}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["export"])
    ap.add_argument("--out", type=Path, default=Path("analytics_export"))
    ap.add_argument("--project", help="GCP project: also load the files into BigQuery")
    ap.add_argument("--dataset", default="algo_rhythm")
    args = ap.parse_args(argv)
    if not settings().database_url:
        raise SystemExit("set DATABASE_URL: the export reads the database")
    for name, n in export(args.out).items():
        print(f"{name}: {n} rows -> {args.out / (name + '.json')}")
    if args.project:
        load_bigquery(args.out, args.project, args.dataset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
