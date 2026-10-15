"""The MC's line library: a fresh line from Gemini for the moment (the place, the vibe, a dedication), or one already
spoken and kept, so most lines cost no AI call and no voice rendering.

    m = mc_lines.Moment(party_id=7, owner_id=3, level="high", build=False, mood="party", place=("Bengaluru", "bengaluru"))
    got = mc_lines.pick(m, rng, speak)        (line row, audio) or None
    mc_lines.heard(row_id, m)                 after the line is mixed in: counted, and kept away from this party
                                              and, for HEARD_DAYS, from its host

Every line is stored (mc_lines) with how it may be reused, decided when it is written, never by reading its text:
  generic   (no place, no names)       any party
  location  (names the place)          parties at that place only (place key: "bengaluru", "india")
  personal  (a dedication's names)     its own party only: never offered again
and its level (or "build", the energy rising), mood and voice: another voice never reuses an old recording.

Choosing for a moment: the stored lines that fit it, minus those this party heard and those its host heard in the
last HEARD_DAYS, least used first. A fresh line is written when fewer than MIN_POOL fit, else about one time in
FRESH_EVERY; a dedication is always fresh. Gemini answers within DEADLINE_S or a stored line (or a built-in one) is
used: the music never waits for it.
"""
import concurrent.futures
import dataclasses
import datetime
import hashlib
import logging
import os
import re

from sqlalchemy import or_, select, update

from algo_rhythm import db, genai, mc, places, repository

log = logging.getLogger("algo_rhythm.mc_lines")
FRESH_EVERY = 3
MIN_POOL = 5
HEARD_DAYS = 7
DEADLINE_S = 8
MAX_WORDS = 10
DEDICATION_WORDS = 7   # a dedication must fit a short instrumental gap: "For Ishan, from Priya!" is about 2 s
PROMPT_VERSION = "mc-line-v1"
SCHEMA = {"type": "OBJECT", "required": ["line", "uses_place", "names"], "properties": {
    "line": {"type": "STRING"}, "uses_place": {"type": "BOOLEAN"},
    "names": {"type": "ARRAY", "items": {"type": "STRING"}}}}
FEEL = {"low": "warm and easy, the night is young or winding down", "medium": "groove, get moving",
        "high": "party, dance floor energy", "max": "peak time, hands up, the loudest moment"}
BLOCKED = re.compile(r"\b(fuck|shit|bitch|bastard|chutiya|madarchod|bhenchod|sex|drugs?|drunk|kill)\b", re.I)
PROMPT = """You are the MC at a live Bollywood party. Write ONE short hype line, spoken by a text-to-speech voice over
the music right now, between sung lines. English or Hinglish, at most {max_words} words.
Energy: {level} ({feel}).{build}{mood}{place}{dedication}
Do not name any song, film, artist or other place, make no promise about which song comes next, nothing rude.
Not like these recent lines: {recent}
Return: line; uses_place = true if the line says {place_name}; names = every person's name in the line.
"""
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="mc-line")


class NoCache:
    """A fresh line must really be fresh: the AI cache would hand back the same answer to the same prompt."""
    def get(self, key):
        return None

    def put(self, *a, **k):
        pass


@dataclasses.dataclass
class Moment:
    party_id: int
    owner_id: int | None
    level: str
    build: bool = False
    mood: str | None = None
    place: tuple = (None, None)        # (what the MC says, key), places.spoken()
    voice: str = ""
    titles: frozenset = frozenset()    # song titles, plain (mc.song_titles): a line never names a song
    dedication: dict | None = None     # {"to": "Ishan", "by": "Priya", "after": bool}: a personal line, always fresh
    recent: tuple = ()                 # lines heard lately in this party, to write unlike them


def voice_id():
    """The voice a line is recorded in: lines are only reused in the same voice."""
    return f"{os.environ.get('MC_TTS', 'gemini')}:{os.environ.get('MC_VOICE', 'Aoede')}:{os.environ.get('MC_STYLE', mc.MC_STYLE)}"


def asset_key(voice, text):
    """The assets row (and file) holding a line's recording (party.speak stores it under this key)."""
    return hashlib.sha256(f"{voice}|{text}".encode()).hexdigest()


def candidates(m, exclude=(), limit=20):
    """Stored lines that fit the moment and that neither this party nor (lately) its host heard, least used first."""
    since = db.now() - datetime.timedelta(days=HEARD_DAYS)
    with repository.session() as s:
        heard = db.MCPlay.party_id == m.party_id
        if m.owner_id is not None:
            heard = or_(heard, (db.MCPlay.owner_id == m.owner_id) & (db.MCPlay.played_at >= since))
        heard = select(db.MCPlay.line_id).where(heard)
        scope = db.MCLine.scope == "generic"
        if m.place[1]:
            scope = or_(scope, (db.MCLine.scope == "location") & (db.MCLine.place == m.place[1]))
        q = (select(db.MCLine).where(db.MCLine.voice == m.voice, db.MCLine.audio.is_not(None), scope,
                                     db.MCLine.id.not_in(heard))
             .order_by(db.MCLine.uses, db.MCLine.last_used_at.is_not(None), db.MCLine.last_used_at, db.MCLine.id))
        q = q.where(db.MCLine.build.is_(True)) if m.build else q.where(db.MCLine.build.is_(False), db.MCLine.level == m.level)
        if m.mood:
            q = q.where(or_(db.MCLine.mood == m.mood, db.MCLine.mood.is_(None)))
        if exclude:
            q = q.where(db.MCLine.id.not_in(list(exclude)))
        return list(s.scalars(q.limit(limit)))


