"""Gemini judges recorded transitions: each join's clip (8 s before, the overlap, 8 s after) is sent as audio and
scored 1-5 with a list of issues. Batch job, after or during a party; needs DATABASE_URL (clips are listed on plays).

    python -m algo_rhythm.review [--party N] [--limit 15] [--library LIBRARY_DIR]

Only plays with a clip and no review yet are sent. Stops cleanly when the daily Gemini cap (or quota) is used up.
"""
import argparse
import base64
import logging
import sys
from pathlib import Path

import soundfile as sf
from sqlalchemy import select

from algo_rhythm import db, genai, live, repository, storage
from algo_rhythm.config import settings

PROMPT_VERSION = "review-v1"
ISSUES = ["beat_clash", "vocal_clash", "lyric_cut", "volume_jump", "abrupt", "muddy", "none"]
SCHEMA = {"type": "OBJECT", "properties": {
    "score": {"type": "INTEGER", "description": "1 = clearly bad mix, 5 = seamless"},
    "smooth": {"type": "BOOLEAN"},
    "issues": {"type": "ARRAY", "items": {"type": "STRING", "enum": ISSUES}},
    "summary": {"type": "STRING", "description": "one short sentence"}},
    "required": ["score", "smooth", "issues", "summary"]}
PROMPT = """You are an experienced Bollywood DJ judging one transition between two songs in a live DJ set.
The audio clip is {edge} s of the outgoing song "{a}", then the {ov:.0f} s transition ({kind}), then {edge} s of the
incoming song "{b}". The transition starts at about {edge} s and ends at about {end:.0f} s into the clip.
Judge only the transition: do the beats stay locked (beat_clash), do two singers overlap (vocal_clash), is a sung line
cut off mid-word (lyric_cut), is there a sudden loudness change (volume_jump), does it sound like a hard, unplanned
cut (abrupt), is it cluttered or boomy (muddy)? An echo-out is a deliberate effect; judge whether it lands well.
Give score 1-5 (5 = a seamless, crowd-pleasing mix), smooth true or false, the issues you hear (["none"] if there are
none), and a one-sentence summary."""
log = logging.getLogger("algo_rhythm.review")


class _NoCache:
    """The reviews table is the cache; keep megabytes of base64 audio out of ai_cache."""

    def get(self, key):
        return None

    def put(self, *a):
        pass


def pending(s, party=None, limit=15):
    """(play, outgoing song name, incoming song name) for clips without a review, oldest first."""
    reviewed = select(db.TransitionReview.play_id)
    q = (select(db.Play, db.Song.name).join(db.Song, db.Song.id == db.Play.song_id)
         .where(db.Play.clip.is_not(None), db.Play.id.not_in(reviewed)).order_by(db.Play.id).limit(limit))
    if party is not None:
        q = q.where(db.Play.party_id == party)
    out = []
    for play, name in s.execute(q).all():
        prev = s.scalar(select(db.Song.name).join(db.Play, db.Play.song_id == db.Song.id)
                        .where(db.Play.party_id == play.party_id, db.Play.position == play.position - 1))
        out.append((play, prev or "unknown", name))
    return out


def judge(play, a, b, library, generate=genai.generate):
    """Gemini's answer for one play's clip, checked against SCHEMA (score must be 1-5). Raises genai errors."""
    path = storage.local_path(play.clip, library)
    audio = path.read_bytes()
    ov = max(0.0, sf.info(path).duration - 2 * live.CLIP_EDGE_S)  # the overlap is what is left after the two edges
    kind = (play.transition or {}).get("type", "blend")
    text = PROMPT.format(edge=live.CLIP_EDGE_S, a=a, b=b, ov=ov, kind=kind, end=live.CLIP_EDGE_S + ov)
    parts = [{"text": text}, {"inlineData": {"mimeType": "audio/flac", "data": base64.b64encode(audio).decode()}}]
    ans = generate("review", PROMPT_VERSION, parts, SCHEMA, cache=_NoCache(), on_call=repository.log_ai_call,
                   timeout=120)
    if not 1 <= ans["score"] <= 5:
        raise genai.GenAIError(f"answer.score: {ans['score']} is not 1-5")
    return ans


def run(party=None, limit=15, library=None, generate=genai.generate):
    """Review up to `limit` clips; returns how many were stored. A bad answer skips that clip; QuotaError stops."""
    library = Path(library or settings().library_dir or "library_100")
    with repository.session() as s:
        todo = pending(s, party, limit)
    done = 0
    for play, a, b in todo:
        try:
            ans = judge(play, a, b, library, generate)
        except genai.QuotaError as e:
            print(f"stopped: {e}")
            break
        except (genai.GenAIError, OSError) as e:
            print(f"play {play.id}: skipped ({e})")
            continue
        with repository.session() as s:
            s.add(db.TransitionReview(play_id=play.id, model=genai.model_name(), prompt_version=PROMPT_VERSION,
                                      score=ans["score"], smooth=ans["smooth"], issues=ans["issues"],
                                      summary=ans["summary"][:500]))
            s.commit()
        done += 1
        print(f"play {play.id} ({a} -> {b}): score {ans['score']}, smooth {ans['smooth']}, "
              f"issues {', '.join(ans['issues'])}: {ans['summary']}")
    print(f"reviewed {done} of {len(todo)} clips")
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--party", type=int)
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--library", type=Path, help="where clips are read from (default LIBRARY_DIR)")
    args = ap.parse_args(argv)
    logging.basicConfig(level="INFO")
    if not settings().database_url:
        raise SystemExit("set DATABASE_URL: clips are listed on the plays table")
    run(args.party, args.limit, args.library)
    return 0


if __name__ == "__main__":
    sys.exit(main())
