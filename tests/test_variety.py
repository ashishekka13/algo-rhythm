"""Runnable check that nights do not all start alike:

    python -m tests.test_variety [LIBRARY]      (default test_library, see tests/test_recipe.py)

Temp SQLite database (TEST_DATABASE_URL: PostgreSQL instead).
  1. fresh parties (no history) do not all open with the same song, and each opener is a good one (within the margin);
  2. a host's next party knows what they played lately: the songs of their last party lose points, so it opens with
     another song, and another host's party is not affected;
  3. one party re-planning with nothing changed shows the same next song (the page does not flicker);
  4. without a seed (the offline engine, the other tests) the choice is the single best, as before.
"""
import os
import sys
import tempfile
from pathlib import Path


def main(library="test_library"):
    library = Path(library).resolve()
    tmp = Path(tempfile.mkdtemp())
    os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp / 'algo_rhythm.db'}"
    os.environ["LIBRARY_DIR"] = str(library)
    os.environ["MC_TTS"] = "none"
    os.environ.pop("STORAGE_URL", None)
    from algo_rhythm import accounts, importer, library as lib_mod, live, live_plan, party, repository, db
    from sqlalchemy import select
    importer.main([str(library), "--database-url", os.environ["DATABASE_URL"]])
    lib_mod.reset()
    lib = lib_mod.get()
    accounts.create("dj_a", "a@example.com", "password-a", active=True)
    accounts.create("dj_b", "b@example.com", "password-b", active=True)
    with repository.session() as s:
        a_id, b_id = (s.scalar(select(db.User.id).where(db.User.username == n)) for n in ("dj_a", "dj_b"))
    opener = lambda code: party.read(code)[1]["timeline"][0]["entry"]["song"]

    # 1. fresh parties: different good openers
    planner = lib.planner()
    scores = {}
    for i, s in enumerate(lib.songs):
        if planner.intl(i):
            continue
        import math
        for w in __import__("algo_rhythm.dj", fromlist=["dj"]).candidate_windows(s, planner.excerpt_s + live_plan.BUILD_EXTRA_S, (), force_first=True):
            if w["natural_start"]:
                sc = math.exp(-((s["energy_pct"] - live_plan.LEVELS["medium"]) / 0.2) ** 2) + 0.25 * w["in_q"] + 0.2 * (1 - w["in_vocal"])
                scores[s["file"]] = max(scores.get(s["file"], -9), sc)
    best = max(scores.values())
    openers = []
    for _ in range(20):
        code, _ = party.create("medium")
        party.start(code)
        openers.append(opener(code))
    # random seeds: how many differ varies; the 16 test songs have only a few good openers (the real library: 8 of 8)
    assert len(set(openers)) >= 2, f"20 fresh parties all opened with {openers[0]}"
    assert all(scores[f] >= best - live_plan.OPEN_MARGIN - 1e-9 for f in openers), "every opener is a good one"
    print(f"1. 20 fresh parties opened with {len(set(openers))} different songs, all within {live_plan.OPEN_MARGIN} of the best")

    # 2. a host's next party avoids what they just played; another host is not held back by it
    first, _ = party.create("medium", a_id)
    party.start(first)
    for _ in range(3):  # a few songs into the night
        tl = party.read(first)[1]["timeline"]
        party.sync(first, tl[-1]["start_s"] + 1, "playing")
        assert party.advance(first)["status"] == "ready"
    with repository.session() as s:
        heard = set(s.scalars(select(db.Song.file).join(db.Play, db.Play.song_id == db.Song.id)
                              .join(db.Party, db.Party.id == db.Play.party_id).where(db.Party.code == first)))
    assert len(heard) >= 3, heard
    hist = party.recent_plays(a_id, -1)
    assert set(hist) == heard and party.recent_plays(b_id, -1) == {} and party.recent_plays(None, -1) == {}
    for _ in range(5):
        nxt, _ = party.create("medium", a_id)
        party.start(nxt)
        st = party.read(nxt)[1]
        # the host's last week: the first night, and the openers of the parties made in this loop so far
        assert all(st["history"].get(f, 0) >= n for f, n in hist.items()), "the new party starts knowing the host's last week"
        assert opener(nxt) not in heard, f"opened with {opener(nxt)}, heard in the host's last party"
    print(f"2. after a night of {len(heard)} songs, the same host's next 5 parties opened with songs not heard in it")

    # 3. re-planning with nothing changed keeps the same next song
    code, _ = party.create("medium")
    party.start(code)
    q1 = party.read(code)[1]["queue"][0]["song"]
    for _ in range(3):
        party.act(code, lambda sess, st: sess._plan())
        assert party.read(code)[1]["queue"][0]["song"] == q1, "the next song stays put"
    print("3. re-planning with nothing changed keeps the same next song")

    # 4. no seed: the single best, as before
    s1 = live.Session(lib.songs, lib.planner(), "medium", lib.root, None)
    s2 = live.Session(lib.songs, lib.planner(), "medium", lib.root, None)
    e1, e2 = s1.start(None), s2.start(None)
    assert e1["song"] == e2["song"] and s1.queue[0]["song"] == s2.queue[0]["song"]
    assert scores[lib.songs[e1["song"]]["file"]] == best
    print("4. without a seed the opener is the best-scoring song every time")
    print("VARIETY PASS")


if __name__ == "__main__":
    main(*sys.argv[1:])
