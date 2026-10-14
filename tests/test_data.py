"""Runnable check for the database + storage layer (plain asserts, temp dirs only; the library is only read).

    python -m tests.test_data [LIBRARY]      default: library_100 (or ../song-matcher/library_100 from a worktree)
"""
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="algorhythm-data-")).resolve()
os.environ.update(DATABASE_URL=f"sqlite:///{tmp}/algo_rhythm.db", STORAGE_URL=f"file://{tmp}/remote",
                  LIBRARY_DIR=str(tmp / "cache"))

import numpy as np  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from algo_rhythm import db, dj, importer, repository, storage  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = next((p for p in (ROOT / "library_100", ROOT.parent / "song-matcher" / "library_100") if p.exists()),
               ROOT / "library_100")


def check_library(library):
    url = os.environ["DATABASE_URL"]
    assert importer.main([str(library), "--database-url", url]) == 0
    assert importer.main([str(library), "--database-url", url]) == 0  # rerun: updates, no new rows
    with repository.session() as s:
        n = s.scalar(select(func.count()).select_from(db.Song))
        assert n == s.scalar(select(func.count()).select_from(db.SongAnalysis)), "one analysis per song"
    files, _ = dj.load_library(library)
    rows, _ = repository.load_songs(library)
    assert len(rows) == len(files) > 8, (len(rows), len(files))
    for a, b in zip(files, rows):
        assert a["file"] == b["file"] and a["sha256"] == b["sha256"], (a["file"], b["file"])
        for k in ("energy_pct", "triplet", "slips", "segs", "llm"):
            assert a[k] == b[k], (a["file"], k)
        assert np.array_equal(a["beats"], b["beats"]) and np.allclose(a["timbre"], b["timbre"]), a["file"]
        assert a.keys() == b.keys(), a["file"]
    print(f"library: {len(rows)} songs from the database match file mode ({n} rows)")
    return rows


def check_writes(songs):
    party = repository.start_party("high")
    repository.record_play(party, 1, songs[0]["file"], 12.5, 80.0, {"type": "blend"}, {"match": 0.8})
    req = repository.record_request(party, "tera ban jaunga", songs[1]["file"], "queued")
    with repository.session() as s:
        play = s.scalar(select(db.Play).where(db.Play.party_id == party))
        assert (play.position, play.start_s, play.end_s, play.transition, play.why) == (
            1, 12.5, 80.0, {"type": "blend"}, {"match": 0.8})
        assert s.get(db.Song, play.song_id).file == songs[0]["file"]
        r = s.get(db.SongRequest, req)
        assert (r.text, r.status, s.get(db.Song, r.song_id).file) == ("tera ban jaunga", "queued", songs[1]["file"])
    assert repository.cache_get("k1") is None
    repository.cache_put("k1", "parse", "m", "v1", {"q": 1}, {"a": 1})
    repository.db_cache.put("k1", "parse", "m", "v1", {"q": 1}, {"a": 2})  # same key: replaced
    assert repository.cache_get("k1") == repository.db_cache.get("k1") == {"a": 2}
    repository.log_ai_call("parse", "m", "v1", True, True, 12, None)
    repository.log_ai_call("parse", "m", "v1", False, False, 900, "timeout")
    with repository.session() as s:
        assert [(c.cached, c.ok, c.error) for c in s.scalars(select(db.AICall).order_by(db.AICall.id))] == [
            (True, True, None), (False, False, "timeout")]
    print("writes: party, play, request, AI cache and call log round-trip")


def check_storage():
    src = tmp / "x.bin"
    src.write_bytes(os.urandom(1000))
    storage.upload(src, "songs/a b/x.bin")
    assert (tmp / "remote/songs/a b/x.bin").read_bytes() == src.read_bytes()
    p = storage.local_path("songs/a b/x.bin")
    assert p == tmp / "cache/songs/a b/x.bin" and p.read_bytes() == src.read_bytes()
    assert not storage.local_path("songs/missing.mp3").exists()
    print("storage: file:// upload and download round-trip")


if __name__ == "__main__":
    songs = check_library(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT)
    check_writes(songs)
    check_storage()
    print(f"PASS  (temp dir {tmp})")
