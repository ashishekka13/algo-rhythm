"""Runnable check for the web server (no speakers): python -m tests.test_server LIBRARY"""
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from algo_rhythm import dj, live, server


def main(library):
    library = Path(library).resolve()
    tmp = Path(tempfile.mkdtemp())
    sess = live.build(library, "medium", history=tmp / "history.json")  # never the real play history
    sink = live.FileSink(dj.OUT_SR, tmp / "out.wav")
    sink.paused = False
    sink.reset = lambda: None
    sess.playhead = sink.playhead
    player = server.Player(sess, sink, lambda: live.Engine(sess, library, sink))
    c = TestClient(server.create_app(player, {"testserver"}))
    J = {"Content-Type": "application/json"}

    assert c.get("/").status_code == 200 and b"<html" in c.get("/").content.lower()
    for view in ("/guest", "/presenter"):
        r = c.get(view)
        assert r.status_code == 200 and b"<html" in r.content.lower() and r.headers["cache-control"] == "no-store", view
    r = c.get("/static/visuals.js")
    assert r.status_code == 200 and b"function garden" in r.content and r.headers["cache-control"] == "no-store", r.status_code
    s = c.get("/state").json()
    assert s["api"] == live.API_VERSION and s["status"] == "idle" and s["level"] == "medium", s
    assert c.get("/healthz").json()["songs"] == len(sess.songs)
    assert len(c.get("/songs").json()) == len(sess.songs)
    assert c.get("/state", headers={"Host": "evil.example"}).status_code == 403, "DNS-rebinding guard"
    assert c.post("/vibe", content='{"level": "max"}', headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post("/vibe", content="x" * 5000, headers=J).status_code == 400, "body size limit"
    assert c.post("/vibe", json={"level": "max"}).json()["level"] == "max"
    assert c.post("/vibe", json={"level": "loud"}).status_code == 400
    v = c.post("/vibe", json={"vibe": "romantic"}).json()
    assert (v["vibe"], v["level"], v["mood"]) == ("romantic", "medium", "romantic"), v
    assert c.post("/vibe", json={"vibe": "loud"}).status_code == 400
    assert c.post("/mood", json={"mood": "party"}).json()["mood"] == "party"
    assert c.post("/visuals", json={"world": "holi", "kaleido": True}).json()["visuals"] == {"world": "holi", "kaleido": True}
    assert c.get("/state").json()["visuals"]["world"] == "holi" and c.post("/visuals", json={"world": "x", "kaleido": 1}).status_code == 400
    assert c.post("/mood", json={"mood": "angry"}).status_code == 400 and c.post("/mood", json={"mood": "auto"}).json()["mood"] == "auto"
    assert c.post("/request", json={"file": "not/a/song.mp3"}).status_code == 400
    r = c.post("/request", json={"file": sess.songs[0]["file"]})
    assert r.status_code == 200, r.text
    assert c.post("/transport", json={"action": "dance"}).status_code == 400
    assert c.post("/transport", json={"action": "skip"}).status_code == 200, "skip before Play is a no-op"
    # closing the admin tab stops the server after BYE_GRACE_S, even mid-song; a reload of the admin page cancels it
    import time
    assert c.post("/transport", json={"action": "bye"}).status_code == 200
    now = time.monotonic()
    assert not server.should_stop(player, now), "within the grace a reload can still come back"
    player.engine.is_alive = lambda: True
    assert server.should_stop(player, now + live.BYE_GRACE_S + 1), "the admin tab closed: stop, even while playing"
    del player.engine.is_alive
    c.get("/guest")
    assert server.should_stop(player, now + live.BYE_GRACE_S + 1), "a guest page does not keep it up"
    c.get("/")
    assert not server.should_stop(player, now + live.BYE_GRACE_S + 1), "the admin page reloaded: keep running"
    # idle: stops only after IDLE_STOP_S with no page; music or requests never stop it
    now = time.monotonic()
    assert not server.should_stop(player, now), "a page just asked for /transport"
    player.page["seen"] = now - server.IDLE_STOP_S - 1
    assert sess.requests and not server.should_stop(player, now), "a queued request keeps the server up"
    held, sess.requests = sess.requests, []
    assert server.should_stop(player, now), "idle, nothing queued, no page for 30 min: stop"
    player.engine.is_alive = lambda: True  # playing or paused
    assert not server.should_stop(player, now), "never while the engine runs"
    del player.engine.is_alive
    sess.requests = held
    c.get("/state")
    assert not server.should_stop(player, time.monotonic() + server.IDLE_STOP_S - 60), "a page visit resets the clock"
    # Talk to the DJ: the route applies what the interpreter decided (fake answer, no Gemini call)
    from algo_rhythm import talk
    real = talk.interpret
    talk.interpret = lambda text, state, songs, **k: {"reply": "Peak time!", "source": "gemini", "actions": [
        {"type": "set_level", "level": "high"}, {"type": "request", "file": sess.songs[1]["file"]}]}
    try:
        r = c.post("/talk", json={"text": "build it up"})
        assert r.status_code == 200 and r.json()["level"] == "high" and r.json()["talk"]["reply"] == "Peak time!", r.text
        assert len(r.json()["talk"]["done"]) == 2, r.json()["talk"]
        assert c.post("/talk", json={"text": "  "}).status_code == 400
        talk.interpret = lambda text, state, songs, **k: {"reply": "Okay!", "source": "gemini", "actions": [
            {"type": "pause"}, {"type": "set_level", "level": "low"}, {"type": "set_mood", "mood": "romantic"}]}
        r = c.post("/talk", json={"text": "romantic and pause", "guest": True}).json()
        assert r["talk"]["done"] == ["mood romantic"] and r["mood"] == "romantic" and r["level"] == "high", r["talk"]
        assert "Only the host" in r["talk"]["reply"], r["talk"]
    finally:
        talk.interpret = real
    # AI MC switch: no "mc" key without an MC; with one, the admin toggles it and /state reports it
    assert "mc" not in c.get("/state").json() and c.post("/transport", json={"action": "mc_on"}).status_code == 400
    from algo_rhythm import mc
    sess.mc = mc.MC()
    assert c.get("/state").json()["mc"]["enabled"] is True
    assert c.post("/transport", json={"action": "mc_off"}).json()["mc"]["enabled"] is False
    assert c.post("/transport", json={"action": "mc_on"}).json()["mc"]["next_eligible_s"] == mc.EVERY_S
    # /search: names containing the text first, then by meaning (fake nearest, no Gemini call)
    from algo_rhythm import search
    real_nearest = search.nearest
    search.nearest = lambda q, songs, k=20, level=None, embed=None: [5, 1]
    try:
        kc = next(i for i, s in enumerate(sess.songs) if "Kala Chashma" in s["name"])
        r = c.get("/search", params={"q": "kala  chashma"}).json()
        assert [x["file"] for x in r] == [sess.songs[kc]["file"]] and set(r[0]) == {"file", "name", "mood", "named"}, r
        r = c.get("/search", params={"q": "zzqx song for rain"}).json()  # no name matches: songs similar in meaning
        assert [x["file"] for x in r] == [sess.songs[i]["file"] for i in (5, 1)] and not any(x["named"] for x in r), r
        assert c.get("/search", params={"q": ""}).json() == []
    finally:
        search.nearest = real_nearest
    # song art: only images listed in the art index are served; anything else is a 404
    assert c.get("/art/../../etc/passwd").status_code == 404 and c.get("/art/0123456789abcdef.jpg").status_code == 404
    if sess.art:
        name = next(iter(sess.art.values()))
        r = c.get(f"/art/{name}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", r.status_code
        song = next(x for x in sess.songs if x["sha256"][:16] + ".jpg" == name)
        assert sess.art_url(song) == f"/art/{name}"
    # requester names: cleaned, shown as "by" on the requested song while it is coming up
    sess.requests, sess.timeline, sess.queue = [], [], []
    req = sess.songs[7]
    assert c.post("/request", json={"file": req["file"], "name": "  Priya\u0007   from  accounts and more text"}).status_code == 200
    assert player.requesters[req["name"]] == "Priya from accounts and", player.requesters
    sess.queue = [{**sess._entry_for_test(7), "request": True}]
    s = c.get("/state").json()
    assert s["next"][0]["by"] == "Priya from accounts and" and s["guest_url"] is None, s["next"][0]

    # reactions: known emoji only, shown for a few seconds, rate limited per guest
    r = c.post("/react", json={"guest": "guest-react1", "emoji": "🔥"})
    assert r.status_code == 200 and [x["emoji"] for x in r.json()["reactions"]] == ["🔥"], r.text
    assert c.post("/react", json={"guest": "guest-react1", "emoji": "<b>"}).status_code == 400
    assert c.post("/react", json={"guest": "x", "emoji": "🔥"}).status_code == 400
    for _ in range(server.REACTS_PER_MIN - 1):
        assert c.post("/react", json={"guest": "guest-react1", "emoji": "🎉"}).status_code == 200
    assert c.post("/react", json={"guest": "guest-react1", "emoji": "🎉"}).status_code == 429
    player.reactions[0]["t"] -= server.REACTION_SHOW_S + 1
    assert len(c.get("/state").json()["reactions"]) == server.REACTS_PER_MIN - 1, "old reactions drop out"

    # file mode feedback: one JSON line per vote with the join's songs and type; the host rates joins, guests songs only
    import json
    fb_file = tmp / "_mixes" / "feedback.jsonl"
    server.record_to_file(player, fb_file)
    a, b = sess._entry_for_test(2), sess._entry_for_test(3)
    a["position"], b["position"] = 1, 2
    sess.timeline = [{"entry": a, "start_s": 0.0}, {"entry": b, "start_s": 10.0, "overlap_s": 6.0}]
    sess.playhead = lambda: 12.0  # mid-join is 13 s: the first song is still the loudest
    fb = c.get("/state").json()["feedback"]
    assert fb["mix"]["from"] == sess.songs[2]["name"] and fb["mix"]["to"] == sess.songs[3]["name"] and fb["mix"]["open"], fb
    assert c.post("/feedback", json={"guest": "guest-1234", "target": "transition", "value": "up"}).status_code == 400
    assert c.post("/feedback", json={"guest": "host-12345", "target": "transition", "value": "up", "host": True}).status_code == 200
    assert c.post("/feedback", json={"guest": "guest-1234", "target": "song", "value": "down"}).status_code == 200
    p2 = server.Player(sess, sink, lambda: live.Engine(sess, library, sink))  # a restart appends to the same file
    server.record_to_file(p2, fb_file)
    p2.vote("guest-5678", "song", "up")
    lines = [json.loads(x) for x in fb_file.read_text().splitlines()]
    assert len(lines) == 3 and lines[2]["value"] == "up", lines
    j, g = lines[:2]
    assert (j["target"], j["value"], j["by"], j["position"], j["kind"]) == ("transition", "up", "host", 2, "blend"), j
    assert (j["from_file"], j["to_file"]) == (sess.songs[2]["file"], sess.songs[3]["file"]) and j["time"], j
    assert (g["target"], g["by"], g["position"], g["kind"], g["from"]) == ("song", "guest", 1, "start", None), g
    print("SERVER PASS")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "library_100")
