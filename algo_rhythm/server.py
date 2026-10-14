"""algo-rhythm web server: the page, the JSON API and the player in one process (the speakers are on this machine).

    python -m algo_rhythm.server LIBRARY [--level medium] [--host 127.0.0.1] [--port 8765]

Nothing plays until Play is pressed on the page. Closing the admin tab stops the server (a reload within BYE_GRACE_S
keeps it); it also stops when idle and no page has asked for anything for IDLE_STOP_S (see watchdog).
Routes keep the shapes the page already uses; /talk adds "Talk to the DJ"; /search finds songs by name and meaning;
/healthz is for process supervisors.
"""
import argparse
import collections
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from algo_rhythm import config, dj, live, live_plan, mc, storage

log = logging.getLogger("algo_rhythm.server")
MAX_BODY = 4096
VOTES_PER_MIN = 10  # per guest id
REACTS_PER_MIN = 20  # live reactions per guest id...
REACTS_ALL_PER_MIN = 240  # ...and from everyone together, so a flood cannot bury the big screen
REACTIONS = ("🔥", "❤️", "🙌", "💃", "🎉")
REACTION_SHOW_S = 6  # a reaction stays in /state this long, so every screen polling once a second sees it
FEEDBACK_FILE = Path("_mixes/feedback.jsonl")  # under the library, in file mode: one JSON line per vote
GUEST_ACTIONS = ("request", "set_mood")
WORLDS = ("auto", "koi", "lanterns", "flowers", "holi")  # the page's Visuals menu  # what Ask the DJ may do from the guest page
GUEST_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
IDLE_STOP_S = 30 * 60  # nothing playing or queued and no page at all for this long: the server stops itself


class JsonLogs(logging.Formatter):
    """One JSON object per line, so a log collector (Cloud Logging, Loki) can index the fields."""

    def format(self, r):
        out = {"t": self.formatTime(r, "%Y-%m-%dT%H:%M:%S"), "level": r.levelname, "logger": r.name, "msg": r.getMessage()}
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out)


def setup_logging(level="INFO"):
    h = logging.StreamHandler()
    h.setFormatter(JsonLogs())
    logging.basicConfig(level=level, handlers=[h], force=True)


