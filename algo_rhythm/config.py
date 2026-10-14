"""Settings, read from the environment once (settings()). Bad values stop the program with a clear message.

    DATABASE_URL   sqlite:///path.db or postgresql://...   unset = file mode (LIBRARY/tags, enrich, _mixes)
    STORAGE_URL    file:///abs/path or gs://bucket/prefix  unset = the audio is already in LIBRARY_DIR
    LIBRARY_DIR    local library / cache folder the audio code reads from (needed with STORAGE_URL)
    GEMINI_MODEL   default gemini-3.5-flash-lite
    GEMINI_API_KEY else the key alone in ~/.config/song-matcher/gemini_key
    HOST, PORT     default 127.0.0.1, 8765
    LOG_LEVEL      default INFO
"""
import functools
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

KEY_FILE = Path.home() / ".config/song-matcher/gemini_key"


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    storage_url: str | None
    library_dir: Path | None
    gemini_model: str
    gemini_api_key: str | None = field(repr=False)
    host: str
    port: int
    log_level: str


@functools.cache
def settings():
    env = os.environ.get
    db, store, lib = env("DATABASE_URL") or None, env("STORAGE_URL") or None, env("LIBRARY_DIR") or None
    if db and not db.startswith(("sqlite", "postgresql")):
        raise SystemExit(f"DATABASE_URL must start with sqlite:// or postgresql://, got {db.split(':')[0]}:...")
    if store and not (store.startswith("gs://") and len(store) > 5 or store.startswith("file:///")):
        raise SystemExit(f"STORAGE_URL must be file:///abs/path or gs://bucket/prefix, got {store}")
    if store and not lib:
        raise SystemExit("STORAGE_URL needs LIBRARY_DIR: the local folder downloaded audio is cached in")
    try:
        port = int(env("PORT", "8765"))
        assert 0 < port < 65536
    except (ValueError, AssertionError):
        raise SystemExit(f"PORT must be a number from 1 to 65535, got {env('PORT')}") from None
    level = env("LOG_LEVEL", "INFO").upper()
    if not isinstance(logging.getLevelName(level), int):
        raise SystemExit(f"LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR, got {level}")
    key = env("GEMINI_API_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else None)
    return Settings(db, store, Path(lib).expanduser().resolve() if lib else None,
                    env("GEMINI_MODEL", "gemini-3.5-flash-lite"), key, env("HOST", "127.0.0.1"), port, level)
