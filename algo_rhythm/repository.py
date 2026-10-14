"""Reads and writes the database (DATABASE_URL): the song library, parties, plays, requests, and the AI cache.

load_songs() is what the player calls: songs from the database when DATABASE_URL is set, else from the files.
"""
import copy
import functools

from sqlalchemy import select

from algo_rhythm import db, dj, tagger
from algo_rhythm.config import settings

FEATURE_KINDS = ("rhythm_feel", "grid_slip", "vocal_segments")


@functools.cache
def _factory(url):
    engine = db.make_engine(url)
    db.create_all(engine)
    return db.session_factory(engine)


def session(url=None):
    """A new session on `url` (default DATABASE_URL). Use it as `with session() as s:`."""
    url = url or settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return _factory(url)()


def load_songs(library_dir):
    """(songs, skipped) for the player: database mode when DATABASE_URL is set, else file mode (dj.load_library)."""
    if not settings().database_url:
        return dj.load_library(library_dir)
    with session() as s:
        return load_library(s, library_dir)


def load_library(s, library_dir):
    """Exactly what dj.load_library returns, built from the database rows of the active songs.
    An analysis missing from song_features is computed from the audio (via storage) and saved."""
    rows = s.execute(select(db.Song, db.SongAnalysis).join(db.SongAnalysis)
                     .where(db.Song.status == "active")).all()
    llm = {a.song_id: a.answer for a in s.scalars(select(db.SongAI))}
    feats = {}
    for f in s.scalars(select(db.SongFeature)):
        feats.setdefault(f.song_id, {})[f.kind] = f.data
    rows.sort(key=lambda r: tagger.tag_key(r.Song.file) + ".json")  # the order dj.load_library reads tag files in
    songs, skipped, seen = [], [], set()
    for song, analysis in rows:
        have = feats.get(song.id, {})
        known = set(have)
        t, reason = dj.song_from(library_dir, copy.deepcopy(analysis.tag), copy.deepcopy(llm.get(song.id)), seen, have)
        if t is None:
            skipped.append((song.file, reason))
            continue
        for kind in set(have) - known:
            s.add(db.SongFeature(song_id=song.id, kind=kind, data=have[kind]))
        songs.append(t)
    s.commit()
    dj.finalize(songs)
    return songs, skipped


def _song_id(s, file):
    return s.scalar(select(db.Song.id).where(db.Song.file == file)) if file else None


def start_party(level):
    """A new party; returns its id."""
    with session() as s:
        p = db.Party(level=level)
        s.add(p)
        s.commit()
        return p.id


def record_play(party_id, position, file, start_s, end_s, transition=None, why=None):
    """One song played at a party: `file` is the song's library path (song["file"]), times are seconds in the song."""
    with session() as s:
        s.add(db.Play(party_id=party_id, position=position, song_id=_song_id(s, file), start_s=start_s, end_s=end_s,
                      transition=transition, why=why))
        s.commit()


def record_request(party_id, text, file=None, status="pending"):
    """A listener's request: what they typed, and the song it matched (file) if any. Returns its id."""
    with session() as s:
        r = db.SongRequest(party_id=party_id, text=text, song_id=_song_id(s, file), status=status)
        s.add(r)
        s.commit()
        return r.id


def set_play_clip(party_id, position, clip):
    """Note where the clip of the join into this play was written (relative to the library)."""
    with session() as s:
        play = s.scalar(select(db.Play).where(db.Play.party_id == party_id, db.Play.position == position))
        if play:
            play.clip = clip
            s.commit()


def record_feedback(party_id, position, guest, target, value):
    """A guest's vote; a second vote on the same play and target replaces the first."""
    with session() as s:
        row = s.scalar(select(db.Feedback).where(db.Feedback.party_id == party_id, db.Feedback.position == position,
                                                 db.Feedback.guest == guest, db.Feedback.target == target))
        if row:
            row.value, row.created_at = value, db.now()
        else:
            s.add(db.Feedback(party_id=party_id, position=position, guest=guest, target=target, value=value))
        s.commit()


def load_embeddings(model=None):
    """{song file: vector} from song_embeddings (only `model`'s, when given)."""
    with session() as s:
        q = select(db.Song.file, db.SongEmbedding.vector).join(db.SongEmbedding)
        if model:
            q = q.where(db.SongEmbedding.model == model)
        return {f: list(v) for f, v in s.execute(q)}


def save_embeddings(vectors, model):
    """Store {song file: vector} for `model`, replacing older ones."""
    with session() as s:
        ids = dict(s.execute(select(db.Song.file, db.Song.id).where(db.Song.file.in_(list(vectors)))).all())
        for f, v in vectors.items():
            if f in ids:
                s.merge(db.SongEmbedding(song_id=ids[f], model=model, dim=len(v), vector=list(v)))
        s.commit()


def cache_get(key):
    """The cached AI response for `key`, or None."""
    with session() as s:
        row = s.get(db.AICache, key)
        return row.response if row else None


def cache_put(key, task, model, prompt_version, request, response):
    with session() as s:
        s.merge(db.AICache(key=key, task=task, model=model, prompt_version=prompt_version,
                           request=request, response=response))
        s.commit()


def log_ai_call(task, model, prompt_version, cached, ok, latency_ms, error):
    with session() as s:
        s.add(db.AICall(task=task, model=model, prompt_version=prompt_version, cached=cached, ok=ok,
                        latency_ms=latency_ms, error=error))
        s.commit()


class _DbCache:
    """The AI cache as an object, for the GenAI gateway."""

    def get(self, key):
        return cache_get(key)

    def put(self, key, task, model, prompt_version, request, response):
        cache_put(key, task, model, prompt_version, request, response)


db_cache = _DbCache()
