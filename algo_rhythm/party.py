"""A party, held in the database (parties.state) so the web server keeps nothing between requests.

Each request loads the state, rebuilds a live.Session around it (the same planner and rules as the offline engine),
acts, and saves it back under a row lock (SELECT ... FOR UPDATE on PostgreSQL). The host's browser plays the music
and reports where it is (sync); everyone else's screen is built from that.

    code, key = party.create("medium")
    party.act(code, lambda sess, st: sess.set_vibe("party"))
    party.start(code)                    first Play: the opening song, ready for the browser
    party.advance(code)                  the next song and the join into it (seconds of work: the lock is not held)
    party.view(code, host=True)          what a page shows (+ what the host's player needs)

Music time: seconds since the party's first song started, without pauses. The host page keeps that clock; the
state holds its last report (playhead, reported_at) and everyone else extrapolates from it.
"""
import collections
import contextlib
import copy
import datetime
import hashlib
import json
import logging
import math
import os
import random
import secrets
import threading
import time
import uuid

import numpy as np
from sqlalchemy import func, select, text

from algo_rhythm import config, db, dj, joins, library, live, live_plan, mc, recipe, repository, storage, timing

log = logging.getLogger("algo_rhythm.party")
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: read off a big screen
CODE_LEN = 6
LOCK_WAIT_S = 20       # a request waits at most this long for the party's lock, then answers "busy, try again"
RENDER_STALE_S = 120  # a join still "rendering" after this long was abandoned (the request died): render it again
# Joins rendered at once on this server, whatever the party: each holds several song windows (100+ MB) and keeps a
# CPU busy, and Cloud Run sends up to 40 requests at a time to the one instance. More wait their turn.
RENDER_SLOTS = threading.BoundedSemaphore(int(os.environ.get("RENDER_SLOTS", "2")))
SWAP_MARGIN_S = 4      # a moved-up request must be ready this long before the join it replaces starts
REACTION_SHOW_S = 6
VOTES_PER_MIN, REACTS_PER_MIN, REACTS_ALL_PER_MIN = 10, 20, 240
REACTIONS = ("🔥", "❤️", "🙌", "💃", "🎉")
WORLDS = ("auto", "koi", "lanterns", "flowers", "holi")


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class RateLimited(Exception):
    pass


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_state(level):
    return {"v": 1, "level": level, "mood": None, "status": "idle", "epoch": 0, "playhead": 0.0, "reported_at": None,
            "counts": {}, "played": [], "requests": [], "deadline": {}, "early": None, "queue": [], "timeline": [],
            "log": [], "positions": 0, "logged": None, "visuals": {"world": "auto", "kaleido": False}, "requesters": {},
            "mc": {"enabled": True, "used": [], "level": None, "prev": None, "changed": False, "last_line": None,
                   "last_at": None}}


def create(level="medium", owner_id=None):
    """A new party: (code, host key). Only the hash of the key is stored; the host's page keeps the key.
    owner_id: the host account that started it (algo_rhythm.accounts)."""
    if level not in live_plan.LEVELS:
        raise ValueError("unknown level")
    key = secrets.token_urlsafe(24)
    with repository.session() as s:
        for _ in range(20):
            code = "".join(secrets.choice(CODE_CHARS) for _ in range(CODE_LEN))
            if not s.scalar(select(db.Party.id).where(db.Party.code == code)):
                break
        s.add(db.Party(level=level, code=code, host_key=hash_key(key), state=new_state(level), version=0,
                       updated_at=db.now(), owner_id=owner_id))
        s.commit()
    return code, key


def is_host(code, key):
    if not key:
        return False
    with repository.session() as s:
        h = s.scalar(select(db.Party.host_key).where(db.Party.code == code))
    return h is not None and secrets.compare_digest(h, hash_key(key))


# ---------------------------------------------------------------- state <-> live.Session

