"""Song trivia: one multiple-choice question per song on the big screen; guests answer on their phones for coins.

    python -m algo_rhythm.pipeline trivia      (the job) questions for every song that has none: from our own song data
                                               (who sings it, the year) and "did you know?" facts from Gemini
    trivia.pick(song_id, asked, rng)           a song's question for a round: about 1 in 4 from our data, 3 in 4 Gemini's
    trivia.phase(start, end, playhead)         where a round is: before, asking, reveal, after (None: no room for one)

A round, in seconds from the song's start (its join): the question opens at ASK_AT_S, answers are taken for OPEN_S,
then the reveal shows for REVEAL_S (the right option, how many got it, the fact, the top 5). It is computed from the
playhead, so pausing pauses it. A song whose part ends too soon after that gets no round. No AI runs during a party.
The answer is never sent to a screen before the reveal; scoring is on the server: COINS for a right answer, +FAST_BONUS
within FAST_S of the question opening. Coins are per party.
"""
import argparse
import logging
import random
import re
import sys
import tempfile
from pathlib import Path

from sqlalchemy import func, select

from algo_rhythm import db, genai, repository

log = logging.getLogger("algo_rhythm.trivia")
ASK_AT_S, OPEN_S, REVEAL_S, MIN_TAIL_S = 30.0, 20.0, 10.0, 5.0
COINS, FAST_BONUS, FAST_S = 10, 5, 5.0
DATA_SHARE = 0.25          # of the questions asked: from our song data; the rest Gemini's facts
FACTS_PER_SONG = 3
MIN_CONFIDENCE = 0.8       # Gemini's own: a fact it is less sure of is not asked
BATCH = 10
PROMPT_VERSION = "trivia-v1"
SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT",
          "required": ["n", "question", "options", "answer", "fact", "confidence"], "properties": {
              "n": {"type": "INTEGER"}, "question": {"type": "STRING"},
              "options": {"type": "ARRAY", "items": {"type": "STRING"}}, "answer": {"type": "INTEGER"},
              "fact": {"type": "STRING"}, "confidence": {"type": "NUMBER"}}}}
PROMPT = """You write trivia for a live Bollywood party. For each numbered song below, write up to {k} multiple-choice
questions with a surprising, true fact most listeners do not know: how the song was made, recorded or composed, the
music director or lyricist, the film's cast or story, awards, records, a remake or its original, a famous moment.
Rules:
 - Only facts you are certain of and that are widely documented. If unsure, write fewer questions or none.
 - The answer must not be given away by the song title or film name (they are on screen).
 - Exactly 4 options, short (at most 6 words), of the same kind, all plausible; one right. answer = its index (0-3).
 - question: at most 18 words, easy to medium, no trick questions, no lyrics quotes.
 - fact: one sentence (at most 25 words) shown after the answer: the interesting part, in plain words.
 - confidence: 0-1, how sure you are the right option is right. n: the song's number.
Songs (number | title | film | singers | year):
{songs}
"""


# ---------------------------------------------------------------- the questions (the job)

def _song_line(i, s):
    l = s.get("llm") or {}
    return f"{i} | {l.get('title') or s['name']} | {l.get('film') or '-'} | {', '.join(l.get('singers') or []) or '-'} | {l.get('year') or '-'}"


def _clean(x, n):
    return " ".join(str(x).split())[:n]


def check(q, song):
    """A usable question (tidied), or None."""
    opts = [_clean(o, 60) for o in q.get("options") or []]
    question, fact = _clean(q.get("question", ""), 160), _clean(q.get("fact", ""), 220)
    a = q.get("answer")
    if len(opts) != 4 or len({o.lower() for o in opts}) != 4 or not all(opts) or not question or not fact:
        return None
    if not isinstance(a, int) or isinstance(a, bool) or not 0 <= a <= 3:
        return None
    if float(q.get("confidence", 1.0)) < MIN_CONFIDENCE:
        return None
    l = song.get("llm") or {}
    shown = {x.lower() for x in (l.get("title"), l.get("film"), song["name"]) if x}
    if opts[a].lower() in shown or any(o.lower() in question.lower() for o in opts if len(o) > 3):
        return None  # the answer is on screen, or an option is given away in the question
    return {"question": question, "options": opts, "answer": a, "fact": fact}


def data_questions(i, songs, rng):
    """Questions from our own song data: who sings it, the year it came out. Wrong options from other songs."""
    s = songs[i]
    l = s.get("llm") or {}
    out = []
    singers = " & ".join((l.get("singers") or [])[:2])
    if singers:
        others = sorted({" & ".join((o.get("llm") or {}).get("singers", [])[:2]) for o in songs} - {singers, ""})
        if len(others) >= 3:
            opts = rng.sample(others, 3) + [singers]
            rng.shuffle(opts)
            film = f" in {l['film']}" if l.get("film") else ""
            out.append({"question": "Who sings this song?", "options": opts, "answer": opts.index(singers),
                        "fact": f"It is sung by {singers}{film}" + (f" ({l['year']})." if l.get("year") else ".")})
    year = l.get("year")
    if isinstance(year, int):
        near = [y for y in range(year - 8, year + 9) if y != year and y <= 2026]
        opts = rng.sample(near, 3) + [year]
        rng.shuffle(opts)
        what = f"{l['film']} came out in {year}" if l.get("film") else f"It came out in {year}"
        out.append({"question": "Which year did this song come out?", "options": [str(y) for y in opts],
                    "answer": opts.index(year), "fact": f"{what}."})
    return out


