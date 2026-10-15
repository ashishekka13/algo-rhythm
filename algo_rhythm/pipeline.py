"""Cloud Run Jobs that build the library in the database from the original audio in the bucket. Nothing heavy runs on
a laptop; the web server only reads what these write.

    python -m algo_rhythm.pipeline songs      every original not yet in the library: browser audio, Demucs vocals,
                                              analysis, features -> songs, song_analysis, song_features + the bucket
    python -m algo_rhythm.pipeline details    Gemini song details (song_ai, display names) and search vectors
    python -m algo_rhythm.pipeline art [--limit N]   one image per song (costs about $0.04 each; see algo_rhythm.art)
    python -m algo_rhythm.pipeline trivia      questions for songs without them: our data and Gemini's facts

Environment: DATABASE_URL (Cloud SQL), SOURCE_URL (gs://bucket/prefix of the originals: any folders of .mp3, .wav,
.flac, .ogg), STORAGE_URL (gs://bucket/prefix of the library the browser plays), GEMINI_API_KEY or the service account (details, art).
`songs` splits the work over the job's tasks (CLOUD_RUN_TASK_INDEX of CLOUD_RUN_TASK_COUNT): run it with
--tasks 10 and ten machines take a tenth each. It is safe to run again: a song already analysed is skipped.

Per song: the original is decoded and written again as the canonical MP3 (algo_rhythm.media: the browser and the
server then hear the same timing), its vocals are separated (Demucs htdemucs on the CPU) and encoded the same way,
then it is analysed (tagger) and measured (rhythm feel, beat-grid slip, sung segments), imported and uploaded. Local
files go as soon as the song is uploaded: a Cloud Run task's disk is its memory.
"""
import argparse
import hashlib
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from algo_rhythm import config

log = logging.getLogger("algo_rhythm.pipeline")
EXTS = (".mp3", ".wav", ".flac", ".ogg")


def _bucket_prefix(url):
    bucket, _, prefix = url[len("gs://"):].partition("/")
    return bucket, prefix.rstrip("/")


def originals(source_url):
    """[(relative path, blob or local Path)] of every original under SOURCE_URL (gs:// or file://), sorted."""
    if source_url.startswith("file://"):
        root = Path(source_url[len("file://"):])
        return sorted((p.relative_to(root).as_posix(), p) for p in root.rglob("*")
                      if p.suffix.lower() in EXTS and not any(x.startswith((".", "_")) for x in p.relative_to(root).parts))
    from google.cloud import storage as gcs
    bucket, prefix = _bucket_prefix(source_url)
    out = []
    for b in gcs.Client().list_blobs(bucket, prefix=prefix + "/" if prefix else None):
        rel = b.name[len(prefix) + 1:] if prefix else b.name
        if rel.lower().endswith(EXTS) and not any(x.startswith((".", "_")) for x in rel.split("/")):
            out.append((rel, b))
    return sorted(out, key=lambda x: x[0])


def done_files():
    """Library files already analysed with the current tagger (skipped on a rerun)."""
    from sqlalchemy import select
    from algo_rhythm import db, repository, tagger
    with repository.session() as s:
        return set(s.scalars(select(db.Song.file).join(db.SongAnalysis).where(
            db.SongAnalysis.schema_version == tagger.SCHEMA_VERSION, db.Song.status == "active")))


def canonical(src_path, out_path):
    """Decode an original and write it as the library's canonical MP3 (44.1 kHz stereo, media.encode)."""
    import librosa
    import numpy as np
    import soundfile as sf
    from algo_rhythm import media
    try:
        audio, sr = sf.read(src_path, always_2d=True, dtype="float32")
    except Exception:  # a format libsndfile cannot read: librosa falls back to audioread/ffmpeg
        y, sr = librosa.load(src_path, sr=None, mono=False)
        audio = np.atleast_2d(y).T
    if sr != media.SR:
        audio = librosa.resample(audio.T, orig_sr=sr, target_sr=media.SR).T
    media.encode(audio, media.SR, out_path)


