"""Runnable check for the web server (stateless API + WebSockets), no browser:

    python -m tests.test_web [LIBRARY]      (default test_library, see tests/test_recipe.py)

A temp SQLite database with LIBRARY imported, the library folder as storage. Plays a party through the API the way the
host's page does (play, advance, sync, a request moved up, votes, reactions, talk, restart) and checks what every
screen gets, over HTTP and over the WebSocket.
"""
import os
import time
import sys
import tempfile
from pathlib import Path


def main(library="test_library"):
    library = Path(library).resolve()
    tmp = Path(tempfile.mkdtemp())
    # before config.settings() is first read; TEST_DATABASE_URL: an empty PostgreSQL database instead (row locks!)
    os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp / 'algo_rhythm.db'}"
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

    # host accounts: hosting needs an approved, signed-in account; a signup waits for approval
    from algo_rhythm import accounts
    r = c.post("/api/parties", json={"level": "medium"})
    assert r.status_code == 401 and r.json()["login"], r.text
    r = c.post("/api/auth/register", json={"username": "NewHost", "email": "new@example.com", "password": "longenough1"})
    assert r.status_code == 201 and r.json() == {"ok": True, "username": "newhost", "active": False}, r.text
    with repository.session() as sdb:
        u = sdb.scalar(select(db.User).where(db.User.username == "newhost"))
    assert u and u.active is False and u.password_hash.startswith("scrypt$") and "longenough1" not in u.password_hash
    r = c.post("/api/auth/login", json={"username": "newhost", "password": "longenough1"})
    assert r.status_code == 401 and "approval" in r.json()["error"] and accounts.COOKIE not in r.cookies, r.text
    for bad, why in (({"username": "newhost", "email": "x@example.com", "password": "longenough1"}, "taken"),
                     ({"username": "other", "email": "NEW@example.com", "password": "longenough1"}, "email"),
                     ({"username": "ab", "email": "a@example.com", "password": "longenough1"}, "Username"),
                     ({"username": "abc", "email": "not-an-email", "password": "longenough1"}, "email"),
                     ({"username": "abc", "email": "a@example.com", "password": "short"}, "Password")):
        r = c.post("/api/auth/register", json=bad)
        assert r.status_code == 400 and why in r.json()["error"], (bad, r.text)
    accounts.set_active("newhost", True)  # approved by hand
    r = c.post("/api/auth/login", json={"username": "newhost", "password": "wrong-password"})
    assert r.status_code == 401 and r.json()["error"] == "Wrong username or password.", r.text
    r = c.post("/api/auth/login", json={"username": "nobody", "password": "wrong-password"})
    assert r.status_code == 401 and r.json()["error"] == "Wrong username or password.", r.text
    r = c.post("/api/auth/login", json={"username": "NewHost", "password": "longenough1"})
    assert r.status_code == 200 and r.json()["user"]["username"] == "newhost", r.text
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert c.get("/api/auth/me").json()["user"]["username"] == "newhost"
    assert c.get("/login").status_code == 200

    # a party: the host gets a key, guests only the code
    r = c.post("/api/parties", json={"level": "medium"})
    code, key = r.json()["code"], r.json()["host_key"]
    with repository.session() as sdb:
        assert sdb.scalar(select(db.Party.owner_id).where(db.Party.code == code)) == u.id
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
    # the host shows or hides the join QR on the big screen; guests cannot
    assert c.get(f"/api/p/{code}/state").json()["screen"]["qr"] is True
    assert c.post(f"/api/p/{code}/screen", json={"qr": False}).status_code == 403
    assert c.post(f"/api/p/{code}/screen", json={"qr": "no"}, headers=H).status_code == 400
    assert c.post(f"/api/p/{code}/screen", json={"qr": False}, headers=H).json()["screen"]["qr"] is False
    assert c.get(f"/api/p/{code}/state").json()["screen"]["qr"] is False
    assert c.post(f"/api/p/{code}/screen", json={"qr": True}, headers=H).json()["screen"]["qr"] is True

    # the host's guest count: guests' live screens, each guest once (two tabs), not the big screen
    # (one event loop for all of them, as on a server: a TestClient used without "with" makes one per connection)
    with TestClient(server.create_app()) as cl, cl.websocket_connect(f"/ws/p/{code}?role=guest&g=guest-count-0001") as w1, \
         cl.websocket_connect(f"/ws/p/{code}?role=guest&g=guest-count-0001") as w2, \
         cl.websocket_connect(f"/ws/p/{code}?role=guest&g=guest-count-0002") as w3, \
         cl.websocket_connect(f"/ws/p/{code}?role=screen") as w4:
        for w in (w1, w2, w3, w4):
            w.receive_json()
        time.sleep(1.5)  # a tick: the count is written
        n = cl.get(f"/api/p/{code}/state", headers=H).json()["guests_online"]
    assert n == 2, n
    # the leaderboard on the big screen: the host's switch
    assert c.post(f"/api/p/{code}/screen", json={"board": True}).status_code == 403
    v = c.post(f"/api/p/{code}/screen", json={"board": True}, headers=H).json()
    assert v["screen"] == {"qr": True, "board": True} and v["leaderboard"] == [], v["screen"]
    c.post(f"/api/p/{code}/screen", json={"board": False}, headers=H)
    # the host goes offline: the screens say so, the clock stops 20 s on
    st = party.read(code)[1]
    if st["status"] == "playing":
        party.act(code, lambda sess, s_: s_.update(reported_at=s_["reported_at"] - 120))
        v = c.get(f"/api/p/{code}/state").json()
        assert v["host_offline"] and v["host_offline_s"] >= 100, (v["host_offline"], v["host_offline_s"])
        assert v["playhead"] <= st["playhead"] + party.HOST_GONE_S + 1, (v["playhead"], st["playhead"])
        party.sync(code, st["playhead"], "playing")
        assert not c.get(f"/api/p/{code}/state").json()["host_offline"]

    # sign out; then failed sign-ins from one address are limited
    assert c.post("/api/auth/logout", json={}).status_code == 200 and c.get("/api/auth/me").json()["user"] is None
    assert c.post("/api/parties", json={"level": "medium"}).status_code == 401
    codes = [c.post("/api/auth/login", json={"username": "newhost", "password": "nope-nope"}).status_code
             for _ in range(accounts.LOGIN_FAILS)]
    assert codes[-1] == 429 and set(codes[:-1]) <= {401, 429}, codes
    assert c.post("/api/auth/login", json={"username": "newhost", "password": "longenough1"}).status_code == 429
    print(f"WEB PASS (party {code}, {n_plays} plays recorded, join recipe {len(str(join)) // 1024} KB)")


if __name__ == "__main__":
    main(*sys.argv[1:])