def _plain(x):
    """JSON-ready: numpy numbers as floats, infinity as None."""
    def fix(v):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        if isinstance(v, dict):
            return {str(k): fix(w) for k, w in v.items()}
        if isinstance(v, (list, tuple)):
            return [fix(w) for w in v]
        return v
    return fix(json.loads(json.dumps(x, default=lambda o: o.item() if hasattr(o, "item") else float(o))))


def _entry_out(e, songs):
    if e is None:
        return None
    out = {**e, "song": songs[e["song"]]["file"]}
    if "alts" in e:
        out["alts"] = [{**a, "song": songs[a["song"]]["file"]} for a in e["alts"]]
    return _plain(out)


def _entry_in(e, lib):
    if e is None:
        return None
    i = lib.by_file.get(e["song"])
    if i is None:  # the song left the library since: drop it
        return None
    out = {**e, "song": i}
    if "alts" in e:
        out["alts"] = [{**a, "song": lib.by_file[a["song"]]} for a in e["alts"] if a["song"] in lib.by_file]
    return out


def playhead_now(st):
    """Where the music is now (music time): the host's last report, moved on by the time since, unless paused."""
    ph = st.get("playhead") or 0.0
    if st.get("status") == "playing" and st.get("reported_at"):
        ph += max(0.0, time.time() - st["reported_at"])
    return ph


def session_from(st, lib):
    """A live.Session holding this state (its planner is the request's own; the library is shared)."""
    sess = live.Session(lib.songs, lib.planner(), st["level"], lib.root, None)
    sess.planner.mood = st["mood"]
    idx = lambda f: lib.by_file.get(f)
    sess.counts = dict(st["counts"])
    sess.played = [i for i in map(idx, st["played"]) if i is not None]
    sess.requests = [i for i in map(idx, st["requests"]) if i is not None]
    sess.deadline = {idx(f): n for f, n in st["deadline"].items() if idx(f) is not None}
    sess.early = _entry_in(st["early"], lib)
    sess.queue = [e for e in (_entry_in(x, lib) for x in st["queue"]) if e is not None]
    sess.timeline = []
    for t in st["timeline"]:
        e = _entry_in(t["entry"], lib)
        if e is not None:
            sess.timeline.append({**t, "entry": e, "start_s": float("inf") if t["start_s"] is None else t["start_s"]})
    sess.log = collections.deque(st["log"], maxlen=12)
    sess.positions = st["positions"]
    lg = st.get("logged")
    sess._logged = (idx(lg[0]), lg[1], lg[2]) if lg else None
    sess.art = lib.art
    sess.playhead = lambda: playhead_now(st)
    return sess


def dump(sess, st):
    """Write the session back into the state dict."""
    songs = sess.songs
    f = lambda i: songs[i]["file"]
    st.update(level=sess.level, mood=sess.planner.mood, counts=dict(sess.counts), played=[f(i) for i in sess.played][-200:],
              requests=[f(i) for i in sess.requests], deadline={f(i): n for i, n in sess.deadline.items()},
              early=_entry_out(sess.early, songs), queue=[_entry_out(e, songs) for e in sess.queue],
              timeline=[_plain({**t, "entry": _entry_out(t["entry"], songs)}) for t in sess.timeline],
              log=list(sess.log), positions=sess.positions,
              logged=[f(sess._logged[0]), sess._logged[1], sess._logged[2]] if sess._logged else None)


# ---------------------------------------------------------------- transactions

_locks = collections.defaultdict(threading.Lock)  # one process: requests for the same party take turns


@contextlib.contextmanager
def locked(code):
    """(row, state) of the party, locked; the state is saved when the block ends without an error.
    PostgreSQL: FOR NO KEY UPDATE, not FOR UPDATE. Rows that point at the party (plays, requests, votes) are written on
    other connections while the lock is held, and FOR UPDATE would make those inserts wait for it: a deadlock with
    ourselves. Never wait forever: LOCK_WAIT_S, then Conflict."""
    lock = _locks[code]
    if not lock.acquire(timeout=LOCK_WAIT_S):
        raise Conflict("the party is busy; try again in a moment")
    try:
        with repository.session() as s:
            q = select(db.Party).where(db.Party.code == code)
            if s.bind.dialect.name == "postgresql":
                s.execute(text(f"SET LOCAL lock_timeout = '{LOCK_WAIT_S}s'"))
                q = q.with_for_update(key_share=True)  # FOR NO KEY UPDATE
            row = s.scalar(q)
            if row is None or row.state is None:
                raise NotFound("no such party")
            st = copy.deepcopy(row.state)
            yield row, st
            row.state = st
            row.version = (row.version or 0) + 1
            row.updated_at = db.now()
            row.level = st["level"]
            s.commit()
    finally:
        lock.release()


