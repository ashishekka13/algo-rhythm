"""Audio and stems live in object storage (STORAGE_URL: gs://bucket/prefix, or file:///abs/path in dev); the audio
code reads local files, so local_path() fetches a file into LIBRARY_DIR the first time it is asked for.

With STORAGE_URL that folder is only a cache, and on Cloud Run a disk is the instance's memory: files the server made
are deleted once uploaded (forget()), and audio and images fetched from the bucket are kept up to LOCAL_CACHE_MB,
least recently used out first. Without STORAGE_URL the folder IS the library: nothing is ever deleted."""
import functools
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from algo_rhythm.config import settings


@functools.cache
def _bucket(name):
    from google.cloud import storage  # imported only when gs:// is used
    return storage.Client().bucket(name)


def _blob(rel):
    bucket, _, prefix = settings().storage_url[len("gs://"):].partition("/")
    return _bucket(bucket).blob(f"{prefix.rstrip('/')}/{rel}" if prefix else rel)


def local_path(rel, root=None):
    """LIBRARY_DIR/rel (or root/rel), downloaded from STORAGE_URL first if it is not there yet. Returns the path even
    when the file is in neither place, so callers keep their own exists() checks. No STORAGE_URL: just the path."""
    p = Path(root or settings().library_dir) / rel
    url = settings().storage_url
    if p.exists() or not url:
        if url and p.exists():
            _touch(p)  # recently used: the cache keeps it
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".part")  # two threads may fetch the same file: never half-read
    os.close(fd)
    try:
        if url.startswith("file://"):
            src = Path(url[len("file://"):]) / rel
            if not src.exists():
                return p
            shutil.copyfile(src, tmp)
        else:
            from google.api_core.exceptions import NotFound
            try:
                _blob(rel).download_to_filename(tmp)
            except NotFound:
                return p
        os.replace(tmp, p)
    finally:
        Path(tmp).unlink(missing_ok=True)
    trim(Path(root or settings().library_dir))
    return p


# ---------------------------------------------------------------- the local cache (STORAGE_URL only)

log = logging.getLogger("algo_rhythm.storage")
CACHED = {".mp3", ".flac", ".wav", ".ogg", ".jpg", ".jpeg", ".png", ".webp"}  # can always be fetched again
_trim_lock = threading.Lock()


def _touch(p):
    try:
        os.utime(p)
    except OSError:
        pass


def trim(root, limit_mb=None):
    """Keep the audio and images under root within LOCAL_CACHE_MB (default 1000): least recently used out first.
    Only when STORAGE_URL is set (they come back from the bucket when asked for again). Returns the MB freed."""
    if not settings().storage_url:
        return 0
    limit = (limit_mb if limit_mb is not None else int(os.environ.get("LOCAL_CACHE_MB", "1000"))) * 1_000_000
    with _trim_lock:
        files = []
        for p in Path(root).rglob("*"):
            if p.suffix.lower() in CACHED and p.is_file():
                st = p.stat()
                files.append((st.st_mtime, st.st_size, p))
        total = sum(f[1] for f in files)
        if total <= limit:
            return 0
        freed = 0
        for _, size, p in sorted(files, key=lambda f: f[0]):
            if total - freed <= limit * 0.8:  # down to 80%: not a trim on every fetch once full
                break
            try:
                p.unlink()  # a reader that has it open keeps reading (the file goes when it is closed)
                freed += size
            except OSError:
                pass
        log.info("local cache trimmed: %d MB freed, %d MB kept", freed // 1_000_000, (total - freed) // 1_000_000)
        return freed // 1_000_000


def forget(local):
    """A file the server made and uploaded: the local copy goes (fetched back from the bucket if ever needed)."""
    if settings().storage_url:
        Path(local).unlink(missing_ok=True)


def upload(local, rel):
    """Copy a local file to STORAGE_URL/rel; skipped when a file of the same size is already there."""
    local, url = Path(local), settings().storage_url
    if not url:
        raise SystemExit("STORAGE_URL is not set: nowhere to upload to")
    if url.startswith("file://"):
        dst = Path(url[len("file://"):]) / rel
        if not (dst.exists() and dst.stat().st_size == local.stat().st_size):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local, dst)
        return
    blob = _blob(rel)
    if blob.exists():
        blob.reload()
        if blob.size == local.stat().st_size:
            return
    blob.upload_from_filename(local)