def one_song(rel_src, blob, model, device, work):
    """Everything for one original, in its own temp library folder; returns the library file name."""
    from algo_rhythm import dj, importer, media, repository, stems, tagger
    lib = Path(tempfile.mkdtemp(dir=work))
    try:
        rel = str(Path(rel_src).with_suffix(".mp3"))
        src = lib / "_src" / Path(rel_src).name
        src.parent.mkdir(parents=True)
        if isinstance(blob, Path):
            shutil.copyfile(blob, src)
        else:
            blob.download_to_filename(src)
        canonical(src, lib / rel)
        src.unlink()
        key = tagger.tag_key(rel)
        digest = hashlib.sha256((lib / rel).read_bytes()).hexdigest()
        (lib / "stems").mkdir()
        stems.separate_to(model, device, lib / rel, lib / media.stem_rel(key), lib / "stems" / f"{key}.json", digest)
        (lib / "tags").mkdir()
        status, _, note = tagger.tag_one(lib, rel, True, False)
        if status != "tagged":
            raise RuntimeError(f"tagging failed: {note}")
        dj.load_library(lib)  # measures rhythm feel, beat-grid slip and sung segments into LIBRARY/_mixes
        with repository.session() as s:
            stats = importer.import_library(s, lib, upload=True)
        if not stats["songs"]:
            raise RuntimeError("not imported (a duplicate of a song already in the library?)")
        return rel
    finally:
        shutil.rmtree(lib, ignore_errors=True)


def run_songs(limit=None):
    cfg = config.settings()
    source = os.environ.get("SOURCE_URL")
    if not source or not cfg.database_url or not cfg.storage_url:
        sys.exit("songs needs SOURCE_URL, DATABASE_URL and STORAGE_URL")
    task, tasks = int(os.environ.get("CLOUD_RUN_TASK_INDEX", 0)), int(os.environ.get("CLOUD_RUN_TASK_COUNT", 1))
    have = done_files()
    # share out the whole list, then skip what is done: shares made after skipping would shift as other tasks finish
    share = originals(source)[task::tasks]
    mine = [(r, b) for r, b in share if str(Path(r).with_suffix(".mp3")) not in have][:limit]
    log.info("task %d of %d: %d songs are mine, %d of them still to do", task + 1, tasks, len(share), len(mine))
    if not mine:
        return 0
    from algo_rhythm import stems
    model, device = stems.load_model()
    work = Path(tempfile.mkdtemp())
    failed = 0
    for k, (rel, blob) in enumerate(mine, 1):
        t0 = time.time()
        try:
            name = one_song(rel, blob, model, device, work)
            log.info("[%d/%d] %.0f s  %s", k, len(mine), time.time() - t0, name)
        except Exception as e:  # one bad file must not stop the rest; a rerun retries it
            failed += 1
            log.exception("[%d/%d] FAILED %s: %s", k, len(mine), rel, e)
    log.info("task %d done: %d songs, %d failed", task + 1, len(mine) - failed, failed)
    return 1 if failed else 0


def run_details():
    """Gemini's song details for every song without them, then a search vector for every song without one."""
    from algo_rhythm import enrich, repository, search
    done, total = enrich.run_db()
    log.info("song details: %d of %d saved", done, total)
    root = Path(tempfile.mkdtemp())
    songs, _ = repository.load_songs(root)
    n = search.build(root, songs)
    log.info("search vectors: %d new", n)
    return 0 if done == total else 1


def run_art(limit=None):
    """One image per song without one; each is also noted in the assets table so servers reload the art index."""
    from algo_rhythm import art, db, repository
    root = Path(config.settings().library_dir or tempfile.mkdtemp())
    (root / "art" / "index.json").unlink(missing_ok=True)  # read the bucket's index, not a stale local copy
    songs, _ = repository.load_songs(root)
    index = art.load_index(root)
    todo = [s for s in songs if art.art_id(s) not in index][:limit]
    log.info("art: %d songs, %d with art, %d to make (about $%.2f)", len(songs), len(index), len(todo),
             len(todo) * art.PRICE_PER_IMAGE)
    made, failed = art.run(root, todo)
    with repository.session() as s:
        for sid, name in art.load_index(root).items():
            s.merge(db.Asset(key=f"art-{sid}", kind="art", path=f"art/{name}", seconds=0.0))
        s.commit()
    return 0 if not failed else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=("songs", "details", "art", "trivia"))
    ap.add_argument("--limit", type=int, help="at most this many songs (per task, for songs)")
    args = ap.parse_args(argv)
    from algo_rhythm.server import setup_logging
    setup_logging(config.settings().log_level)
    if args.step == "songs":
        return run_songs(args.limit)
    if args.step == "details":
        return run_details()
    if args.step == "trivia":  # questions for every song without them (algo_rhythm.trivia)
        from algo_rhythm import repository, trivia
        log.info("trivia: %d questions stored", trivia.run(args.limit, on_call=repository.log_ai_call))
        return 0
    return run_art(args.limit)


if __name__ == "__main__":
    sys.exit(main())