def read(code):
    """(party id, state) without a lock: for showing, never for changing."""
    with repository.session() as s:
        row = s.scalar(select(db.Party).where(db.Party.code == code))
        if row is None or row.state is None:
            raise NotFound("no such party")
        return row.id, row.state


def act(code, fn):
    """Run fn(sess, state) on the party's session under its lock and save what it changed. Returns fn's answer."""
    lib = library.get()
    with locked(code) as (row, st):
        sess = session_from(st, lib)
        sess.party_id = row.id
        out = fn(sess, st)
        dump(sess, st)
        return out


# ---------------------------------------------------------------- the player

def _record_play(party_id, sess, e, info):
    w, song = e["window"], sess.songs[e["song"]]
    try:
        repository.record_play(party_id, e["position"], song["file"], w["start_s"], w["end_s"], _plain(info),
                               _plain({"why": e["why"], "score": e["score"], "match": e["match"], "terms": e["terms"],
                                       "level": sess.level, "energy": song["energy_pct"]}))
    except Exception:
        log.exception("could not record the play")


def start(code):
    """First Play: pick the opening song and make it ready for the browser. Idempotent while it is on."""
    lib = library.get()
    with locked(code) as (row, st):
        if st["status"] != "idle" and st["timeline"]:
            if st["status"] == "paused":
                st["status"], st["reported_at"] = "playing", time.time()
            return
        sess = session_from(st, lib)
        e = sess.start(None)
        ex = joins.load(lib.root, lib.songs[e["song"]], e["window"])
        sess.on_play = lambda entry, info: _record_play(row.id, sess, entry, info)
        sess.played_hook(e, None)
        sess.timeline[0].update(item=recipe.start_item(ex), ex=ex.p, body_start=0, skip=0, rid=uuid.uuid4().hex[:12])
        dump(sess, st)
        st.update(status="playing", playhead=0.0, reported_at=time.time())


def sync(code, playhead, status):
    """The host's player reports where the music is. Returns nothing; the hub sends everyone the new view."""
    if not isinstance(playhead, (int, float)) or not math.isfinite(playhead) or playhead < 0:
        raise ValueError("playhead is a number of seconds")
    if status not in ("playing", "paused"):
        raise ValueError("status is playing or paused")
    with locked(code) as (row, st):
        if st["status"] == "idle":
            return
        st.update(playhead=float(playhead), reported_at=time.time(), status=status)


def _mc_state(st):
    return st.setdefault("mc", new_state("medium")["mc"])


def _mc_line(lib, st_mc, level, J, j, rng):
    """(seconds after the join, voice audio, voice gain, line) for an MC line at this join, or None."""
    if not st_mc["enabled"]:
        return None
    m = mc.MC(enabled=True, ai={"cache": repository.db_cache, "on_call": repository.log_ai_call},
              titles=[s["name"] for s in lib.songs], rng=rng)
    for k in ("level", "prev", "changed", "last_line", "last_at"):
        setattr(m, k, st_mc[k])
    m.used = set(st_mc["used"])
    room = j.nxt.voc is not None and m.due(J, level) and live.mc_room(j.overlap, j.nxt.audio, j.nxt_skip, j.nxt.voc, j.info)
    st_mc.update(level=m.level, prev=m.prev, changed=m.changed)
    if not room:
        return None
    pool = m.lines()[m.key(level)]
    fresh = [x for x in pool if x not in m.used] or pool
    for line in rng.sample(fresh, len(fresh))[:3]:
        voice = speak(line)
        if voice is None or len(voice) > mc.MAX_LINE_S * dj.OUT_SR:
            continue
        music, segs, end_s = room
        spot = mc.placement(music, segs, voice, dj.OUT_SR, end_s=end_s)
        if spot is None:
            return None
        m.spoke(line, J + spot[0])
        st_mc.update(used=sorted(m.used)[-60:], last_line=m.last_line, last_at=m.last_at, changed=False)
        return spot[0], voice, spot[1], line
    return None