class Player:
    """The running DJ: session, speakers, the engine thread, and what the page last did."""

    def __init__(self, sess, sink, new_engine):
        self.sess, self.sink, self.new_engine = sess, sink, new_engine
        self.engine = new_engine()
        self.page = {"seen": time.monotonic()}  # when a page last asked for anything
        self.record_request = lambda text, file: None  # the database log, when there is a database
        self.visuals = {"world": "auto", "kaleido": False}  # the admin's pick; guest and presenter screens follow it
        self.ai = {}  # GenAI gateway options: the database cache and call log, when there is a database
        self.record_feedback = lambda position, guest, target, value, by: None  # the database or file log
        self.votes = {}  # (target, play position) -> {guest: "up" | "down"}; tallies for the page
        self.recent = {}  # (kind, guest) -> times of their recent votes or reactions (rate limit)
        self.votes_lock = threading.Lock()
        self.requesters = {}  # song name -> the name a guest gave with the request (shown as "requested by")
        self.reactions = collections.deque(maxlen=40)  # recent {"id", "emoji", "t"}
        self.reaction_id = 0
        self.guest_url = None  # the address guests open, when the server is open to the network

    def status(self):
        if self.engine.ident is None:
            return "idle"
        return "paused" if getattr(self.sink, "paused", False) else "playing"

    def view(self):
        state = self.sess.view(self.status())
        state["engine_error"] = str(self.engine.error) if self.engine.error else None
        state["feedback"] = self.tallies()
        state["visuals"] = dict(self.visuals)
        for e in [state["now"], *state["next"]]:
            if e and e.get("request") and e["name"] in self.requesters:
                e["by"] = self.requesters[e["name"]]
        cut = time.monotonic() - REACTION_SHOW_S
        with self.votes_lock:
            state["reactions"] = [{"id": r["id"], "emoji": r["emoji"]} for r in self.reactions if r["t"] > cut]
        state["guest_url"] = self.guest_url
        if self.sess.mc:
            state["mc"] = self.sess.mc.view()
        return state

    def tallies(self):
        """Up and down votes for the song on air and the latest mix (and whether that mix can still be rated)."""
        t = self.sess.feedback_targets()

        def count(target, pos):
            with self.votes_lock:
                v = list(self.votes.get((target, pos), {}).values())
            return {"position": pos, "up": v.count("up"), "down": v.count("down")}
        song = count("song", t["song"]) if t["song"] else None
        if song:
            song["name"] = self.join_of(t["song"])["to"]
        return {"song": song,
                "mix": {**count("transition", t["mix"]), "open": t["mix_open"], **self.join_of(t["mix"])} if t["mix"] else None}

    def join_of(self, pos):
        """The join that brought play `pos` in: from/to song names and files, and its type (start for the first song)."""
        sess = self.sess
        with sess.lock:
            tl = sess.timeline
            i = next((i for i, t in enumerate(tl) if t["entry"].get("position") == pos), None)
            if i is None:
                return {"from": None, "to": None, "from_file": None, "to_file": None, "kind": None}
            e, prev = tl[i]["entry"], tl[i - 1]["entry"] if i else None
            kind = (e.get("transition_info") or {}).get("type") or e["transition_in"] if prev else "start"
            a, b = sess.songs[prev["song"]] if prev else None, sess.songs[e["song"]]
            return {"from": a and a["name"], "to": b["name"], "from_file": a and a["file"], "to_file": b["file"], "kind": kind}

    def _limit(self, key, per_min, msg):
        """Allow at most per_min actions a minute for key; call with votes_lock held."""
        now = time.monotonic()
        if len(self.recent) > 5000:  # forget guests who have not done anything for a minute
            self.recent = {k: q for k, q in self.recent.items() if q and now - q[-1] < 60}
        q = self.recent.setdefault(key, collections.deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= per_min:
            raise RateLimited(msg)
        q.append(now)

    def vote(self, guest, target, value, host=False):
        """A vote on the song on air, or (the host only) the latest join; voting again on the same one replaces the first.
        host is the admin page's word, not checked: it keeps guests to song votes, it is not a security boundary."""
        if not isinstance(guest, str) or not GUEST_ID.fullmatch(guest):
            raise ValueError("bad guest id")
        if target not in ("song", "transition") or value not in ("up", "down"):
            raise ValueError("target is song or transition, value is up or down")
        if target == "transition" and host is not True:
            raise ValueError("only the host rates the joins")
        t = self.sess.feedback_targets()
        pos = t["song"] if target == "song" else t["mix"] if t["mix_open"] else None
        if pos is None:
            raise ValueError("nothing to rate right now" if target == "song" else "no mix to rate right now")
        with self.votes_lock:
            self._limit(("vote", guest), VOTES_PER_MIN, "too many votes; try again in a minute")
            self.votes.setdefault((target, pos), {})[guest] = value
        try:
            self.record_feedback(pos, guest, target, value, "host" if host is True else "guest")
        except Exception:
            log.exception("could not record the vote")

    def react(self, guest, emoji):
        """A live reaction from a guest: shown as a burst on the guest and presenter screens for REACTION_SHOW_S."""
        if not isinstance(guest, str) or not GUEST_ID.fullmatch(guest):
            raise ValueError("bad guest id")
        if emoji not in REACTIONS:
            raise ValueError("unknown reaction")
        with self.votes_lock:
            self._limit(("react", guest), REACTS_PER_MIN, "slow down a little")
            self._limit(("react", "*"), REACTS_ALL_PER_MIN, "lots of reactions right now; try again in a moment")
            self.reaction_id += 1
            self.reactions.append({"id": self.reaction_id, "emoji": emoji, "t": time.monotonic()})

    def transport(self, action):
        if action == "bye":  # the admin tab is closing: the watchdog stops the server unless the admin page comes back
            self.page["bye"] = time.monotonic()
        elif action == "skip":
            if self.engine.ident is not None:
                self.engine.skip.set()
        elif action == "pause":
            self.sink.paused = True
        elif action == "play":
            if self.engine.ident is None:
                self.engine.start()  # first Play: the music starts now, not before
            else:
                self.sink.paused = False
        elif action == "restart":
            self.engine.stop_flag.set()
            self.engine.join(30)  # it stops after the render in progress
            if self.engine.is_alive():
                raise Conflict("still finishing a transition; try again in a moment")
            self.sink.reset()
            self.sess.reset()
            self.engine = self.new_engine()  # a finished thread cannot be started again
        elif action in ("mc_on", "mc_off"):
            if not self.sess.mc:
                raise ValueError("there is no MC in this player")
            self.sess.mc.enabled = action == "mc_on"
            self.sess._say(f"MC {'on' if self.sess.mc.enabled else 'off'}")
        else:
            raise ValueError("unknown action")

    def talk(self, text, guest=False):
        """Talk to the DJ: words in, DJ actions out (algo_rhythm.talk). A guest may only request songs and set the
        mood; pause, skip, play and the energy level stay with the host (no auth: this keeps a guest's words from
        stopping the party, it is not a security boundary)."""
        from algo_rhythm import talk
        if not isinstance(text, str) or not text.strip():
            raise ValueError("say something to the DJ")
        sess = self.sess
        v = sess.view(self.status())
        state = {"level": v["level"], "mood": v.get("mood"), "status": v["status"], "now": (v.get("now") or {}).get("name"),
                 "next": [e.get("name") for e in v.get("next", [])][:3]}
        result = talk.interpret(text.strip()[:300], state, sess.songs, **self.ai)
        held = []
        if guest:
            held = [a for a in result["actions"] if a["type"] not in GUEST_ACTIONS]
            result = {**result, "actions": [a for a in result["actions"] if a["type"] in GUEST_ACTIONS]}

        def request(file):
            sess.request(file)
            self.safe_record(text.strip()[:300], file)

        done = talk.apply(result, set_level=sess.set_level, request=request,
                          skip=lambda: self.transport("skip"), play=lambda: self.transport("play"),
                          pause=lambda: self.transport("pause"), set_mood=sess.set_mood)
        log.info("talk %s", json.dumps({"text": text[:300], "source": result.get("source"), "done": done}))
        reply = result.get("reply", "") + (" Only the host can change the energy, pause or skip." if held else "")
        return {"reply": reply, "done": done, "source": result.get("source")}


    def request(self, file, by=None):
        """A request picked from the song list; it is logged with the song's name as what was asked for. `by` is the
        name the guest typed (optional), shown as "requested by" while the song is coming up and playing."""
        self.sess.request(file)
        name = next((s["name"] for s in self.sess.songs if s["file"] == file), file)
        by = " ".join(by.split())[:24] if isinstance(by, str) else ""
        by = "".join(ch for ch in by if ch.isprintable())
        if by:
            self.requesters.pop(name, None)
            self.requesters[name] = by
            while len(self.requesters) > 50:
                self.requesters.pop(next(iter(self.requesters)))
        self.safe_record(name, file)

    def safe_record(self, text, file):
        try:
            self.record_request(text, file)
        except Exception:
            log.exception("could not record the request")


class Conflict(Exception):
    pass


class RateLimited(Exception):
    pass


def create_app(player, allowed_hosts=None):
    app = FastAPI(title="algo-rhythm", docs_url="/docs", redoc_url=None)
    page_html = live.STATIC.read_bytes()
    visuals_js = (live.STATIC.parent / "visuals.js").read_bytes()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if allowed_hosts is not None and request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"error": "bad host"}, 403)  # stops DNS rebinding
        if request.method == "POST":
            # Non-simple content type: a page on another site cannot send this without a preflight we refuse.
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"error": "send application/json"}, 415)
            if not 0 < int(request.headers.get("content-length") or 0) <= MAX_BODY:
                return JSONResponse({"error": "bad body size"}, 400)
        if request.url.path != "/healthz":
            player.page["seen"] = time.monotonic()
        if request.url.path == "/":  # the admin page reloaded: cancel its goodbye
            player.page["bye"] = None
        return await call_next(request)

    async def body(request):
        try:
            data = json.loads(await request.body())
        except ValueError:
            raise ValueError("body is not JSON")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def act(fn):
        try:
            fn()
        except Conflict as e:
            return JSONResponse({"error": str(e)}, 409)
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, 400)
        return JSONResponse(player.view())

    @app.get("/")
    def index():
        return HTMLResponse(page_html, headers={"Cache-Control": "no-store"})

    @app.get("/guest")  # same page; it picks the guest view from the path
    def guest():
        return HTMLResponse(page_html, headers={"Cache-Control": "no-store"})

    @app.get("/presenter")  # same page; it picks the presenter view from the path
    def presenter():
        return HTMLResponse(page_html, headers={"Cache-Control": "no-store"})

    @app.get("/static/visuals.js")  # the page's visual engine
    def visuals():
        return Response(visuals_js, media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/state")
    def state():
        return JSONResponse(player.view(), headers={"Cache-Control": "no-store"})

    @app.get("/songs")
    def songs():
        return [{"file": s["file"], "name": s["name"], "mood": s["mood"]} for s in player.sess.songs]

    @app.get("/search")
    def search_songs(q: str = ""):
        from algo_rhythm import search
        songs = player.sess.songs
        named = set(search.name_hits(q, songs))  # the rest are only similar in meaning: the page says so
        return [{"file": songs[i]["file"], "name": songs[i]["name"], "mood": songs[i]["mood"], "named": i in named}
                for i in search.find(q, songs)]

    @app.get("/art/{name}")
    def art_image(name: str):
        """A song's generated image; only names listed in the art index, so no other file can be read."""
        if not re.fullmatch(r"[0-9a-f]{16}\.jpg", name) or name not in player.sess.art.values():
            return JSONResponse({"error": "no such image"}, 404)
        path = storage.local_path(f"art/{name}", player.sess.library)
        if not Path(path).exists():
            return JSONResponse({"error": "no such image"}, 404)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/healthz")
    def healthz():
        return {"ok": player.engine.error is None, "status": player.status(), "songs": len(player.sess.songs)}

    @app.post("/vibe")
    async def vibe(request: Request):
        data = await body(request)
        return act(lambda: player.sess.set_vibe(data["vibe"]) if "vibe" in data else player.sess.set_level(data["level"]))

    @app.post("/visuals")
    async def visuals_pick(request: Request):
        data = await body(request)

        def pick():
            if data.get("world") not in WORLDS or not isinstance(data.get("kaleido"), bool):
                raise ValueError("world must be one of " + ", ".join(WORLDS) + " and kaleido true or false")
            player.visuals = {"world": data["world"], "kaleido": data["kaleido"]}
        return act(pick)

    @app.post("/mood")
    async def mood(request: Request):
        data = await body(request)
        return act(lambda: player.sess.set_mood(data["mood"]))

    @app.post("/request")
    async def request_song(request: Request):
        data = await body(request)
        return act(lambda: player.request(data["file"], data.get("name")))

    @app.post("/transport")
    async def transport(request: Request):
        data = await body(request)
        return act(lambda: player.transport(data["action"]))

    @app.post("/feedback")
    async def feedback(request: Request):
        data = await body(request)
        try:
            player.vote(data.get("guest"), data.get("target"), data.get("value"), data.get("host") is True)
        except RateLimited as e:
            return JSONResponse({"error": str(e)}, 429)
        return act(lambda: None)

    @app.post("/react")
    async def react(request: Request):
        data = await body(request)
        try:
            player.react(data.get("guest"), data.get("emoji"))
        except RateLimited as e:
            return JSONResponse({"error": str(e)}, 429)
        return act(lambda: None)

    @app.post("/talk")
    def talk(data: dict):
        try:
            said = player.talk(data.get("text"), guest=data.get("guest") is True)
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, 400)
        return {**player.view(), "talk": said}

    @app.exception_handler(ValueError)
    async def bad_input(request, e):
        return JSONResponse({"error": str(e)}, 400)

    return app


