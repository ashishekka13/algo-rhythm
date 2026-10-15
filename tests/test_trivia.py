"""Runnable check of song trivia (algo_rhythm.trivia), with a fake Gemini:

    python -m tests.test_trivia [LIBRARY]      (default test_library, see tests/test_recipe.py)

Temp SQLite database (TEST_DATABASE_URL: PostgreSQL instead).
  1. the job: questions from our song data for every song (who sings it, the year) and Gemini's facts, the bad ones
     dropped (not 4 options, no valid answer, unsure, the answer on screen, an option given away in the question);
  2. a round from the playhead: before, asking, reveal, after; none when the song's part is too short;
  3. a party with trivia on: every screen sees the question while it is open, never the answer; the reveal brings
     the answer, the fact, how many got it and the leaderboard; trivia off = no round;
  4. answers through the API: right and quick = 15 coins, wrong = 0, twice, late, a bad choice refused; a guest's
     coins do not tell right from wrong before the reveal;
  5. about 1 question in 4 comes from our data, the rest are Gemini's facts.
"""
import os
import random
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
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select
    from algo_rhythm import db, importer, library as lib_mod, party, repository, server, trivia
    importer.main([str(library), "--database-url", os.environ["DATABASE_URL"]])
    lib_mod.reset()
    lib = lib_mod.get()

    # 1. the job, with Gemini's answers good and bad
    def fake_generate(task, version, parts, schema, **k):
        text = parts[0]["text"]
        nums = [int(line.split(" | ")[0]) for line in text.splitlines() if " | " in line and line.split(" | ")[0].isdigit()]
        out = []
        for n in nums:
            song = lib.songs[n]
            film = (song.get("llm") or {}).get("film") or "Some Film"
            out += [
                {"n": n, "question": "Who composed the music for this song?", "options": ["Pritam", "A. R. Rahman", "Vishal-Shekhar", "Amit Trivedi"],
                 "answer": 1, "fact": "The tune was composed in one night.", "confidence": 0.95},
                {"n": n, "question": "Which award did it win?", "options": ["Filmfare", "IIFA", "Zee Cine", "Screen"],
                 "answer": 0, "fact": "It won Best Music.", "confidence": 0.9},
                {"n": n, "question": "Three options only?", "options": ["a", "b", "c"], "answer": 0, "fact": "x", "confidence": 1},
                {"n": n, "question": "An unsure one", "options": ["a", "b", "c", "d"], "answer": 0, "fact": "x", "confidence": 0.3},
                {"n": n, "question": "Which film is it from?", "options": [film, "Other A", "Other B", "Other C"],
                 "answer": 0, "fact": "On screen already.", "confidence": 1},
                {"n": n, "question": "Was it Pritam who composed it?", "options": ["Pritam", "Amit Trivedi", "Anu Malik", "Salim"],
                 "answer": 0, "fact": "Given away.", "confidence": 1},
                {"n": n, "question": "Bad index", "options": ["a", "b", "c", "d"], "answer": 7, "fact": "x", "confidence": 1},
            ]
        return out
    stored = trivia.run(generate=fake_generate)
    with repository.session() as s:
        by_kind = dict(s.execute(select(db.SongTrivia.kind, func.count()).group_by(db.SongTrivia.kind)).all())
        songs_with = s.scalar(select(func.count(func.distinct(db.SongTrivia.song_id))))
        facts = [q.question for q in s.scalars(select(db.SongTrivia).where(db.SongTrivia.kind == "fact"))]
    n = len(lib.songs)
    assert songs_with == n and by_kind["fact"] == 2 * n, (songs_with, by_kind)
    assert set(facts) == {"Who composed the music for this song?", "Which award did it win?"}, set(facts)
    assert by_kind["data"] >= n, by_kind
    assert trivia.run(generate=fake_generate) == 0, "songs with questions are not asked again"
    print(f"1. {stored} questions for {n} songs: {by_kind['data']} from our data, {by_kind['fact']} facts; "
          "5 kinds of bad answer dropped")

    # 2. a round from the playhead
    ph = trivia.phase
    assert ph(0, 100, 10)[0] == "before" and ph(0, 100, 35)[0] == "asking" and ph(0, 100, 55)[0] == "reveal"
    assert ph(0, 100, 70)[0] == "after" and ph(0, 60, 35) is None and ph(0, None, 35) is None
    assert abs(ph(100, 300, 135)[1] - 15) < 1e-9
    print("2. rounds: question at 30 s, answers for 20 s, reveal for 10 s; none when the part ends too soon")

    # 3 and 4. a party with trivia on, through the API
    c = TestClient(server.create_app())
    code, key = party.create("medium")
    H = {"X-Host-Key": key}
    party.start(code)
    assert c.get(f"/api/p/{code}/state").json()["trivia"] is None
    assert c.post(f"/api/p/{code}/trivia", json={"on": True}).status_code == 403, "host only"
    assert c.post(f"/api/p/{code}/trivia", json={"on": True}, headers=H).json()["trivia_on"] is True
    st = party.read(code)[1]
    t0 = st["timeline"][0]
    assert t0.get("trivia"), "the opening song has its question"
    party.sync(code, 10, "playing")
    assert c.get(f"/api/p/{code}/state").json()["trivia"] is None, "not before 30 s"
    party.sync(code, 31, "playing")
    for v in (c.get(f"/api/p/{code}/state").json(), c.get(f"/api/p/{code}/state", headers=H).json()):
        tv = v["trivia"]
        assert tv["phase"] == "asking" and len(tv["options"]) == 4 and "answer" not in tv and "fact" not in tv, tv
    rnd = tv["round"]
    q = trivia.question(t0["trivia"])
    right, wrong = q.answer, (q.answer + 1) % 4
    A, B, C = "guest-aaaa1111", "guest-bbbb2222", "guest-cccc3333"
    ans = lambda g, ch, r=rnd: c.post(f"/api/p/{code}/answer", json={"guest": g, "name": g[6:10].title(), "round": r, "choice": ch})
    assert ans(A, right).status_code == 200
    assert ans(B, wrong).status_code == 200
    assert ans(A, wrong).status_code == 409, "one answer per round"
    assert ans(C, 9).status_code == 400 and ans(C, right, rnd + 5).status_code == 409
    mine = c.get(f"/api/p/{code}/coins", params={"guest": A}).json()
    assert mine["coins"] == 0 and mine["round"] == {"round": rnd, "choice": right}, mine  # nothing given away yet
    assert c.get(f"/api/p/{code}/state").json()["trivia"]["answered"] == 2
    party.sync(code, 52, "playing")
    tv = c.get(f"/api/p/{code}/state").json()["trivia"]
    assert tv["phase"] == "reveal" and tv["answer"] == right and tv["fact"] and tv["right"] == 1 and tv["answered"] == 2, tv
    assert tv["board"][0] == {"name": "Aaaa", "coins": trivia.COINS + trivia.FAST_BONUS}, tv["board"]
    assert ans(C, right).status_code == 409, "too late"
    me_a = c.get(f"/api/p/{code}/coins", params={"guest": A}).json()
    me_b = c.get(f"/api/p/{code}/coins", params={"guest": B}).json()
    assert me_a["coins"] == 15 and me_a["round"]["correct"] is True and me_b["coins"] == 0 and me_b["round"]["correct"] is False
    party.sync(code, 70, "playing")
    assert c.get(f"/api/p/{code}/state").json()["trivia"] is None, "after the reveal"
    c.post(f"/api/p/{code}/trivia", json={"on": False}, headers=H)
    party.sync(code, 52, "playing")
    assert c.get(f"/api/p/{code}/state").json()["trivia"] is None, "off = no round"
    print("3. the question while open (no answer on any screen), then the answer, the fact, 1 of 2 right, the leaderboard")
    print("4. right and quick 15 coins, wrong 0; twice, late and a bad choice refused; coins say nothing before the reveal")

    # 5. the mix: about 1 in 4 from our data
    with repository.session() as s:
        song_id = s.scalar(select(db.SongTrivia.song_id).limit(1))
        kinds = dict(s.execute(select(db.SongTrivia.id, db.SongTrivia.kind).where(db.SongTrivia.song_id == song_id)).all())
    rng = random.Random(3)
    picks = [kinds[trivia.pick(song_id, [], rng)] for _ in range(400)]
    share = picks.count("data") / len(picks)
    assert 0.18 <= share <= 0.32, share
    print(f"5. over 400 picks {share:.0%} came from our data, the rest Gemini's facts")
    print("TRIVIA PASS")


if __name__ == "__main__":
    main(*sys.argv[1:])