def speak(line):
    """The MC's voice for `line` (float stereo at dj.OUT_SR), made once per line and voice and kept as an asset."""
    import os
    import soundfile as sf
    voice_id = f"{os.environ.get('MC_TTS', 'gemini')}:{os.environ.get('MC_VOICE', 'Aoede')}:{os.environ.get('MC_STYLE', mc.MC_STYLE)}"
    key = hashlib.sha256(f"{voice_id}|{line}".encode()).hexdigest()
    with repository.session() as s:
        row = s.get(db.Asset, key)
    if row:
        path = storage.local_path(row.path, library.get().root)
        if path.exists():
            audio, sr = sf.read(path, always_2d=True, dtype="float32")
            return audio
    audio = mc.speak(line)
    if audio is not None:
        library.store_asset("mc", audio, key=key)
    return audio


def _clip(party_id, pos, j, skip):
    """The join as the server rendered it (CLIP_EDGE_S either side), for the Gemini review (algo_rhythm.review)."""
    import soundfile as sf
    try:
        sr, edge = dj.OUT_SR, live.CLIP_EDGE_S
        x = np.concatenate([j.before[max(skip, len(j.before) - edge * sr):], j.overlap, j.nxt.audio[j.nxt_skip:j.nxt_skip + edge * sr]])
        rel = f"_mixes/clips/{party_id}/{pos}.flac"
        path = library.get().root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(path, 0.97 * dj.soft_limit(x.copy()), sr, format="FLAC")
        if config.settings().storage_url:
            storage.upload(path, rel)
        repository.set_play_clip(party_id, pos, rel)
    except Exception:
        log.exception("could not write the clip of join %s", pos)


def advance(code, swap=False):
    """Make the next join ready: lock in the next song, render the join into it (without holding the lock: it takes
    seconds) and attach its recipe. swap=True: a fitting request takes the place of the locked next song instead.
    Returns {"status": "ready" | "rendering" | "nothing" | "failed"} ("failed": the song could not be mixed into and was
    dropped; the next call picks another). Every call that renders logs where its time went (one JSON line)."""
    with timing.request() as t:
        result = {"status": "error"}
        try:
            result = _advance(code, swap, t)
            return result
        finally:
            if "render" in t.stages:
                log.info("advance %s timing %s", code, t.summary(status=result["status"], swap=swap))


def _give_up(code, token, swap, drop, name=None):
    """A render that failed: free the claim at once (a stale "rendering" mark would stall the party for RENDER_STALE_S
    while the music runs out). drop: the next song cannot be mixed into at all, so it leaves the timeline and the
    planner, which counts it as played, picks another."""
    lib = library.get()
    with locked(code) as (row, st):
        sess = session_from(st, lib)
        tl = sess.timeline
        if swap:
            if (st.get("swapping") or {}).get("token") == token:
                st.pop("swapping")
        elif tl and "item" not in tl[-1] and (tl[-1].get("rendering") or [None])[0] == token:
            if drop:
                tl.pop()
                sess._say(f"skipped '{name}': it could not be mixed in")
            else:
                tl[-1].pop("rendering")
        dump(sess, st)


