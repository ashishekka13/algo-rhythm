"""Runnable check for the web server (stateless API + WebSockets), no browser:

    python -m tests.test_web [LIBRARY]      (default test_library, see tests/test_recipe.py)

A temp SQLite database with LIBRARY imported, the library folder as storage. Plays a party through the API the way the
host's page does (play, advance, sync, a request moved up, votes, reactions, talk, restart) and checks what every
screen gets, over HTTP and over the WebSocket.
"""
import os
import sys
import tempfile
from pathlib import Path


def main(library="test_library"):
    library = Path(library).resolve()
    tmp = Path(tempfile.mkdtemp())
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp / 'algo_rhythm.db'}"  # before config.settings() is first read
    os.environ["LIBRARY_DIR"] = str(library)
    os.environ.pop("STORAGE_URL", None)
    os.environ["MC_TTS"] = "none"  # no voice in tests: the MC stays silent
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from algo_rhythm import db, importer, library as lib_mod, live, party, recipe, repository, server, talk

    importer.main([str(library), "--database-url", os.environ["DATABASE_URL"]])
    lib_mod.reset()
    c = TestClient(server.create_app())
    J = {"Content-Type": "application/json"}

    # pages and the library
    for path in ("/", "/p/ABCDEF", "/p/ABCDEF/host", "/p/ABCDEF/screen"):
        r = c.get(path)
        assert r.status_code == 200 and b"<html" in r.content.lower() and r.headers["cache-control"] == "no-store", path
    assert b"function garden" in c.get("/static/visuals.js").content
    assert c.get("/static/nope.js").status_code == 404
    songs = c.get("/api/songs").json()
    assert len(songs) == c.get("/health").json()["songs"] >= 8
    assert c.get("/api/search", params={"q": "Synth 03"}).json()[0]["name"].startswith("Synth 03")

    # a party: the host gets a key, guests only the code
    r = c.post("/api/parties", json={"level": "medium"})
    code, key = r.json()["code"], r.json()["host_key"]
    H = {"X-Host-Key": key}
    s = c.get(f"/api/p/{code}/state").json()
    assert s["api"] == live.API_VERSION and s["status"] == "idle" and "engine" not in s, s.keys()
    assert "engine" in c.get(f"/api/p/{code}/state", headers=H).json()
    assert c.get("/api/p/ZZZZZZ/state").status_code == 404
    assert c.post(f"/api/p/{code}/vibe", json={"vibe": "party"}).status_code == 403, "guests cannot set the vibe"
    assert c.post(f"/api/p/{code}/vibe", json={"vibe": "party"}, headers={"X-Host-Key": "wrong"}).status_code == 403
    assert c.post(f"/api/p/{code}/vibe", content='{"vibe": "party"}', headers={**H, "Content-Type": "text/plain"}).status_code == 415
    assert c.post(f"/api/p/{code}/vibe", content="x" * 5000, headers={**H, **J}).status_code == 400
    v = c.post(f"/api/p/{code}/vibe", json={"vibe": "groove"}, headers=H).json()
    assert (v["vibe"], v["level"], v["mood"]) == ("groove", "medium", "party"), v
    assert c.post(f"/api/p/{code}/vibe", json={"vibe": "loud"}, headers=H).status_code == 400
    assert c.post(f"/api/p/{code}/visuals", json={"world": "holi", "kaleido": True}, headers=H).json()["visuals"]["world"] == "holi"

    # Play: the opening song is ready for the browser, and it asks for the next join at once
    s = c.post(f"/api/p/{code}/transport", json={"action": "play"}, headers=H).json()
    eng = s["engine"]
    assert s["status"] == "playing" and len(eng["items"]) == 1 and eng["need_advance"], eng
    first = eng["items"][0]
    assert first["start_s"] == 0 and first["body"]["file"] in eng["media"], first
    assert c.get(eng["media"][first["body"]["file"]]["mix"]).status_code == 200
    s = c.post(f"/api/p/{code}/advance", json={}, headers=H).json()
    assert s["advance"]["status"] == "ready", s["advance"]
    items = s["engine"]["items"]
    assert len(items) == 2 and not s["engine"]["need_advance"], s["engine"]
    join = items[1]
    assert join["start_s"] > 30 and join["join"]["type"] in ("blend", "echo") and join["join"]["plays"], join["start_s"]
    for p in join["join"]["plays"] + [join["body"]]:
        assert {"stem", "at", "end", "src", "gain"} <= set(p), p.keys()
        assert p["stem"] == "asset" or p["file"] in s["engine"]["media"], p
    assert s["next"] and s["next"][0]["status"] == "locked", s["next"]
    assert c.post(f"/api/p/{code}/advance", json={}, headers=H).json()["advance"]["status"] == "ready", "one ahead is enough"
    with repository.session() as sdb:
        n_plays = sdb.scalar(select(func.count()).select_from(db.Play))
        clip = sdb.scalar(select(db.Play.clip).where(db.Play.position == 2))
    assert n_plays == 2 and clip and (library / clip).exists(), (n_plays, clip)

    # the music reaches the join: the next one is asked for and lands where the previous body says
    t = join["start_s"] + 1
    s = c.post(f"/api/p/{code}/sync", json={"playhead": t, "status": "playing"}, headers=H).json()
    assert s["engine"]["need_advance"] and s["now"]["name"], s["engine"]
    assert c.post(f"/api/p/{code}/sync", json={"playhead": -1, "status": "playing"}, headers=H).status_code == 400
    s = c.post(f"/api/p/{code}/advance", json={}, headers=H).json()
    items = s["engine"]["items"]
    assert [i["index"] for i in items] == [0, 1, 2] and items[2]["start_s"] > items[1]["start_s"] + 20, [i["start_s"] for i in items]

    # a guest's request; when it fits the song on air it may take the locked next song's place
    target = next(x["file"] for x in songs if x["file"] not in {i["file"] for i in items})
    s = c.post(f"/api/p/{code}/request", json={"file": target, "name": "Priya"}).json()
    assert s["requests"] or any(e.get("request") for e in s["next"]), (s["requests"], s["next"])
    assert c.post(f"/api/p/{code}/request", json={"file": "not/a/song.mp3"}).status_code == 400
    s = c.get(f"/api/p/{code}/state", headers=H).json()
    if not s["engine"]["swap_pending"]:  # the planner did not offer it: offer it by hand, to check the swap itself
        def offer(sess, st):
            i = sess.by_file[target]
            sess.early = {**sess.planner.entry(i, sess.planner.win[i][0], {}, "echo", "test", 0.7, 0.0), "request": True}
        party.act(code, offer)
        s = c.get(f"/api/p/{code}/state", headers=H).json()
    assert s["engine"]["swap_pending"], s["engine"]
    old_rid = s["engine"]["items"][-1]["rid"]
    s = c.post(f"/api/p/{code}/advance", json={"swap": True}, headers=H).json()
    new_last = s["engine"]["items"][-1]
    assert s["advance"]["status"] == "ready" and new_last["rid"] != old_rid and new_last["file"] == target, (s["advance"], new_last["file"])
    assert not s["engine"]["swap_pending"] and target not in [x for x in s["requests"]]
    print("request moved up:", s["next"][0]["name"], "replaces the locked next song")

    # guests: votes (song only, rate limited), reactions, talk without host powers
    s = c.post(f"/api/p/{code}/feedback", json={"guest": "guest-0001", "target": "song", "value": "up"}).json()
    assert s["feedback"]["song"]["up"] == 1, s["feedback"]
    assert c.post(f"/api/p/{code}/feedback", json={"guest": "guest-0001", "target": "transition", "value": "up"}).status_code == 400
    assert c.post(f"/api/p/{code}/feedback", json={"guest": "x", "target": "song", "value": "up"}).status_code == 400
    s = c.post(f"/api/p/{code}/react", json={"guest": "guest-0001", "emoji": "🔥"}).json()
    assert [r["emoji"] for r in s["reactions"]] == ["🔥"], s["reactions"]
    assert c.post(f"/api/p/{code}/react", json={"guest": "guest-0001", "emoji": "💩"}).status_code == 400
    real = talk.interpret
    talk.interpret = lambda text, state, songs, **k: {"reply": "Peak time!", "source": "gemini", "actions": [
        {"type": "set_level", "level": "max"}, {"type": "skip"}]}
    try:
        g = c.post(f"/api/p/{code}/talk", json={"text": "peak please"}).json()
        assert g["level"] != "max" and "Only the host" in g["talk"]["reply"], g["talk"]
        h = c.post(f"/api/p/{code}/talk", json={"text": "peak please"}, headers=H).json()
        assert h["level"] == "max" and h["talk"]["client_actions"] == ["skip"], h["talk"]
    finally:
        talk.interpret = real

    # media: only the library's own files
    assert c.get("/media/../secrets.txt").status_code in (404, 400)
    assert c.get("/media/tags/x.json").status_code == 404
    stem = s["engine"]["media"][items[1]["file"]]["vocals"] if "engine" in s else None

    # WebSockets: the host's screen gets its player's part and can report; a guest's does not
    with c.websocket_connect(f"/ws/p/{code}?key={key}") as ws:
        m = ws.receive_json()
        assert m["type"] == "state" and "engine" in m["state"], m.keys()
        ws.send_json({"type": "sync", "playhead": t + 2, "status": "paused"})
        ws.send_json({"type": "ping"})
        for _ in range(5):
            m = ws.receive_json()
            if m["type"] == "pong":
                break
    assert c.get(f"/api/p/{code}/state").json()["status"] == "paused"
    with c.websocket_connect(f"/ws/p/{code}") as ws:
        m = ws.receive_json()
        assert m["type"] == "state" and "engine" not in m["state"]
        ws.send_json({"type": "sync", "playhead": 0, "status": "playing"})  # ignored: not the host
    assert c.get(f"/api/p/{code}/state").json()["status"] == "paused"

    # start over
    s = c.post(f"/api/p/{code}/transport", json={"action": "restart"}, headers=H).json()
    assert s["status"] == "idle" and s["engine"]["epoch"] == 1 and not s["engine"]["items"], s["engine"]
    print(f"WEB PASS (party {code}, {n_plays} plays recorded, join recipe {len(str(join)) // 1024} KB)")


if __name__ == "__main__":
    main(*sys.argv[1:])
