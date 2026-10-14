"""Runnable check for guest feedback, transition clips, the Gemini transition review and the analytics export:

    python -m tests.test_feedback LIBRARY

Temp SQLite database; a short simulated session (no speakers) writing clips under LIBRARY/_mixes/clips/test-*/ (removed
at the end); votes through the FastAPI TestClient; the review with a fake Gemini; the export to a temp folder.
"""
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path


def main(library):
    library = Path(library).resolve()
    tmp = Path(tempfile.mkdtemp())
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp / 'algo_rhythm.db'}"  # before config.settings() is first read
    import soundfile as sf
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from algo_rhythm import analytics, db, dj, genai, importer, live, repository, review, server

    clips_root = library / "_mixes" / "clips" / f"test-{uuid.uuid4().hex[:8]}"
    try:
        importer.main([str(library), "--database-url", os.environ["DATABASE_URL"]])
        # 1. a short session that writes a clip of every join
        sess = live.build(library, "high", history=tmp / "history.json")
        sink = live.FileSink(dj.OUT_SR, tmp / "out.wav")
        sess.playhead = sink.playhead
        sess.clip_dir = clips_root / "files"
        player = server.Player(sess, sink, lambda: live.Engine(sess, library, sink, None, [], 330))
        server.record_to_database(player, "high")
        player.request(sess.songs[3]["file"])
        player.engine.start()
        player.engine.join()
        for t in player.engine.clip_threads:
            t.join()
        sink.close()
        assert player.engine.error is None, player.engine.error
        with repository.session() as s:
            plays = s.scalars(select(db.Play).order_by(db.Play.position)).all()
        joins = [p for p in plays if p.position > 1]
        assert len(joins) >= 3, f"need 3 joins for the review check, got {len(joins)}"
        for p in joins:
            assert p.clip and p.clip.startswith(f"_mixes/clips/{clips_root.name}/"), p.clip
            info = sf.info(library / p.clip)
            assert info.format == "FLAC" and info.duration > 2 * live.CLIP_EDGE_S, (p.clip, info.duration)
        assert plays[0].clip is None, "the opening song has no join"
        assert "terms" in joins[0].why and joins[0].why["level"] == "high", joins[0].why

        # 2. votes: attached to the song on air or the latest join, upserted, rate limited
        tl = [t for t in sess.timeline if t["start_s"] != float("inf")]
        j = tl[-1]
        ph = {"t": j["start_s"] + 0.5}  # just after the last join started: the outgoing song is still the loudest
        sess.playhead = lambda: ph["t"]
        song_pos, mix_pos = tl[-2]["entry"]["position"], j["entry"]["position"]
        c = TestClient(server.create_app(player, {"testserver"}))
        A, B, C = "guest-aaaa", "guest-bbbb", "guest-cccc"
        fb = c.get("/state").json()["feedback"]
        names = lambda pos: [sess.songs[t["entry"]["song"]]["name"] for t in tl if t["entry"]["position"] in (pos - 1, pos)]
        assert fb == {"song": {"position": song_pos, "up": 0, "down": 0, "name": names(song_pos)[-1]},
                      "mix": {"position": mix_pos, "up": 0, "down": 0, "open": True, "from": names(mix_pos)[0], "to": names(mix_pos)[1],
                              "from_file": fb["mix"]["from_file"], "to_file": fb["mix"]["to_file"], "kind": fb["mix"]["kind"]}}, fb
        assert fb["mix"]["kind"] in ("blend", "cut", "echo-out"), fb["mix"]
        r = c.post("/feedback", json={"guest": A, "target": "song", "value": "up"})
        assert r.status_code == 200 and r.json()["feedback"]["song"]["up"] == 1, r.text
        r = c.post("/feedback", json={"guest": A, "target": "song", "value": "down"})  # changes the vote
        assert {k: r.json()["feedback"]["song"][k] for k in ("position", "up", "down")} == {"position": song_pos, "up": 0, "down": 1}, r.json()["feedback"]
        assert c.post("/feedback", json={"guest": B, "target": "song", "value": "up"}).status_code == 200
        r = c.post("/feedback", json={"guest": A, "target": "transition", "value": "up"})
        assert r.status_code == 400 and "host" in r.json()["error"], "guests rate songs only"
        r = c.post("/feedback", json={"guest": A, "target": "transition", "value": "up", "host": True})
        assert r.json()["feedback"]["mix"]["up"] == 1, r.json()["feedback"]
        for bad in ({"guest": "x", "target": "song", "value": "up"}, {"guest": A, "target": "dj", "value": "up"},
                    {"guest": A, "target": "song", "value": "meh"}, {"guest": 7, "target": "song", "value": "up"}):
            assert c.post("/feedback", json=bad).status_code == 400, bad
        ph["t"] = j["start_s"] + j["overlap_s"] + live.MIX_FEEDBACK_S + 1  # the mix is over: song votes only
        fb = c.get("/state").json()["feedback"]
        assert fb["mix"]["open"] is False and fb["song"]["position"] == mix_pos, fb
        r = c.post("/feedback", json={"guest": B, "target": "transition", "value": "down", "host": True})
        assert r.status_code == 400 and "no mix" in r.json()["error"], r.text
        for i in range(server.VOTES_PER_MIN):
            assert c.post("/feedback", json={"guest": C, "target": "song", "value": "up"}).status_code == 200, i
        assert c.post("/feedback", json={"guest": C, "target": "song", "value": "up"}).status_code == 429
        with repository.session() as s:
            rows = {(f.guest, f.target, f.position): f.value for f in s.scalars(select(db.Feedback))}
        assert rows == {(A, "song", song_pos): "down", (B, "song", song_pos): "up", (A, "transition", mix_pos): "up",
                        (C, "song", mix_pos): "up"}, rows

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
        assert "transition" in sent[0][0]["text"]
        with repository.session() as s:
            revs = s.scalars(select(db.TransitionReview)).all()
            n_calls = s.scalar(select(func.count()).select_from(db.AICall).where(db.AICall.task == "review"))
        assert len(revs) == 1 and revs[0].play_id == joins[1].id and revs[0].score == 4, revs
        assert revs[0].prompt_version == "review-v1" and revs[0].issues == ["volume_jump"]
        assert n_calls == 3, n_calls

        # 4. export: one newline-delimited JSON file per table
        out = tmp / "export"
        counts = analytics.export(out)
        load = lambda name: [json.loads(x) for x in (out / f"{name}.json").read_text().splitlines()]
        assert counts["plays"] == len(plays) and counts["feedback"] == 4 and counts["transition_reviews"] == 1, counts
        assert counts["song_requests"] == 1 and counts["ai_calls"] == 3 and counts["songs"] == len(sess.songs), counts
        ps = load("plays")
        assert ps[0]["transition_type"] == "start" and ps[1]["transition_type"] in ("blend", "echo-out"), ps[1]
        assert ps[1]["song_name"] and ps[1]["clip"] and ps[1]["level"] == "high" and "term_join" in ps[1], ps[1]
        songs = load("songs")
        assert any(s.get("gemini_energy") for s in songs), songs[0]
        print(f"FEEDBACK PASS ({len(joins)} clips, 4 votes, 1 review, {sum(counts.values())} rows exported)")
    finally:
        shutil.rmtree(clips_root, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "library_100")
