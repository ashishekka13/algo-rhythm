"""The party report: what played, what the room did, where the planner was wrong, and what the next party starts from.

    report.party_id_for(code)          the party's id (LookupError: no such party)
    report.build(party_id, narrate)    the whole report (a dict); never raises for a thin party
    report.narrate_report(data)        Gemini's write-up over the figures; None when it is unavailable (never raises)

Code computes every number; Gemini only writes prose over figures handed to it, and is told never to invent a number,
a song, a time or a name. A party's figures stop changing once it has ended, so the write-up's prompt is the same on
every reload and the AI cache answers it: reloading the report makes no second Gemini call.

The disagreements here (joins the planner rated >= CONFIDENT that the room voted down, with >= MIN_VOTES votes) are a
per-party view of sql/analytics.sql query 3's idea. If CONFIDENT or MIN_VOTES change, change that query too.
"""
import datetime
import logging
import math
from collections import Counter, defaultdict

from sqlalchemy import select

from algo_rhythm import db, genai, live_plan, repository

log = logging.getLogger("algo_rhythm.report")
PROMPT_VERSION = "report-v1"
MIN_VOTES = 3    # a vote needs this many guests before it is a verdict, not one person's opinion
CONFIDENT = 0.7  # the planner thought the join was good at/above this
TIMELINE_BINS = 40


def party_id_for(code):
    with repository.session() as s:
        pid = s.scalar(select(db.Party.id).where(db.Party.code == code))
    if pid is None:
        raise LookupError("no such party")
    return pid


def _utc(t):
    return t if t is None or t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def _iso(t):
    return _utc(t).isoformat() if t else None


def _library():
    try:
        from algo_rhythm import library
        return library.get()
    except Exception:        # no library on this instance is not a reason to lose the report
        return None


def _art(lib, sha):
    """The song's generated image (algo_rhythm.art), as live.art_url; None when it has none yet (the page draws one)."""
    try:
        name = lib.art.get((sha or "")[:16]) if lib else None
    except Exception:
        return None
    return f"/art/{name}" if name else None


def _tally(rows):
    """{(position, target): {"up": n, "down": n}} from feedback rows."""
    out = defaultdict(lambda: {"up": 0, "down": 0})
    for f in rows:
        if f.value in ("up", "down"):
            out[(f.position, f.target)][f.value] += 1
    return out


