"""Runnable check for guest feedback, transition clips, the Gemini transition review and the analytics export:

    python -m tests.test_feedback [LIBRARY]      (default test_library, see tests/test_recipe.py)

Temp SQLite database; a party played through the web API (no browser: the test moves the playhead itself) writing
a clip of every join under LIBRARY/_mixes/clips/<party>/ (removed at the end); votes; the review with a fake Gemini;
the export to a temp folder.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def main(library="test_library"):
    library = Path(library).resolve()
    tmp = Path(tempfile.mkdtemp())
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp / 'algo_rhythm.db'}"  # before config.settings() is first read
    os.environ["LIBRARY_DIR"] = str(library)
    os.environ.pop("STORAGE_URL", None)
    import soundfile as sf
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from algo_rhythm import analytics, db, genai, importer, library as lib_mod, live, party, repository, review, server

    importer.main([str(library), "--database-url", os.environ["DATABASE_URL"]])
    lib_mod.reset()
    c = TestClient(server.create_app())
    code, key = party.create("high")
    H = {"X-Host-Key": key}
    party_id = party.read(code)[0]
    clips_root = library / "_mixes" / "clips" / str(party_id)
    try:
        # 1. a party with three joins, each with its clip
        s = c.post(f"/api/p/{code}/transport", json={"action": "play"}, headers=H).json()
        for _ in range(3):
            s = c.post(f"/api/p/{code}/advance", json={}, headers=H).json()
            assert s["advance"]["status"] == "ready", s["advance"]
            last = s["engine"]["items"][-1]
            party.sync(code, last["start_s"] + 1, "playing")
        with repository.session() as db_s:
            plays = db_s.scalars(select(db.Play).where(db.Play.party_id == party_id).order_by(db.Play.position)).all()
        joins = [p for p in plays if p.position > 1]
        assert len(joins) == 3, f"need 3 joins, got {len(joins)}"
        for p in joins:
            assert p.clip and p.clip.startswith(f"_mixes/clips/{party_id}/"), p.clip
            info = sf.info(library / p.clip)
            assert info.format == "FLAC" and info.duration > 2 * live.CLIP_EDGE_S, (p.clip, info.duration)
        assert plays[0].clip is None, "the opening song has no join"
        assert "terms" in joins[0].why and joins[0].why["level"] == "high", joins[0].why

        # 2. votes: attached to the song on air or the latest join, upserted, rate limited
        st = party.read(code)[1]
        tl = st["timeline"]
        j = tl[-1]
        party.sync(code, j["start_s"] + 0.5, "paused")  # just after the last join started: the outgoing song is still the loudest
        song_pos, mix_pos = tl[-2]["entry"]["position"], j["entry"]["position"]
        A, B, C = "guest-aaaa", "guest-bbbb", "guest-cccc"
        fb = c.get(f"/api/p/{code}/state").json()["feedback"]
        assert fb["song"]["position"] == song_pos and fb["mix"]["position"] == mix_pos and fb["mix"]["open"], fb
        assert fb["mix"]["kind"] in ("blend", "echo-out"), fb["mix"]
        r = c.post(f"/api/p/{code}/feedback", json={"guest": A, "target": "song", "value": "up"})
        assert r.status_code == 200 and r.json()["feedback"]["song"]["up"] == 1, r.text
        r = c.post(f"/api/p/{code}/feedback", json={"guest": A, "target": "song", "value": "down"})  # changes the vote
        assert {k: r.json()["feedback"]["song"][k] for k in ("position", "up", "down")} == {"position": song_pos, "up": 0, "down": 1}
        assert c.post(f"/api/p/{code}/feedback", json={"guest": B, "target": "song", "value": "up"}).status_code == 200
        r = c.post(f"/api/p/{code}/feedback", json={"guest": A, "target": "transition", "value": "up"})
        assert r.status_code == 400 and "host" in r.json()["error"], "guests rate songs only"
        r = c.post(f"/api/p/{code}/feedback", json={"guest": A, "target": "transition", "value": "up"}, headers=H)
        assert r.json()["feedback"]["mix"]["up"] == 1, r.json()["feedback"]
        for bad in ({"guest": "x", "target": "song", "value": "up"}, {"guest": A, "target": "dj", "value": "up"},
                    {"guest": A, "target": "song", "value": "meh"}, {"guest": 7, "target": "song", "value": "up"}):
            assert c.post(f"/api/p/{code}/feedback", json=bad).status_code == 400, bad
        party.sync(code, j["start_s"] + j["overlap_s"] + live.MIX_FEEDBACK_S + 1, "paused")  # the mix is over: song votes only
        fb = c.get(f"/api/p/{code}/state").json()["feedback"]
        assert fb["mix"]["open"] is False and fb["song"]["position"] == mix_pos, fb
        r = c.post(f"/api/p/{code}/feedback", json={"guest": B, "target": "transition", "value": "down"}, headers=H)
        assert r.status_code == 400 and "no mix" in r.json()["error"], r.text
        for i in range(party.VOTES_PER_MIN):
            assert c.post(f"/api/p/{code}/feedback", json={"guest": C, "target": "song", "value": "up"}).status_code == 200, i
        assert c.post(f"/api/p/{code}/feedback", json={"guest": C, "target": "song", "value": "up"}).status_code == 429
        with repository.session() as db_s:
            rows = {(f.guest, f.target, f.position): f.value for f in db_s.scalars(select(db.Feedback))}
        assert rows == {(A, "song", song_pos): "down", (B, "song", song_pos): "up", (A, "transition", mix_pos): "up",
                        (C, "song", mix_pos): "up"}, rows
        c.post(f"/api/p/{code}/request", json={"file": c.get("/api/songs").json()[5]["file"]})

        # 3. review with a fake Gemini: a schema violation is skipped, a good answer stored, quota stops the run
        answers = [{"score": "five", "smooth": True, "issues": ["none"], "summary": "x"},
                   {"score": 4, "smooth": True, "issues": ["volume_jump"], "summary": "Clean beat match, a small bump."}]
        sent = []

        def fake_post(model, parts, schema, timeout):
            sent.append(parts)
            if not answers:
                raise genai.QuotaError("daily Gemini cap reached")
            return answers.pop(0)
        real = genai._post
        genai._post = fake_post
        try:
            assert review.run(limit=10, library=library) == 1
        finally:
            genai._post = real
        assert len(sent) == 3, len(sent)
        assert sent[0][1]["inlineData"]["mimeType"] == "audio/flac" and len(sent[0][1]["inlineData"]["data"]) > 1000
        with repository.session() as db_s:
            revs = db_s.scalars(select(db.TransitionReview)).all()
            n_calls = db_s.scalar(select(func.count()).select_from(db.AICall).where(db.AICall.task == "review"))
        assert len(revs) == 1 and revs[0].play_id == joins[1].id and revs[0].score == 4, revs
        assert revs[0].issues == ["volume_jump"] and n_calls == 3, (revs[0].issues, n_calls)

        # 4. export: one newline-delimited JSON file per table
        out = tmp / "export"
        counts = analytics.export(out)
        load = lambda name: [json.loads(x) for x in (out / f"{name}.json").read_text().splitlines()]
        assert counts["plays"] == len(plays) and counts["feedback"] == 4 and counts["transition_reviews"] == 1, counts
        assert counts["song_requests"] == 1 and counts["ai_calls"] >= 3, counts
        ps = load("plays")
        assert ps[0]["transition_type"] == "start" and ps[1]["transition_type"] in ("blend", "echo-out"), ps[1]
        assert ps[1]["song_name"] and ps[1]["clip"] and ps[1]["level"] == "high" and "term_join" in ps[1], ps[1]
        assert any(x.get("gemini_energy") for x in load("songs"))
        print(f"FEEDBACK PASS ({len(joins)} clips, 4 votes, 1 review, {sum(counts.values())} rows exported)")
    finally:
        shutil.rmtree(clips_root, ignore_errors=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