def _advance(code, swap, t):
    lib = library.get()
    token = uuid.uuid4().hex[:12]
    # 1. decide what to render, and claim it
    with t.stage("claim"), locked(code) as (row, st):
        party_id = row.id
        if st["status"] == "idle" or not st["timeline"]:
            return {"status": "nothing"}
        sess = session_from(st, lib)
        tl, ph = sess.timeline, playhead_now(st)
        last = tl[-1]
        if swap:
            early = sess.early
            if early is None or len(tl) < 2 or "item" not in last or last["start_s"] - ph < live.EARLY_MIN_LEFT_S:
                sess.early = None
                dump(sess, st)
                return {"status": "nothing"}
            sess.early = None
            st["swapping"] = {"token": token, "rid": last["rid"]}
            base, target = tl[-2], early
        else:
            if "item" not in last:
                r = last.get("rendering")
                if r and time.time() - r[1] < RENDER_STALE_S:
                    return {"status": "rendering"}
            elif last["start_s"] > ph + 1:
                return {"status": "ready"}  # the next join is ready and not on the air yet: one ahead is enough
            else:
                sess.commit_next(None)
            tl[-1]["rendering"] = [token, time.time()]
            base, target = tl[-2], tl[-1]["entry"]
        dump(sess, st)
        snap = {"base": copy.deepcopy(base), "target": copy.deepcopy(target), "level": st["level"],
                "mc": copy.deepcopy(_mc_state(st)), "pos": st["positions"] + 1}
    # 2. render it (no lock: guests can request and vote meanwhile)
    base, nxt_entry = snap["base"], snap["target"]
    name = lib.songs[nxt_entry["song"]]["name"]
    planned = copy.deepcopy(nxt_entry)
    said = []
    with t.stage("queue"):
        RENDER_SLOTS.acquire()
    try:
        return _render_and_attach(code, token, swap, t, lib, snap, base, nxt_entry, name, planned, said, party_id)
    finally:
        RENDER_SLOTS.release()


def _render_and_attach(code, token, swap, t, lib, snap, base, nxt_entry, name, planned, said, party_id):
    try:
        with t.stage("rebuild"):
            a = joins.rebuild(lib.root, lib.songs[base["entry"]["song"]], base["ex"])
    except Exception:
        log.exception("advance %s: could not rebuild the song on the air", code)
        _give_up(code, token, swap, drop=False)
        raise
    try:
        with t.stage("render"):
            planner = lib.planner()
            j = joins.render_join(lib.root, lib.songs, base["entry"], a, base["skip"], nxt_entry,
                                  alternatives=lambda limit: live.alternatives(planner, lib.songs, base["entry"], nxt_entry, limit),
                                  say=said.append)
    except Exception:
        # the planned join cannot be made: an echo-out (no stretch, no blend checks) into the same part instead
        log.exception("advance %s: the join into '%s' failed; trying an echo-out", code, name)
        nxt_entry.clear()
        nxt_entry.update(copy.deepcopy(planned), transition_in="echo")
        said = [f"into '{name}': the mix could not be made, used an echo-out"]
        try:
            with t.stage("render_fallback"):
                j = joins.render_join(lib.root, lib.songs, base["entry"], a, base["skip"], nxt_entry, say=said.append)
        except Exception:
            log.exception("advance %s: no join into '%s' at all; dropping it", code, name)
            _give_up(code, token, swap, drop=True, name=name)
            return {"status": "failed"}
    try:
        J = recipe.next_join_at(base["item"], base["body_start"], len(j.before))
        rng = random.Random(f"{code}:{snap['pos']}")
        with t.stage("mc"):
            try:
                line = _mc_line(lib, snap["mc"], snap["level"], J, j, rng)
            except Exception:  # the join goes ahead without the MC
                log.exception("advance %s: no MC line", code)
                line = None
        with t.stage("recipe"):
            item = recipe.join_item(a, j, J, asset=lambda kind, audio: library.store_asset(kind, audio),
                                    mc_line=line[:3] if line else None)
    except Exception:
        log.exception("advance %s: could not make the recipe of the join into '%s'", code, name)
        _give_up(code, token, swap, drop=False)
        raise
    body_start = j.nxt_skip
    # 3. attach it, unless the party moved on meanwhile (restart, another swap)
    with t.stage("attach"), locked(code) as (row, st):
        sess = session_from(st, lib)
        sess.on_play = lambda entry, info: _record_play(row.id, sess, entry, info)
        tl = sess.timeline
        if not tl:
            return {"status": "nothing"}
        last = tl[-1]
        if swap:
            sw = st.pop("swapping", None)
            if not sw or sw["token"] != token or last.get("rid") != sw["rid"] or last["start_s"] - playhead_now(st) < SWAP_MARGIN_S:
                dump(sess, st)
                return {"status": "nothing"}
            if not sess.swap_next(nxt_entry, last["entry"]):
                dump(sess, st)
                return {"status": "nothing"}
            for k in ("item", "rid", "ex", "skip", "body_start"):
                last.pop(k, None)
        elif (last.get("rendering") or [None])[0] != token:
            return {"status": "nothing"}  # restarted, or rendered by another request meanwhile
        last.pop("rendering", None)
        e = last["entry"]
        e.update(window=nxt_entry["window"], transition_in=nxt_entry["transition_in"], transition_info=_plain(j.info))
        last.update(start_s=J, overlap_s=item["overlap_s"], item=item, ex=j.nxt.p, skip=j.nxt_skip,
                    body_start=body_start, rid=token)
        for m in said:
            sess._say(m)
        mc_st = _mc_state(st)  # the MC noticed the level (and may have spoken): keep that, whatever changed meanwhile
        mc_st.update({k: snap["mc"][k] for k in ("used", "last_line", "last_at", "changed", "level", "prev")})
        if line:
            sess._say(f"MC: {line[3]}")
        sess.played_hook(e, j.info)
        pos = e["position"]
        dump(sess, st)
    with t.stage("clip"):
        _clip(party_id, pos, j, base["skip"])
    return {"status": "ready"}