def gemini_questions(rows, generate=genai.generate, on_call=None):
    """{song index: [question]} for a batch [(index, song)]: Gemini's facts, checked; songs it is unsure of get none."""
    prompt = PROMPT.format(k=FACTS_PER_SONG, songs="\n".join(_song_line(i, s) for i, s in rows))
    got = generate("trivia", PROMPT_VERSION, [{"text": prompt}], SCHEMA, timeout=300,
                   **({"on_call": on_call} if on_call else {}))
    by = dict(rows)
    out = {}
    for q in got:
        i = q.get("n")
        if i in by and len(out.get(i, [])) < FACTS_PER_SONG:
            ok = check(q, by[i])
            if ok:
                out.setdefault(i, []).append(ok)
    return out


def run(limit=None, generate=genai.generate, on_call=None, rng=None):
    """The job: questions for every song that has none yet. Returns how many were stored."""
    rng = rng or random.Random(7)
    songs, _ = repository.load_songs(Path(tempfile.mkdtemp()))
    with repository.session() as s:
        has = set(s.scalars(select(db.SongTrivia.song_id).distinct()))
        ids = dict(s.execute(select(db.Song.file, db.Song.id)).all())
    todo = [(i, sg) for i, sg in enumerate(songs) if ids.get(sg["file"]) not in has][:limit]
    stored = 0
    for k in range(0, len(todo), BATCH):
        part = todo[k:k + BATCH]
        try:
            facts = gemini_questions(part, generate, on_call)
        except genai.GenAIError as e:
            log.warning("trivia: no Gemini facts for songs %d-%d (%s); data questions only", k, k + len(part), e)
            facts = {}
        with repository.session() as s:
            for i, sg in part:
                for kind, qs in (("data", data_questions(i, songs, rng)), ("fact", facts.get(i, []))):
                    for q in qs:
                        s.add(db.SongTrivia(song_id=ids[sg["file"]], kind=kind, question=q["question"],
                                            options=q["options"], answer=q["answer"], fact=q["fact"],
                                            source="data" if kind == "data" else "gemini", prompt_version=PROMPT_VERSION))
                        stored += 1
            s.commit()
        log.info("trivia: %d of %d songs done, %d questions so far", min(k + BATCH, len(todo)), len(todo), stored)
    return stored


# ---------------------------------------------------------------- a round in a party

def pick(song_id, asked, rng):
    """A question for this song not asked in this party yet: about DATA_SHARE of the time from our data, else a fact
    (whichever kind the song has). None when it has none left."""
    with repository.session() as s:
        rows = list(s.execute(select(db.SongTrivia.id, db.SongTrivia.kind).where(db.SongTrivia.song_id == song_id)))
    rows = [r for r in rows if r.id not in set(asked)]
    if not rows:
        return None
    want = "data" if rng.random() < DATA_SHARE else "fact"
    pool = [r for r in rows if r.kind == want] or rows
    return rng.choice(sorted(pool, key=lambda r: r.id)).id


def phase(start_s, end_s, playhead):
    """("before" | "asking" | "reveal" | "after", seconds left in that phase, opens at) for a song playing from
    start_s (music time) whose part ends at end_s; None when the round does not fit before its end."""
    opens = start_s + ASK_AT_S
    if end_s is None or opens + OPEN_S + REVEAL_S + MIN_TAIL_S > end_s:
        return None
    t = playhead - opens
    if t < 0:
        return "before", -t, opens
    if t < OPEN_S:
        return "asking", OPEN_S - t, opens
    if t < OPEN_S + REVEAL_S:
        return "reveal", OPEN_S + REVEAL_S - t, opens
    return "after", 0.0, opens


def question(qid):
    with repository.session() as s:
        return s.get(db.SongTrivia, qid)


def board(party_id, top=5):
    """The party's leaderboard: [{"name", "coins"}], best first."""
    with repository.session() as s:
        rows = s.execute(select(db.TriviaAnswer.guest, func.max(db.TriviaAnswer.name), func.sum(db.TriviaAnswer.coins))
                         .where(db.TriviaAnswer.party_id == party_id).group_by(db.TriviaAnswer.guest)
                         .order_by(func.sum(db.TriviaAnswer.coins).desc()).limit(top)).all()
    return [{"name": n or "Guest", "coins": int(c or 0)} for _, n, c in rows if c]


def counts(party_id, round_):
    """(answers, right answers) for a round."""
    with repository.session() as s:
        n = s.scalar(select(func.count()).select_from(db.TriviaAnswer).where(
            db.TriviaAnswer.party_id == party_id, db.TriviaAnswer.round == round_))
        right = s.scalar(select(func.count()).select_from(db.TriviaAnswer).where(
            db.TriviaAnswer.party_id == party_id, db.TriviaAnswer.round == round_, db.TriviaAnswer.correct.is_(True)))
    return int(n or 0), int(right or 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    print(f"{run(args.limit, on_call=repository.log_ai_call)} questions stored")


if __name__ == "__main__":
    sys.exit(main())
