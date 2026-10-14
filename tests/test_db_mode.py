"""Runnable check for database mode (no speakers): python -m tests.test_db_mode LIBRARY

Imports the library into a temp SQLite database, runs a short scripted session through the server's database hooks,
and checks that plays and requests were recorded.
"""
import os
import sys
import tempfile
from pathlib import Path


def main(library):
    library = Path(library).resolve()
    tmp = Path(tempfile.mkdtemp())
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp / 'algo_rhythm.db'}"  # before config.settings() is first read
    from algo_rhythm import db, dj, importer, live, server
    from sqlalchemy import func, select

    importer.main([str(library), "--database-url", os.environ["DATABASE_URL"]])
    sess = live.build(library, "high", history=tmp / "history.json")  # loads songs from the database
    sink = live.FileSink(dj.OUT_SR, tmp / "out.wav")
    sess.playhead = sink.playhead
    player = server.Player(sess, sink, lambda: live.Engine(sess, library, sink, None, [], 200))
    server.record_to_database(player, "high")
    player.request(sess.songs[3]["file"])
    player.engine.start()
    player.engine.join()
    sink.close()
    assert player.engine.error is None, player.engine.error

    from algo_rhythm import repository
    with repository.session() as s:
        plays = s.scalars(select(db.Play).order_by(db.Play.position)).all()
        n_req = s.scalar(select(func.count()).select_from(db.SongRequest))
    audible = [t for t in sess.timeline if t["start_s"] != float("inf")]
    assert len(plays) >= 2 and len(plays) >= len(audible) - 1, (len(plays), len(audible))
    assert plays[1].transition and plays[1].transition.get("type") in ("blend", "echo-out"), plays[1].transition
    assert n_req == 1, n_req
    print(f"DB MODE PASS ({len(plays)} plays recorded, {n_req} request)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "library_100")