def transport(code, action):
    """Host buttons the server must know about. Play and pause happen in the host's browser; this records them."""
    def go(sess, st):
        if action == "pause":
            if st["status"] == "playing":
                st.update(status="paused", playhead=playhead_now(st), reported_at=time.time())
        elif action == "resume":
            if st["status"] == "paused":
                st.update(status="playing", reported_at=time.time())
        elif action == "restart":
            sess.reset()
            st.update(status="idle", playhead=0.0, reported_at=None, epoch=st["epoch"] + 1)
            st.pop("swapping", None)
        elif action == "skip":
            sess._say("skipped to the next transition")
        elif action in ("mc_on", "mc_off"):
            _mc_state(st)["enabled"] = action == "mc_on"
            sess._say(f"MC {'on' if action == 'mc_on' else 'off'}")
        else:
            raise ValueError("unknown action")
    if action == "play":
        return start(code)
    return act(code, go)


# ---------------------------------------------------------------- guests

def request(code, file, by=None, text=None):
    """A request from the song list (by: the name the guest typed, shown as "requested by")."""
    def go(sess, st):
        sess.request(file)
        name = sess.songs[sess.by_file[file]]["name"]
        who = "".join(ch for ch in " ".join(by.split())[:24] if ch.isprintable()) if isinstance(by, str) else ""
        if who:
            req = st["requesters"]
            req.pop(name, None)
            req[name] = who
            while len(req) > 50:
                req.pop(next(iter(req)))
        try:
            repository.record_request(sess.party_id, text or name, file, "queued")
        except Exception:
            log.exception("could not record the request")
    return act(code, go)


def vote(code, guest, target, value, host=False):
    """A vote on the song on air, or (the host only) the latest join; voting again on the same one replaces it."""
    if not isinstance(guest, str) or not live_guest_id(guest):
        raise ValueError("bad guest id")
    if target not in ("song", "transition") or value not in ("up", "down"):
        raise ValueError("target is song or transition, value is up or down")
    if target == "transition" and not host:
        raise ValueError("only the host rates the joins")
    party_id, st = read(code)
    sess = session_from(st, library.get())
    t = sess.feedback_targets()
    pos = t["song"] if target == "song" else t["mix"] if t["mix_open"] else None
    if pos is None:
        raise ValueError("nothing to rate right now" if target == "song" else "no mix to rate right now")
    since = db.now() - datetime.timedelta(seconds=60)
    with repository.session() as s:
        n = s.scalar(select(func.count()).select_from(db.GuestAction).where(
            db.GuestAction.party_id == party_id, db.GuestAction.guest == guest, db.GuestAction.kind == "vote",
            db.GuestAction.created_at > since))
        if n >= VOTES_PER_MIN:
            raise RateLimited("too many votes; try again in a minute")
        s.add(db.GuestAction(party_id=party_id, guest=guest, kind="vote"))
        s.commit()
    repository.record_feedback(party_id, pos, guest, target, value)