def record_to_database(player, level):
    """Log this party's plays and requests, and keep Gemini's answers and call log, in the database."""
    from algo_rhythm import repository
    party, sess = repository.start_party(level), player.sess
    plain = lambda x: json.loads(json.dumps(x, default=float))  # numpy numbers -> JSON

    def on_play(e, info):
        w, song = e["window"], sess.songs[e["song"]]
        repository.record_play(party, e["position"], song["file"], w["start_s"], w["end_s"], plain(info),
                               plain({"why": e["why"], "score": e["score"], "match": e["match"], "terms": e["terms"],
                                      "level": sess.level, "energy": song["energy_pct"]}))

    sess.on_play = on_play
    if sess.clip_dir is not None:  # .../clips/files -> .../clips/<party>
        sess.clip_dir = Path(sess.clip_dir).parent / str(party)
        sess.on_clip = lambda pos, clip: repository.set_play_clip(party, pos, clip)
    player.record_feedback = lambda pos, guest, target, value, by: repository.record_feedback(party, pos, guest, target, value)
    player.record_request = lambda text, file: repository.record_request(party, text, file, "queued")
    player.ai = {"cache": repository.db_cache, "on_call": repository.log_ai_call}
    log.info("recording party %s to the database", party)


def record_to_file(player, path):
    """File mode: every vote is one JSON line in `path` (appended, so it survives a restart), with the join it is about,
    for tuning the scores later. A second vote on the same thing is a new line; the last line per guest wins."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    def write(pos, guest, target, value, by):
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "target": target, "value": value, "by": by, "guest": guest,
                "position": pos, "level": player.sess.level, **player.join_of(pos)}
        with lock, path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    player.record_feedback = write


def lan_ip():
    """This machine's address on the local network (no packet is sent), or None."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def should_stop(player, now):
    """The admin tab closed (and did not reload within BYE_GRACE_S): stop, even mid-song. Otherwise never while music is
    playing, paused or requested, and only after IDLE_STOP_S with no page asking for anything. Guest and presenter tabs never stop it."""
    bye = player.page.get("bye")
    if bye and now - bye > live.BYE_GRACE_S:
        return True
    busy = player.engine.is_alive() or bool(player.sess.requests)
    return not busy and now - player.page["seen"] > IDLE_STOP_S