def build(party_id, narrate=False):
    """The report of a party (see the module docstring). Two songs and no votes give the same shape with zeroes."""
    with repository.session() as s:
        row = s.get(db.Party, party_id)
        if row is None:
            raise LookupError("no such party")
        st = row.state or {}
        host = s.scalar(select(db.User.username).where(db.User.id == row.owner_id)) if row.owner_id else None
        plays = s.execute(select(db.Play, db.Song).join(db.Song, db.Song.id == db.Play.song_id)
                          .where(db.Play.party_id == party_id).order_by(db.Play.position, db.Play.id)).all()
        reviews = {}
        if plays:
            for r in s.scalars(select(db.TransitionReview).where(db.TransitionReview.play_id.in_([p.id for p, _ in plays]))
                               .order_by(db.TransitionReview.id)):
                reviews[r.play_id] = r                       # the latest review of each join
        votes = _tally(s.scalars(select(db.Feedback).where(db.Feedback.party_id == party_id)))
        n_votes = sum(v["up"] + v["down"] for v in votes.values())
        reactions = s.execute(select(db.Reaction.created_at, db.Reaction.emoji, db.Reaction.guest)
                              .where(db.Reaction.party_id == party_id).order_by(db.Reaction.created_at)).all()
        requests = s.execute(select(db.SongRequest.text, db.SongRequest.status, db.SongRequest.song_id,
                                    db.SongRequest.created_at).where(db.SongRequest.party_id == party_id)
                             .order_by(db.SongRequest.id)).all()
        answers = s.scalars(select(db.TriviaAnswer).where(db.TriviaAnswer.party_id == party_id)
                            .order_by(db.TriviaAnswer.id)).all()
        guests = set(s.scalars(select(db.Feedback.guest).where(db.Feedback.party_id == party_id)))
        guests |= set(s.scalars(select(db.GuestAction.guest).where(db.GuestAction.party_id == party_id)))
    guests |= {g for _, _, g in reactions} | {a.guest for a in answers}

    # Plays seen once each (a play recorded twice for one position keeps the first), timed in music seconds from the
    # night's start: each song starts where the last one did, plus its part, less the join's overlap.
    seen, rows_ = set(), []
    for p, song in plays:
        if p.position not in seen:
            seen.add(p.position)
            rows_.append((p, song))
    lib = _library()
    by_file = (lib.by_file if lib else {}) or {}
    started = _utc(row.started_at)
    if rows_:
        started = _utc(rows_[0][0].played_at)
        tail = rows_[-1][0]
        last = _utc(tail.played_at) + datetime.timedelta(seconds=max(0.0, (tail.end_s or 0) - (tail.start_s or 0)))
    else:
        last = started
    level = st.get("level") or "medium"
    songs, at = [], 0.0
    for k, (p, song) in enumerate(rows_):
        t, why = p.transition or {}, p.why or {}
        seconds = max(0.0, (p.end_s or 0) - (p.start_s or 0))
        if k:
            at = max(0.0, at + songs[-1]["seconds"] - float(t.get("overlap_s") or 0.0))
        idx = by_file.get(song.file)
        lsong = lib.songs[idx] if lib is not None and idx is not None else {}
        rv = reviews.get(p.id)
        sv, mv = votes[(p.position, "song")], votes[(p.position, "transition")]
        songs.append({
            "position": p.position, "name": lsong.get("name") or song.name, "file": song.file,
            "art": _art(lib, song.sha256), "at_s": round(at, 1),
            "kind": t.get("type") or ("start" if k == 0 else None),
            "match": why.get("match") if k else None, "why": why.get("why"),
            "energy": why.get("energy"), "target": live_plan.LEVELS.get(why.get("level") or level),
            "mood": lsong.get("mood"), "seconds": round(seconds, 1), "requested": False,
            "song_up": sv["up"], "song_down": sv["down"], "mix_up": mv["up"], "mix_down": mv["down"],
            "reactions": 0, "emoji": None,
            "gemini_score": rv.score if rv else None, "gemini_issues": list(rv.issues or []) if rv else [],
            "gemini_summary": rv.summary if rv else None,
        })
    if songs:   # wall-clock end: whichever is later, the last play's record or the music's own length
        last = max(last, started + datetime.timedelta(seconds=songs[-1]["at_s"] + songs[-1]["seconds"]))
    span = max(1.0, (last - started).total_seconds())

    # reactions: to the song playing when they were sent, and in TIMELINE_BINS bins over the night
    timeline = [0] * TIMELINE_BINS
    emojis = defaultdict(Counter)
    for when, emoji, _ in reactions:
        x = (_utc(when) - started).total_seconds()
        timeline[min(TIMELINE_BINS - 1, max(0, int(x / span * TIMELINE_BINS)))] += 1
        k = max((i for i, e in enumerate(songs) if e["at_s"] <= x), default=0) if songs else None
        if k is not None:
            songs[k]["reactions"] += 1
            emojis[k][emoji] += 1
    for k, c in emojis.items():
        songs[k]["emoji"] = c.most_common(1)[0][0]

    played_ids = {song.id for _, song in rows_}
    by_id = {song.id: k for k, (_, song) in enumerate(rows_)}
    req = []
    for text, status, song_id, _ in requests:
        played = song_id in played_ids
        if played:
            songs[by_id[song_id]]["requested"] = True
        req.append({"text": text, "status": status, "played": played})

    joins = songs[1:]
    blends = sum(1 for x in joins if x["kind"] == "blend")
    echoes = sum(1 for x in joins if (x["kind"] or "").startswith("echo"))
    matches = [x["match"] for x in joins if x["match"] is not None]
    totals = {
        "songs": len(songs), "minutes_of_music": round(sum(x["seconds"] for x in songs) / 60, 1),
        "guests": len(guests), "requests": len(req), "requests_played": sum(1 for r in req if r["played"]),
        "reactions": len(reactions), "votes": n_votes, "blends": blends, "echoes": echoes,
        "blend_rate": round(blends / len(joins), 2) if joins else 0.0,
        "avg_match": round(sum(matches) / len(matches), 2) if matches else 0.0,
    }

    energy = [{"at_s": x["at_s"], "energy": x["energy"], "target": x["target"], "name": x["name"],
               "reactions": x["reactions"]} for x in songs]
    top = sorted((x for x in songs if x["song_up"] + x["song_down"] >= MIN_VOTES),
                 key=lambda x: (x["song_up"] - x["song_down"], x["reactions"]), reverse=True)[:5]

    disagreements = [{"position": x["position"], "name": x["name"], "match": x["match"], "kind": x["kind"],
                      "down": x["mix_down"], "up": x["mix_up"], "why": x["why"]}
                     for x in joins if x["match"] is not None and x["match"] >= CONFIDENT
                     and x["mix_down"] > x["mix_up"] and (x["mix_up"] + x["mix_down"]) >= MIN_VOTES]

    both = [x for x in joins if x["gemini_score"] is not None and (x["mix_up"] + x["mix_down"]) >= MIN_VOTES]
    agreed = sum(1 for x in both if (x["gemini_score"] >= 4) == (x["mix_up"] > x["mix_down"]))
    matrix = {"ai_good|room_good": [], "ai_good|room_bad": [], "ai_bad|room_good": [], "ai_bad|room_bad": []}
    for x in both:
        cell = f"ai_{'good' if x['gemini_score'] >= 4 else 'bad'}|room_{'good' if x['mix_up'] > x['mix_down'] else 'bad'}"
        matrix[cell].append(x["name"])
    calibration = {"judged": len(both), "agreed": agreed, "rate": round(agreed / len(both), 2) if both else None,
                   "matrix": matrix,
                   "note": (f"Only {len(both)} join{'' if len(both) == 1 else 's'} had both a Gemini review and "
                            f"{MIN_VOTES}+ votes: too few to say how well they agree.") if len(both) < 5 else None}

    players = {}
    for a in answers:
        pl = players.setdefault(a.guest, {"name": None, "coins": 0, "right": 0, "answered": 0, "_s": 0.0})
        pl["name"] = a.name or pl["name"] or "Guest"
        pl["coins"] += a.coins or 0
        pl["right"] += 1 if a.correct else 0
        pl["answered"] += 1
        pl["_s"] += a.seconds or 0.0
    trivia = {"players": sorted(({"name": p["name"], "coins": p["coins"], "right": p["right"], "answered": p["answered"],
                                  "seconds": round(p["_s"] / p["answered"], 1)} for p in players.values()),
                                key=lambda p: (-p["coins"], p["seconds"]))[:10],
              "rounds": len({a.round for a in answers})}

    by_mood = {}
    for x in songs:
        m = by_mood.setdefault(x["mood"] or "unknown", {"played": 0, "up": 0, "down": 0, "reactions": 0})
        m["played"] += 1
        m["up"] += x["song_up"]
        m["down"] += x["song_down"]
        m["reactions"] += x["reactions"]
    profile = {
        "liked": [x["name"] for x in sorted(songs, key=lambda x: (x["song_up"] - x["song_down"], x["reactions"]),
                                           reverse=True) if x["song_up"] > x["song_down"]][:8],
        "disliked": [x["name"] for x in songs if x["song_down"] > x["song_up"]][:8],
        "by_mood": by_mood,
        "weak_joins": [{"position": x["position"], "from": songs[i]["name"], "to": x["name"], "kind": x["kind"],
                        "up": x["mix_up"], "down": x["mix_down"], "gemini_score": x["gemini_score"]}
                       for i, x in enumerate(joins) if x["mix_down"] > x["mix_up"]
                       or (x["gemini_score"] is not None and x["gemini_score"] <= 2)],
        "unplayed_requests": [r["text"] for r in req if not r["played"]],
    }

    out = {
        "party": {"code": row.code, "id": row.id, "host": host, "started_at": _iso(started),
                  "ended_at": _iso(last), "minutes": math.ceil(span / 6) / 10,   # up: never short of the music
                  "location": (st.get("location") or {}).get("city"), "level": level, "mood": st.get("mood"),
                  "status": st.get("status")},
        "totals": totals, "songs": songs, "energy": energy, "top_songs": top, "requests": req,
        "disagreements": disagreements, "calibration": calibration, "trivia": trivia,
        "reaction_timeline": timeline, "profile": profile,
    }
    if narrate and songs:
        n = narrate_report(out)
        if n:
            out["narrative"] = n
    return out


