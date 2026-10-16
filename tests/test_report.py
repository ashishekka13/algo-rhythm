"""Runnable check for End session and the party report (algo_rhythm.report), no browser:

    python -m tests.test_report [LIBRARY]      (default test_library)

Ends a played party through the API and checks: the night is closed but nothing is lost, ending twice is harmless,
only the host (by key, or signed in as the party's owner) gets the report, a thin party still gets the whole shape,
Gemini failing leaves the figures, and reloading the report makes no second Gemini call.
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
    os.environ.pop("STORAGE_URL", None)
    os.environ["MC_TTS"] = "none"
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from algo_rhythm import accounts, db, genai, importer, library as lib_mod, party, report, repository, server

    importer.main([str(library), "--database-url", os.environ["DATABASE_URL"]])
    lib_mod.reset()
    accounts.create("host", "host@example.com", "host-password", active=True)
    accounts.create("other", "other@example.com", "other-password", active=True)
    c = TestClient(server.create_app())
    assert c.post("/api/auth/login", json={"username": "host", "password": "host-password"}).status_code == 200
    r = c.post("/api/parties", json={"level": "medium"})
    code, key = r.json()["code"], r.json()["host_key"]
    H = {"X-Host-Key": key}

    # the page shell is open to anyone; the figures are the host's
    r = c.get(f"/p/{code}/report")
    assert r.status_code == 200 and b"svg.chart" in r.content and r.headers["cache-control"] == "no-store"

    # a night: three songs, votes, reactions, a request
    c.post(f"/api/p/{code}/transport", json={"action": "play"}, headers=H)
    for at in (60, 140):
        st = party.read(code)[1]
        party.sync(code, st["timeline"][-1]["start_s"] + 1 if len(st["timeline"]) > 1 else at, "playing")
        c.post(f"/api/p/{code}/advance", json={}, headers=H)
    pid = party.read(code)[0]
    with repository.session() as s:
        for g, v in (("guest-a-0001", "up"), ("guest-b-0002", "up"), ("guest-c-0003", "down")):
            s.add(db.Feedback(party_id=pid, position=1, guest=g, target="song", value=v))
        for g in ("guest-a-0001", "guest-b-0002", "guest-c-0003"):
            s.add(db.Feedback(party_id=pid, position=2, guest=g, target="transition", value="down"))
            s.add(db.Reaction(party_id=pid, guest=g, emoji="🔥"))
        s.commit()
    repository.record_request(pid, "a song we do not have")
    with repository.session() as s:
        n_plays = s.scalar(select(func.count()).select_from(db.Play).where(db.Play.party_id == pid))
    assert n_plays >= 2, n_plays

    # only the host: no key (and signed in as someone else) is 403; an unknown party is 404, not 500
    other = TestClient(server.create_app())
    for path, method in ((f"/api/p/{code}/report", "get"), (f"/api/p/{code}/end", "post")):
        kw = {"json": {}} if method == "post" else {}
        assert getattr(other, method)(path, **kw).status_code == 403, path
    assert other.post("/api/auth/login", json={"username": "other", "password": "other-password"}).status_code == 200
    assert other.get(f"/api/p/{code}/report").status_code == 403
    assert other.post(f"/api/p/{code}/end", json={}).status_code == 403
    for path in ("/api/p/ZZZZZZ/report", "/api/p/no-such!/report"):
        assert c.get(path, headers=H).status_code == 404, path
    assert c.post("/api/p/ZZZZZZ/end", json={}, headers=H).status_code == 404

    # Gemini unavailable: the figures alone, no error
    calls = []
    real_post = genai._post

    def failing(*a, **k):
        calls.append("fail")
        raise genai.GenAIError("no Gemini in tests")
    genai._post = failing
    try:
        r = c.post(f"/api/p/{code}/end", json={}, headers=H)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "narrative" not in d and d["totals"]["songs"] == n_plays, d["totals"]
    finally:
        genai._post = real_post

    # ended: closed, nothing lost, nothing restarts it
    row_id, st = party.read(code)
    assert st["status"] == "ended" and st["ended_at"], st["status"]
    first_end = st["ended_at"]
    assert party.advance(code)["status"] == "nothing"
    party.sync(code, 999.0, "playing")
    party.start(code)
    c.post(f"/api/p/{code}/transport", json={"action": "resume"}, headers=H)
    assert party.read(code)[1]["status"] == "ended"
    assert c.get(f"/api/p/{code}/state").json()["host_offline"] is False  # an ended party is not "host offline"
    with repository.session() as s:
        assert s.scalar(select(func.count()).select_from(db.Play).where(db.Play.party_id == pid)) == n_plays
        assert s.scalar(select(func.count()).select_from(db.Feedback).where(db.Feedback.party_id == pid)) == 6
        assert s.scalar(select(func.count()).select_from(db.Reaction).where(db.Reaction.party_id == pid)) == 3
    # ending twice is harmless and keeps the first time
    assert c.post(f"/api/p/{code}/end", json={}, headers=H).status_code == 200
    assert party.read(code)[1]["ended_at"] == first_end

    # the figures
    d = c.get(f"/api/p/{code}/report", headers=H).json()
    for k in ("party", "totals", "songs", "energy", "top_songs", "requests", "disagreements", "calibration", "trivia",
              "reaction_timeline", "profile"):
        assert k in d, k
    last = d["songs"][-1]
    assert d["party"]["minutes"] * 60 >= last["at_s"] + last["seconds"] - 1, (d["party"]["minutes"], last)
    assert d["songs"][0]["song_up"] == 2 and d["songs"][0]["song_down"] == 1
    assert d["top_songs"][0]["position"] == 1                       # 3 votes: a verdict
    assert d["songs"][1]["mix_down"] == 3 and d["totals"]["reactions"] == 3 and d["totals"]["guests"] == 3
    assert d["calibration"]["note"] and d["calibration"]["judged"] == 0   # no Gemini reviews: too few to say
    assert "a song we do not have" in d["profile"]["unplayed_requests"]
    assert len(d["reaction_timeline"]) == report.TIMELINE_BINS and sum(d["reaction_timeline"]) == 3
    assert all("art" in s for s in d["songs"])                      # None is ordinary: the page draws a cover
    if d["songs"][1]["match"] is not None and d["songs"][1]["match"] >= report.CONFIDENT:
        assert d["disagreements"] and d["disagreements"][0]["position"] == 2

    # the host's account on another device: no key, signed in as the owner
    phone = TestClient(server.create_app())
    assert phone.get(f"/api/p/{code}/report").status_code == 403
    assert phone.post("/api/auth/login", json={"username": "host", "password": "host-password"}).status_code == 200
    assert phone.get(f"/api/p/{code}/report").status_code == 200

    # with Gemini: a write-up; reloading the report makes no second call (the party's figures no longer change)
    answer = {"headline": "A long slow climb to the top tonight", "story": "You opened gently.\n\nThe room joined in.",
              "highlights": [{"label": "Votes", "text": "Three guests voted."}] * 6, "next_time": "Start a little higher."}

    def fake(*a, **k):
        calls.append("ok")
        return dict(answer)
    genai._post = fake
    try:
        calls.clear()
        d1 = c.get(f"/api/p/{code}/report", headers=H).json()
        d2 = c.get(f"/api/p/{code}/report", headers=H).json()
    finally:
        genai._post = real_post
    assert calls == ["ok"], calls
    assert d1["narrative"]["headline"] == "" and len(d1["narrative"]["highlights"]) == 4
    assert d2["narrative"] == d1["narrative"]

    # a thin party: one song, no votes, no reactions: the same shape, zeroes
    code2, key2 = party.create("low")
    party.start(code2)
    party.end(code2)
    t = report.build(party.read(code2)[0])
    assert t["totals"]["songs"] == 1 and t["totals"]["votes"] == 0 and t["top_songs"] == [] and t["calibration"]["note"]
    assert t["songs"][0]["kind"] == "start" and t["party"]["minutes"] > 0
    empty_code, _ = party.create("low")
    party.end(empty_code)
    e = report.build(party.read(empty_code)[0], narrate=True)
    assert e["totals"]["songs"] == 0 and e["songs"] == [] and "narrative" not in e
    print(f"REPORT PASS (party {code}: {n_plays} songs, ended once)")


if __name__ == "__main__":
    main(*sys.argv[1:])