def watchdog(player, server):
    while not server.should_exit:
        time.sleep(5)
        if should_stop(player, time.monotonic()):
            log.info("the admin tab was closed, or idle with no page open: stopping")
            server.should_exit = True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cfg = config.settings()  # environment first (see .env.example); flags override it
    ap.add_argument("library", type=Path, nargs="?", default=cfg.library_dir, help="default: LIBRARY_DIR")
    ap.add_argument("--level", choices=list(live_plan.LEVELS), default="medium")
    ap.add_argument("--start", help="open with the first song whose file name contains this text")
    ap.add_argument("--host", default=cfg.host, help="use 0.0.0.0 to open it to your Wi-Fi (no password!)")
    ap.add_argument("--port", type=int, default=cfg.port)
    ap.add_argument("--log-level", default=cfg.log_level)
    args = ap.parse_args(argv)
    setup_logging(args.log_level)
    if args.library is None:
        ap.error("give the library folder, or set LIBRARY_DIR")
    library = args.library.resolve()

    sess = live.build(library, args.level)
    sink = live.SpeakerSink(dj.OUT_SR)
    sess.playhead = sink.playhead
    sess.clip_dir = library / "_mixes" / "clips" / "files"  # a party's own folder in database mode
    player = Player(sess, sink, lambda: live.Engine(sess, library, sink, args.start))
    if cfg.database_url:
        record_to_database(player, args.level)
    else:
        record_to_file(player, library / FEEDBACK_FILE)
    sess.mc = mc.MC(ai=player.ai, titles=[s["name"] for s in sess.songs])  # AI MC, on by default; the admin page can switch it off
    local = args.host in ("127.0.0.1", "localhost")
    ip = None if local else args.host if args.host not in ("0.0.0.0", "::") else lan_ip()
    player.guest_url = f"http://{ip}:{args.port}/guest" if ip else None
    hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"} if local else None
    server = uvicorn.Server(uvicorn.Config(create_app(player, hosts), host=args.host, port=args.port, log_config=None))
    threading.Thread(target=watchdog, args=(player, server), daemon=True).start()
    log.info("ready: open http://%s:%s (nothing plays until you press Play)",
             args.host if local else "this-laptop-ip", args.port)
    if not local:
        log.warning("anyone on this network can control the music")
    try:
        server.run()
    finally:
        player.engine.stop_flag.set()
        sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