def live_guest_id(g):
    import re
    return re.fullmatch(r"[A-Za-z0-9-]{8,64}", g) is not None


def react(code, guest, emoji):
    if not isinstance(guest, str) or not live_guest_id(guest):
        raise ValueError("bad guest id")
    if emoji not in REACTIONS:
        raise ValueError("unknown reaction")
    party_id, _ = read(code)
    since = db.now() - datetime.timedelta(seconds=60)
    with repository.session() as s:
        mine = s.scalar(select(func.count()).select_from(db.Reaction).where(
            db.Reaction.party_id == party_id, db.Reaction.guest == guest, db.Reaction.created_at > since))
        everyone = s.scalar(select(func.count()).select_from(db.Reaction).where(
            db.Reaction.party_id == party_id, db.Reaction.created_at > since))
        if mine >= REACTS_PER_MIN:
            raise RateLimited("slow down a little")
        if everyone >= REACTS_ALL_PER_MIN:
            raise RateLimited("lots of reactions right now; try again in a moment")
        s.add(db.Reaction(party_id=party_id, guest=guest, emoji=emoji))
        s.commit()


def talk(code, text, guest=False):
    """Talk to the DJ. A guest may only request songs and set the mood. Play, pause and skip are done by the host's
    browser: they come back as client_actions for it."""
    from algo_rhythm import talk as talk_mod
    if not isinstance(text, str) or not text.strip():
        raise ValueError("say something to the DJ")
    lib = library.get()
    _, st = read(code)
    sess = session_from(st, lib)
    v = sess.view(st["status"])
    state = {"level": v["level"], "mood": v.get("mood"), "status": v["status"], "now": (v.get("now") or {}).get("name"),
             "next": [e.get("name") for e in v.get("next", [])][:3]}
    result = talk_mod.interpret(text.strip()[:300], state, lib.songs, cache=repository.db_cache,
                                on_call=repository.log_ai_call)
    held = []
    if guest:
        held = [a for a in result["actions"] if a["type"] not in ("request", "set_mood")]
        result = {**result, "actions": [a for a in result["actions"] if a["type"] in ("request", "set_mood")]}
    client = []

    def go(sess, st):
        def req(file):
            sess.request(file)
            try:
                repository.record_request(sess.party_id, text.strip()[:300], file, "queued")
            except Exception:
                log.exception("could not record the request")
        return talk_mod.apply(result, set_level=sess.set_level, request=req, set_mood=sess.set_mood,
                              skip=lambda: client.append("skip"), play=lambda: client.append("play"),
                              pause=lambda: client.append("pause"))
    done = act(code, go)
    log.info("talk %s", json.dumps({"text": text[:300], "source": result.get("source"), "done": done}))
    reply = result.get("reply", "") + (" Only the host can change the energy, pause or skip." if held else "")
    return {"reply": reply, "done": done, "source": result.get("source"), "client_actions": client}


def set_visuals(code, world, kaleido):
    if world not in WORLDS or not isinstance(kaleido, bool):
        raise ValueError("world must be one of " + ", ".join(WORLDS) + " and kaleido true or false")
    act(code, lambda sess, st: st.update(visuals={"world": world, "kaleido": kaleido}))


# ---------------------------------------------------------------- what the pages show