SCHEMA = {"type": "OBJECT", "required": ["headline", "story", "highlights", "next_time"], "properties": {
    "headline": {"type": "STRING"},
    "story": {"type": "STRING"},
    "highlights": {"type": "ARRAY", "items": {"type": "OBJECT", "required": ["label", "text"], "properties": {
        "label": {"type": "STRING"}, "text": {"type": "STRING"}}}},
    "next_time": {"type": "STRING"}}}

PROMPT = """You write the short end-of-night note for the host of a house party run by an AI DJ.
Below are the night's figures, all counted by code. Use only them.
Rules:
- Never invent a number, a song, a time or a guest's name. Quote figures exactly as given, or leave them out.
- You did not hear the music: never say it sounded good, smooth or tight. Say what the room did (votes, reactions,
  requests), because that was counted.
- headline: at most 6 words, no exclamation mark.
- story: 2 or 3 short paragraphs (separate them with a blank line), second person ("you"), warm and plain, no
  marketing words, no exclamation marks.
- highlights: up to 4, each a short label and one sentence.
- next_time: one sentence on what the next party could start from, drawn from the figures.

{facts}"""


def _facts(d):
    """The figures as compact text: the only thing Gemini sees."""
    t, p = d["totals"], d["party"]
    lines = [f"Night: {p['minutes']} minutes, {t['songs']} songs, {t['guests']} guests, level {p['level']}"
             + (f", in {p['location']}" if p.get("location") else ""),
             f"Joins: {t['blends']} blends, {t['echoes']} echo-outs; average planner match {t['avg_match']}",
             f"Room: {t['votes']} votes, {t['reactions']} reactions, {t['requests']} requests "
             f"({t['requests_played']} played)"]
    lines.append("Songs in order (name | energy 0-1 | song votes up/down | join votes up/down | reactions):")
    for x in d["songs"]:
        e = "?" if x["energy"] is None else round(x["energy"], 2)
        lines.append(f"  {x['position']}. {x['name']} | {e} | {x['song_up']}/{x['song_down']} | "
                     f"{x['mix_up']}/{x['mix_down']} | {x['reactions']}" + (" | requested" if x["requested"] else ""))
    if d["top_songs"]:
        lines.append("Most liked: " + "; ".join(x["name"] for x in d["top_songs"]))
    if d["disagreements"]:
        lines.append("Joins the planner was sure of that the room voted down: "
                     + "; ".join(f"{x['name']} ({x['down']} down, {x['up']} up)" for x in d["disagreements"]))
    peak = max(range(len(d["reaction_timeline"])), key=lambda i: d["reaction_timeline"][i]) if any(d["reaction_timeline"]) else None
    if peak is not None:
        lines.append(f"Reactions peaked {round(peak / len(d['reaction_timeline']) * 100)}% of the way through the night")
    if d["trivia"]["players"]:
        lines.append(f"Trivia: {d['trivia']['rounds']} rounds, {len(d['trivia']['players'])} players")
    if d["profile"]["unplayed_requests"]:
        lines.append("Requests not played: " + "; ".join(d["profile"]["unplayed_requests"][:5]))
    return "\n".join(lines)


def narrate_report(data, generate=None):
    """Gemini's write-up of the figures in `data` ({headline, story, highlights, next_time}), or None: a host ending a
    party at 2 am must not get an error because the daily cap ran out."""
    try:
        gen = generate or genai.generate
        out = gen("report", PROMPT_VERSION, [{"text": PROMPT.format(facts=_facts(data))}], SCHEMA,
                  cache=repository.db_cache, on_call=repository.log_ai_call, timeout=60)
        if len(str(out.get("headline") or "").split()) > 6:
            out["headline"] = ""   # cut short it would end mid-phrase: the page uses its own headline instead
        out["highlights"] = list(out.get("highlights") or [])[:4]
        return out
    except Exception:
        log.exception("could not write the report's narrative")
        return None