def check(answer, m):
    """(text, scope) for a usable answer, else None. The place and names are checked in the text itself: the scope
    is what the line actually says, whatever the model claimed."""
    text = " ".join(str(answer.get("line", "")).replace('"', "").split())
    low = f" {mc.plain(text)} "
    if not text or len(text.split()) > MAX_WORDS or len(text) > 90 or BLOCKED.search(text):
        return None
    if any(f" {t} " in low for t in m.titles):  # the MC never names a song
        return None
    names = [n for n in answer.get("names") or [] if str(n).strip()]
    say = m.place[0]
    others = [p for p in places.POPULAR if f" {p} " in low and (not say or p != say.lower())]
    if others:  # another place than the party's
        return None
    if m.dedication:
        to = m.dedication["to"]
        if f" {mc.plain(to)} " not in low or len(text.split()) > DEDICATION_WORDS + 2:
            return None
        return text, "personal"
    if names:  # a generic line never names anyone
        return None
    if say and f" {mc.plain(say)} " in low:
        return text, "location"
    return text, "generic"


def write(m, generate=genai.generate, on_call=None):
    """A fresh line from Gemini within DEADLINE_S: (text, scope) or None."""
    say = m.place[0]
    ded = m.dedication
    prompt = PROMPT.format(
        max_words=MAX_WORDS, level=m.level, feel=FEEL.get(m.level, ""),
        build=" The energy is rising right now, building toward the peak." if m.build else "",
        mood=f" Mood: {m.mood}." if m.mood else "",
        place=(f" The party is in {say}: about half the time, greet the room by name (\"What's up {say}!\")."
               if say and not ded else ""),
        dedication=((f" This line says the song that just played was for {ded['to']}" if ded.get("after") else
                     f" This line dedicates the song starting now to {ded['to']}")
                    + (f", from {ded['by']}" if ded.get("by") else "")
                    + f". Say {ded['to']}'s name. Keep it very short: at most {DEDICATION_WORDS} words, like "
                    + (f"\"That one was for {ded['to']}!\"" if ded.get("after") else f"\"For {ded['to']}, with love!\"") if ded else ""),
        recent="; ".join(m.recent[-6:]) or "none", place_name=say or "a place")
    call = _pool.submit(generate, "mc-line", PROMPT_VERSION, [{"text": prompt}], SCHEMA, cache=NoCache(),
                        **({"on_call": on_call} if on_call else {}))
    try:
        got = check(call.result(timeout=DEADLINE_S), m)
    except concurrent.futures.TimeoutError:
        log.info("MC line: Gemini took over %d s, using a stored line", DEADLINE_S)
        return None
    except genai.GenAIError as e:
        log.info("MC line: no fresh line (%s)", e)
        return None
    if got is None:
        log.info("MC line: Gemini's line did not pass the checks")
    return got


def store(m, text, scope, audio, source="gemini"):
    """Keep a spoken line for reuse; returns its row."""
    from algo_rhythm import library
    row = db.MCLine(text=text, scope=scope, place=m.place[1] if scope == "location" else None, level=m.level,
                    mood=m.mood, build=m.build, voice=m.voice, source=source,
                    audio=f"assets/mc/{asset_key(m.voice, text)[:32]}.mp3", seconds=round(len(audio) / mc.dj.OUT_SR, 2),
                    party_id=m.party_id if scope == "personal" else None)
    with repository.session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
    return row


def pick(m, rng, speak, *, exclude=(), generate=genai.generate, on_call=None):
    """(row, audio) for the moment: fresh or reused (see the module notes); None when there is nothing to say."""
    pool = [] if m.dedication else candidates(m, exclude)
    fresh = m.dedication is not None or len(pool) < MIN_POOL or rng.random() < 1 / FRESH_EVERY
    if fresh:
        got = write(m, generate, on_call)
        if got:
            text, scope = got
            audio = speak(text)
            if audio is not None:
                return store(m, text, scope, audio), audio
        if m.dedication:  # a dedication says the name: no stored line can stand in for it
            return None
    for row in pool:
        audio = speak(row.text)  # the stored recording (party.speak finds it by voice and text)
        if audio is not None:
            return row, audio
    return None


def heard(line_id, m):
    """The line was mixed into this party: one more use, and kept from this party and its host for a while."""
    with repository.session() as s:
        s.add(db.MCPlay(line_id=line_id, party_id=m.party_id, owner_id=m.owner_id))
        s.execute(update(db.MCLine).where(db.MCLine.id == line_id).values(uses=db.MCLine.uses + 1, last_used_at=db.now()))
        s.commit()
