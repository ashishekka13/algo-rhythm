"""Audio and stems live in object storage (STORAGE_URL: gs://bucket/prefix, or file:///abs/path in dev); the audio
code reads local files, so local_path() fetches a file into LIBRARY_DIR the first time it is asked for."""
import functools
import os
import shutil
import tempfile
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
    return p


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