def _tallies(party_id, sess):
    t = sess.feedback_targets()

    def count(target, pos):
        with repository.session() as s:
            rows = s.execute(select(db.Feedback.value, func.count()).where(
                db.Feedback.party_id == party_id, db.Feedback.target == target, db.Feedback.position == pos)
                .group_by(db.Feedback.value)).all()
        c = dict(rows)
        return {"position": pos, "up": c.get("up", 0), "down": c.get("down", 0)}
    song = count("song", t["song"]) if t["song"] else None
    if song:
        song["name"] = _join_of(sess, t["song"])["to"]
    mix = {**count("transition", t["mix"]), "open": t["mix_open"], **_join_of(sess, t["mix"])} if t["mix"] else None
    return {"song": song, "mix": mix}


def _join_of(sess, pos):
    tl = sess.timeline
    i = next((i for i, t in enumerate(tl) if t["entry"].get("position") == pos), None)
    if i is None:
        return {"from": None, "to": None, "from_file": None, "to_file": None, "kind": None}
    e, prev = tl[i]["entry"], tl[i - 1]["entry"] if i else None
    kind = (e.get("transition_info") or {}).get("type") or e["transition_in"] if prev else "start"
    a, b = sess.songs[prev["song"]] if prev else None, sess.songs[e["song"]]
    return {"from": a and a["name"], "to": b["name"], "from_file": a and a["file"], "to_file": b["file"], "kind": kind}


def engine_view(sess, st):
    """What the host's player needs: the joins it should have scheduled (from the song on air on), and whether to
    ask for the next one."""
    tl, ph = sess.timeline, playhead_now(st)
    now = sess._now_index(ph)
    first = max(0, (now or 0) - 1)
    items, media = [], {}
    for k, t in enumerate(tl[first:], first):
        if "item" not in t:
            continue
        items.append({"index": k, "rid": t["rid"], **t["item"]})
        for f, stem in ([(t["ex"]["file"], t["ex"]["stem"])] + ([(tl[k - 1]["ex"]["file"], tl[k - 1]["ex"]["stem"])] if k else [])):
            media[f] = library.song_media(f, stem)
    last = tl[-1] if tl else None
    rendering = bool(last and "item" not in last and last.get("rendering") and time.time() - last["rendering"][1] < RENDER_STALE_S)
    need = bool(st["status"] != "idle" and last and not rendering and ("item" not in last or last["start_s"] <= ph + 1))
    swap = bool(sess.early is not None and len(tl) >= 2 and last and "item" in last
                and last["start_s"] - ph >= live.EARLY_MIN_LEFT_S)
    return {"epoch": st["epoch"], "items": items, "media": media, "need_advance": need, "swap_pending": swap,
            "rendering": rendering}


def view(code, host=False):
    """What a page shows: the shape the page has always read (live.Session.view) plus votes, reactions, visuals,
    "requested by", the MC; for the host, also what its player needs (engine)."""
    lib = library.get()
    party_id, st = read(code)
    sess = session_from(st, lib)
    v = sess.view(st["status"])
    v["code"] = code
    v["playhead"] = round(playhead_now(st), 3)
    v["feedback"] = _tallies(party_id, sess)
    v["visuals"] = dict(st.get("visuals") or {"world": "auto", "kaleido": False})
    for e in [v["now"], *v["next"]]:
        if e and e.get("request") and e["name"] in st["requesters"]:
            e["by"] = st["requesters"][e["name"]]
    since = db.now() - datetime.timedelta(seconds=REACTION_SHOW_S)
    with repository.session() as s:
        v["reactions"] = [{"id": r.id, "emoji": r.emoji} for r in s.scalars(
            select(db.Reaction).where(db.Reaction.party_id == party_id, db.Reaction.created_at > since).order_by(db.Reaction.id))]
    m = _mc_state(st)
    nxt = mc.START_S if m["last_at"] is None else m["last_at"] + mc.MIN_GAP_S
    v["mc"] = {"enabled": m["enabled"], "last_line": m["last_line"], "last_at_s": m["last_at"],
               "next_eligible_s": round(nxt if m["changed"] else max(nxt, (m["last_at"] or 0) + mc.EVERY_S))}
    v["engine_error"] = None
    if host:
        v["engine"] = engine_view(sess, st)
    return v
