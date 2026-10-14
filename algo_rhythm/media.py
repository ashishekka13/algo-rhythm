"""Audio files the browser plays: one encoding for every song and every vocal stem, so they all decode with the same
offset everywhere (the server's analysis, Chrome, Safari, Firefox). Only numpy and soundfile: the stems environment
imports this too.

    media.encode(audio, sr, path)        write the canonical MP3 (44.1 kHz stereo, constant ~192 kbps)
    media.stem_path(library, key)        LIBRARY/stems/<key>.vocals.mp3, or the older .vocals.flac when only that exists

Why one encoding: an MP3 decoder may or may not trim the encoder's start delay (about 25 ms), and an MP3 and a FLAC
of the same audio can then disagree by that much. Beat matching compares two songs and the vocal swap subtracts a
stem from its song, so what matters is that every file is shifted the same way. Encoding the song and its vocals
with the same encoder and settings guarantees that, whatever the browser does.
"""
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
FORMAT, COMPRESSION, BITRATE_MODE = "MP3", 0.4, "CONSTANT"  # libsndfile's LAME: 0.4 = about 192 kbps
STEM_EXTS = ("vocals.mp3", "vocals.flac")  # the first that exists wins; FLAC stems are from before the web player


def encode(audio, sr, path):
    """Write `audio` (frames x channels, or mono) as the canonical MP3 at `path`. Resampling is the caller's job:
    every library file is SR (librosa.resample before calling when it is not)."""
    if sr != SR:
        raise ValueError(f"media files are {SR} Hz, got {sr}")
    a = np.asarray(audio, dtype=np.float32)
    if a.ndim == 1:
        a = a[:, None]
    if a.shape[1] == 1:
        a = np.repeat(a, 2, axis=1)
    a = np.clip(a[:, :2], -1.0, 1.0)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.mp3")
    sf.write(str(tmp), a, SR, format=FORMAT, compression_level=COMPRESSION, bitrate_mode=BITRATE_MODE)
    tmp.replace(path)
    return path


def stem_rel(key, ext=STEM_EXTS[0]):
    return f"stems/{key}.{ext}"


def stem_path(library, key, local_path=None):
    """The song's vocal stem: .vocals.mp3 first, else the older .vocals.flac. `local_path(rel, library)` fetches from
    object storage (algo_rhythm.storage.local_path); default: only what is on disk. Returns the path of the first that
    exists, else the .mp3 path (callers check exists())."""
    get = local_path or (lambda rel, lib: Path(lib) / rel)
    first = None
    for ext in STEM_EXTS:  # one at a time: each miss may be a round trip to the bucket
        p = Path(get(stem_rel(key, ext), library))
        if p.exists():
            return p
        first = first or p
    return first
