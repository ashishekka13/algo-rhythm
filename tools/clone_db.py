"""Copy the library (and the host accounts) from one database to another, e.g. production into staging:

    python -m tools.clone_db SOURCE_URL TARGET_URL [--no-accounts]

The target gets every table (db.create_all) and a copy of what a server needs to run: songs and everything measured or
written about them, the shared assets and the AI answer cache, and the host accounts (password hashes, so the same
sign-ins work; --no-accounts leaves them out). Never copied: parties, plays, votes, reactions, requests, reviews,
sign-in sessions and the logs. Rows already in the target are replaced, so it can be run again to refresh it.
Both URLs are SQLAlchemy URLs (postgresql+psycopg://... or sqlite:///...).
"""
import argparse
import sys

from sqlalchemy import create_engine, delete, func, select, text

from algo_rhythm import db

LIBRARY = ["songs", "song_analysis", "song_ai", "song_features", "song_embeddings", "assets", "ai_cache"]
ACCOUNTS = ["users"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("target")
    ap.add_argument("--no-accounts", action="store_true")
    args = ap.parse_args(argv)
    if args.source == args.target:
        sys.exit("source and target are the same database")
    src, dst = create_engine(args.source), create_engine(args.target)
    db.create_all(dst)
    names = LIBRARY + ([] if args.no_accounts else ACCOUNTS)
    tables = [t for t in db.Base.metadata.sorted_tables if t.name in names]
    with src.connect() as s, dst.begin() as d:
        for t in reversed(tables):  # children first, so foreign keys allow the delete
            d.execute(delete(t))
        for t in tables:
            rows = [dict(r) for r in s.execute(select(t)).mappings()]
            for k in range(0, len(rows), 500):
                d.execute(t.insert(), rows[k:k + 500])
            print(f"{t.name}: {len(rows)} rows")
        if dst.dialect.name == "postgresql":  # new rows in the target must not reuse the copied ids
            for t in tables:
                pk = list(t.primary_key.columns)
                if len(pk) == 1 and pk[0].autoincrement is not False and isinstance(pk[0].type, db.Integer):
                    top = d.scalar(select(func.max(pk[0])))
                    if top:
                        d.execute(text(f"SELECT setval(pg_get_serial_sequence('{t.name}', '{pk[0].name}'), {int(top)})"))


if __name__ == "__main__":
    main()
