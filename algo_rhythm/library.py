"""The song library as one server instance holds it: read once from the database, shared by every party, never
changed by a request. This is the only thing the web server keeps in memory between requests (a cache: any instance
can rebuild it from the database at any time).

    lib = library.get()          songs, the planner's windows, search vectors, the art index; loaded on first use
    planner = lib.planner()      a planner for one request: its own level, mood and history, the shared caches
    library.media_url(rel)       where the browser fetches a song, a stem or an asset
    library.store_asset(...)     audio the server made for the browser (an MC line, a drum fill)
"""
import copy
import hashlib
import logging
import tempfile
import threading
import time
from pathlib import Path

from sqlalchemy import func, select

from algo_rhythm import config, db, dj, live, live_plan, media, repository, search, storage, tagger

log = logging.getLogger("algo_rhythm.library")
CHECK_S = 300  # how often an instance asks the database whether the library changed (an import ran)


class Library:
    def __init__(self, songs, root):
        self.songs, self.root = songs, Path(root)
        self.by_file = {s["file"]: i for i, s in enumerate(songs)}
        self._planner = live_plan.Planner(songs, live.EXCERPT_S)
        self.art = {}
        self.signature = None
        self.loaded_at = time.monotonic()

    def planner(self):
        """A planner for one request: shares the windows and pair caches (pure functions of the library) but has its
        own listener settings, so two parties never see each other's level, mood or history."""
        p = copy.copy(self._planner)
        p.recent, p.mood, p._level_e, p._penalty = [], None, 0.375, {}
        return p

    def song(self, file):
        i = self.by_file.get(file)
        return None if i is None else self.songs[i]


_lib = {"lib": None}
_lock = threading.Lock()


def signature():
    """Changes whenever songs are imported, archived or re-analysed: (active songs, last song id, analyses)."""
    with repository.session() as s:
        return (s.scalar(select(func.count()).select_from(db.Song).where(db.Song.status == "active")),
                s.scalar(select(func.max(db.Song.id))),
                s.scalar(select(func.count()).select_from(db.SongAnalysis)),
                s.scalar(select(func.count()).select_from(db.SongAI)))


def load():
    """Read the library from the database (DATABASE_URL); audio stays in storage until a request needs it."""
    root = config.settings().library_dir or Path(tempfile.gettempdir()) / "algo-rhythm-library"
    root.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    songs, skipped = repository.load_songs(root)
    lib = Library(songs, root)
    search.attach(root, songs)
    from algo_rhythm import art
    lib.art = art.load_index(root)
    lib.signature = signature()
    log.info("library loaded: %d songs (%d skipped) in %.1f s", len(songs), len(skipped), time.monotonic() - t0)
    return lib


def get():
    """The library, loaded on first use and reloaded when the database says it changed (checked every CHECK_S)."""
    with _lock:
        lib = _lib["lib"]
        if lib is None:
            lib = _lib["lib"] = load()
        elif time.monotonic() - lib.loaded_at > CHECK_S:
            lib.loaded_at = time.monotonic()
            try:
                if signature() != lib.signature:
                    lib = _lib["lib"] = load()
            except Exception:
                log.exception("could not check the library for changes; keeping the loaded one")
        return lib


def reset():
    """Forget the loaded library (tests, and after an import in the same process)."""
    with _lock:
        _lib["lib"] = None


# ---------------------------------------------------------------- media for the browser

def stem_rel(file):
    return media.stem_rel(tagger.tag_key(file))


def media_url(rel):
    """The browser's address for a file of the library: the server answers it (algo_rhythm.server /media). Quoted:
    song names hold spaces, "#" ("F#m") and "?" that would otherwise end the path."""
    from urllib.parse import quote
    return "/media/" + quote(rel, safe="/")


def store_asset(kind, audio, key=None):
    """Save audio the server made (canonical MP3) as LIBRARY/assets/<kind>/<key>.mp3, uploaded to STORAGE_URL when there
    is one, and note it in the assets table. key: what made it (the same line is made once); default: the audio itself.
    Returns its media url."""
    import numpy as np
    key = key or hashlib.sha256(np.ascontiguousarray(audio, dtype=np.float32).tobytes()).hexdigest()
    rel = f"assets/{kind}/{key[:32]}.mp3"
    with repository.session() as s:
        if s.get(db.Asset, key[:64]):
            return media_url(rel)
    lib = get()
    path = lib.root / rel
    media.encode(audio, media.SR, path)
    if config.settings().storage_url:
        storage.upload(path, rel)
    with repository.session() as s:
        s.merge(db.Asset(key=key[:64], kind=kind, path=rel, seconds=len(audio) / media.SR))
        s.commit()
    return media_url(rel)


def allowed_media(rel):
    """Only songs of the library, their vocal stems, assets and art may be fetched through /media."""
    lib = get()
    if rel in lib.by_file:
        return True
    if rel.startswith("stems/") and rel.endswith(".vocals.mp3"):
        return any(stem_rel(f) == rel for f in lib.by_file)
    if rel.startswith("assets/") and ".." not in rel:
        with repository.session() as s:
            return s.scalar(select(db.Asset.key).where(db.Asset.path == rel)) is not None
    return False


def song_media(file, stem):
    """{"mix": url, "vocals": url or None} for a song."""
    return {"mix": media_url(file), "vocals": media_url(stem_rel(file)) if stem else None}
