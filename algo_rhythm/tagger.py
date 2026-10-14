"""Tag audio files with everything the mixer needs. Output contract: TAGGING_CONTRACT.md.

Usage:
  python tagger.py LIBRARY_DIR                 tag every audio file under LIBRARY_DIR
  python tagger.py LIBRARY_DIR --jobs 6        ... with 6 parallel workers
  python tagger.py LIBRARY_DIR --files a.mp3 b.mp3   tag only these (paths relative to LIBRARY_DIR)
  python tagger.py LIBRARY_DIR --force         re-tag even if tags are up to date
  python tagger.py LIBRARY_DIR -v              print a per-song summary

Writes LIBRARY_DIR/tags/<key>.json per song (key = relative path, "/" -> "__", no
extension) and LIBRARY_DIR/tags/_errors/<key>.json for songs that failed.
Deterministic: the same file always produces the same JSON.
"""
import argparse
import hashlib
import json
import os
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import librosa
import numpy as np
import pyloudnorm as pyln
import soundfile as sf

SCHEMA_VERSION = "2.5"  # 2.1 vibe block; 2.2 phrase_mix; 2.3 vocal_activity + phrase_mix.vocal; 2.4 low_db; 2.5 vocal_source
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg"}
SR = 22050
HOP = 512
PHRASE_BEATS = 16
HOOK_SECONDS = 45  # length of the precomputed hook windows; the mixer can pick its own

PITCHES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Krumhansl-Kessler key profiles
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
CAMELOT = {
    "major": dict(zip(["B", "F#", "C#", "G#", "D#", "A#", "F", "C", "G", "D", "A", "E"],
                      [f"{i}B" for i in range(1, 13)])),
    "minor": dict(zip(["G#", "D#", "A#", "F", "C", "G", "D", "A", "E", "B", "F#", "C#"],
                      [f"{i}A" for i in range(1, 13)])),
}


def clip01(x):
    return float(np.clip(x, 0, 1))


def rounded(xs, nd):
    return [round(float(x), nd) for x in xs]


# ---------------------------------------------------------------- analysis

def consensus_bpm(oenv, kick_env):
    """One tempo both trackers start from. Each band's autocorrelation votes for a
    pulse; summing them and weighting by a log-normal prior around 110 BPM picks the
    pulse both bands support, instead of letting two trackers drift to a 3:2 or 4:3
    subdivision of the same groove (common in dotted / triplet-feel ballads)."""
    fps = SR / HOP
    lags = np.arange(int(fps * 60 / 200), int(fps * 60 / 60))  # 60-200 BPM
    votes = np.zeros(len(lags))
    for env in (oenv, kick_env):
        ac = librosa.autocorrelate(env - env.mean())
        votes += ac[lags] / (ac[0] + 1e-9)
    bpms = 60 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpms / 110) / 0.6) ** 2)
    return float(bpms[int(np.argmax(votes * prior))])


def tempo_tags(y, oenv):
    fine = 128
    low = librosa.onset.onset_strength(y=y, sr=SR, hop_length=fine, n_mels=16, fmax=150)
    seed = consensus_bpm(oenv, low[:: HOP // fine][: len(oenv)])
    _, beat_frames = librosa.beat.beat_track(onset_envelope=oenv, sr=SR, hop_length=HOP, start_bpm=seed)
    beats = librosa.frames_to_time(beat_frames, sr=SR, hop_length=HOP)
    # Fit a line through beat times: far more precise than the tempogram's bin.
    period = np.polyfit(np.arange(len(beats)), beats, 1)[0]
    bpm = 60 / period
    median_bpm = 60 / float(np.median(np.diff(beats)))
    if abs(median_bpm / bpm - 1) > 0.02:  # tempo drifts: the line fit and the grid disagree
        bpm = median_bpm                  # contract: bpm must match the grid's median spacing

    # librosa can lock onto off-beat hats, and lags onsets by a frame or two.
    # Kicks land on the beat: slide the grid across one period at ~6 ms
    # resolution and keep the offset where low-band onsets line up best.
    step = fine / SR
    offsets = np.arange(-period / 2, period / 2, step)
    scores = [low[np.clip(np.round((beats + d) / step).astype(int), 0, len(low) - 1)].sum()
              for d in offsets]
    shift = offsets[int(np.argmax(scores))]
    beats = beats + shift
    beats = beats[beats >= 0]
    beat_frames = librosa.time_to_frames(beats, sr=SR, hop_length=HOP)

    # Pulse clarity: how strongly onsets repeat at exactly this period.
    ac = librosa.autocorrelate(oenv - oenv.mean())
    lag = int(round(period * SR / HOP))
    clarity = ac[lag - 1 : lag + 2].max() / ac[0]

    beat_strength = oenv[beat_frames].mean() / oenv.mean()

    # Second opinion from the kick band. Neither estimate is reliably better, so a
    # non-octave disagreement is flagged for review rather than resolved here.
    _, kick_frames = librosa.beat.beat_track(onset_envelope=low[::HOP // fine], sr=SR, hop_length=HOP,
                                         start_bpm=seed)
    kick_beats = librosa.frames_to_time(kick_frames, sr=SR, hop_length=HOP)
    kick_bpm = 60 / np.polyfit(np.arange(len(kick_beats)), kick_beats, 1)[0]
    return beats, {
        "bpm": round(float(bpm), 2),
        "confidence": round(clip01(clarity), 3),
        "beat_count": int(len(beats)),
        "beat_strength": round(float(beat_strength), 3),
        "grid_shift_ms": round(float(shift * 1000), 1),
        "half_double": [round(float(bpm / 2), 2), round(float(bpm * 2), 2)],
        "kick_band_bpm": round(float(kick_bpm), 2),
    }


def key_tags(chroma_frames):
    chroma = chroma_frames.mean(axis=1)
    scores = []
    for mode, profile in (("major", MAJOR), ("minor", MINOR)):
        for tonic in range(12):
            r = np.corrcoef(chroma, np.roll(profile, tonic))[0, 1]
            scores.append((r, PITCHES[tonic], mode))
    scores.sort(reverse=True)
    (r1, tonic, mode), (r2, tonic2, mode2) = scores[0], scores[1]
    return chroma, {
        "name": f"{tonic} {mode}",
        "camelot": CAMELOT[mode][tonic],
        "confidence": round(float(r1), 3),
        "margin": round(float(r1 - r2), 3),
        "runner_up": f"{tonic2} {mode2} ({CAMELOT[mode2][tonic2]})",
    }


def loudness_tags(path):
    audio, native_sr = sf.read(path, always_2d=True)
    lufs = pyln.Meter(native_sr).integrated_loudness(audio)
    peak = 20 * np.log10(np.abs(audio).max() + 1e-12)
    return {"lufs": round(float(lufs), 2), "peak_dbfs": round(float(peak), 2)}


def beat_loudness(rms, beat_frames):
    """Mean RMS per beat. Skips each beat's last 3 frames: the ~93 ms RMS window
    (plus the grid's ~23 ms lag) lets the next beat's kick bleed in."""
    return np.array([rms[a : max(a + 1, b - 3)].mean()
                     for a, b in zip(beat_frames[:-1], beat_frames[1:])])


def structure_tags(per_beat, beats):
    """Intro/outro boundaries from per-beat loudness.

    The intro ends where the track gets loud and stays loud for a full phrase.
    That beat is the best downbeat guess we have without a downbeat tracker,
    so the 16-beat phrase grid is anchored there.
    """
    threshold = 0.5 * np.percentile(per_beat, 75)
    ahead = np.array([per_beat[i : i + 16].mean() for i in range(len(per_beat))])
    behind = np.array([per_beat[max(0, i - 15) : i + 1].mean() for i in range(len(per_beat))])
    loud = per_beat >= threshold
    first = int(np.argmax(loud & (ahead >= threshold)))
    last = len(per_beat) - 1 - int(np.argmax((loud & (behind >= threshold))[::-1]))
    return first % PHRASE_BEATS, {
        "intro_end_s": round(float(beats[first]), 2),
        "outro_start_s": round(float(beats[min(last + 1, len(beats) - 1)]), 2),
        "phrase_anchor_s": round(float(beats[first % PHRASE_BEATS]), 2),
    }


def phrase_tags(per_beat, chroma_frames, beat_frames, anchor):
    """Score every 16-beat phrase for hook-ness: loud (relative to the song) and
    repeated elsewhere in the song (choruses repeat; verses and bridges mostly don't)."""
    bounds = list(zip(beat_frames[:-1], beat_frames[1:]))
    beat_chroma = np.array([np.median(chroma_frames[:, a:b], axis=1) for a, b in bounds])
    beat_chroma /= np.linalg.norm(beat_chroma, axis=1, keepdims=True) + 1e-9

    starts = list(range(anchor, len(bounds) - PHRASE_BEATS + 1, PHRASE_BEATS))
    energy = np.array([per_beat[p : p + PHRASE_BEATS].mean() for p in starts])
    energy = energy / energy.max()
    blocks = np.array([beat_chroma[p : p + PHRASE_BEATS].ravel() for p in starts])
    blocks /= np.linalg.norm(blocks, axis=1, keepdims=True) + 1e-9
    sim = blocks @ blocks.T
    np.fill_diagonal(sim, -1)  # best match with any *other* phrase
    repetition = np.clip(sim.max(axis=1), 0, 1) if len(starts) > 1 else np.zeros(len(starts))
    score = 0.5 * energy + 0.5 * repetition
    return {
        "start_beats": [int(p) for p in starts],
        "energy": rounded(energy, 3),
        "repetition": rounded(repetition, 3),
        "score": rounded(score, 3),
    }


def best_windows(phrases, beats, target_s, k=3, min_phrases=4, exclude=()):
    """Top-k non-overlapping excerpt windows of ~target_s, in whole 32-beat blocks.

    `exclude` holds (start_beat, end_beat) ranges a window must not overlap.
    Shared with the mixer, which calls it with its own target length.
    """
    starts, scores = phrases["start_beats"], np.array(phrases["score"])
    if len(starts) < min_phrases:
        return []
    period = float(np.median(np.diff(beats)))
    n = 2 * max(1, round(target_s / (2 * PHRASE_BEATS * period)))
    n = int(min(max(n, min_phrases), len(starts)))

    def end_s(end_beat):
        return beats[end_beat] if end_beat < len(beats) else beats[-1] + period

    ranked = sorted(range(len(starts) - n + 1),
                    key=lambda i: (-scores[i : i + n].mean(), i))  # deterministic ties
    picked, taken = [], list(exclude)
    for i in ranked:
        s_beat, e_beat = starts[i], starts[i] + PHRASE_BEATS * n
        if any(s_beat < b and a < e_beat for a, b in taken):
            continue
        picked.append({"start_beat": s_beat, "end_beat": e_beat,
                       "start_s": round(float(beats[s_beat]), 3),
                       "end_s": round(float(end_s(e_beat)), 3),
                       "score": round(float(scores[i : i + n].mean()), 3)})
        taken.append((s_beat, e_beat))
        if len(picked) == k:
            break
    return picked


def read_metadata(path):
    """Title/artist from ID3 (or other) tags; falls back to 'Artist - Title' filenames."""
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        tags = f.tags if f is not None and f.tags is not None else {}
        title, artist = (tags.get("title") or [None])[0], (tags.get("artist") or [None])[0]
        if title:
            return title.strip(), (artist.strip() if artist else None), "file_tags"
    except Exception:
        pass
    stem = Path(path).stem
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return title.strip(), artist.strip(), "filename"
    return stem, None, "filename"


def quality_flags(t, n_phrases):
    flags = []
    if not 90 <= t["duration_s"] <= 720:
        flags.append("duration_out_of_range")
    if t["tempo"]["confidence"] < 0.3:
        flags.append("low_tempo_confidence")
    bpm, kick = t["tempo"]["bpm"], t["tempo"]["kick_band_bpm"]
    if min(abs(kick * r - bpm) for r in (0.5, 1, 2)) / bpm > 0.03:
        flags.append("tempo_disagreement")
    if bpm > 150 or bpm < 75:
        flags.append("tempo_octave_ambiguous")
    if t["key"]["margin"] < 0.05:
        flags.append("low_key_margin")
    if n_phrases < 4:
        flags.append("too_few_phrases")
    if t["structure"]["intro_end_s"] > 30:
        flags.append("long_intro")
    if t["loudness"]["peak_dbfs"] > 0:
        flags.append("clipped_source")
    return flags


def felt_bpm(bpm):
    """Fold a tempo into 70-140: how a person feels the pulse, whatever octave the tracker chose."""
    while bpm > 140:
        bpm /= 2
    while bpm < 70:
        bpm *= 2
    return bpm


def window_ease(per_beat, y_harm, y_perc, beats, first, n=32):
    """0..1: how easy a 32-beat window is to mix over. Steady loudness (no break or
    fade inside it) and drums present (something to beat-match against)."""
    seg = per_beat[first : first + n]
    if len(seg) < 8:
        return 0.0
    stable = 1 - clip01(float(seg.std() / (seg.mean() + 1e-9)) * 2)
    a, b = int(beats[first] * SR), int(beats[min(first + n, len(beats) - 1)] * SR)
    h, p = (y_harm[a:b] ** 2).sum(), (y_perc[a:b] ** 2).sum()
    return 0.6 * stable + 0.4 * clip01(float(p / (h + p + 1e-9)) / 0.4)


VOCAL_HOP, VOCAL_SCALE = 1024, 0.3
VOCAL_FLOOR_DB = (-55, -45)  # a vocal-stem frame under -55 dBFS is not singing; full weight from -45 dBFS


def vocal_frames(y):
    """Per-frame 'is something sung or soloed here', 0..1. A PROXY, not a voice detector: it measures how much
    of the 0-4 kHz sound does not repeat from the surrounding 2 s (backing tracks loop, voices don't),
    the repeating-background idea from Rafii & Pardo. The contract's `vocal_ratio` stays null (needs Demucs)."""
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=VOCAL_HOP))[: int(4000 / (SR / 2048))]
    background = librosa.decompose.nn_filter(S, aggregate=np.median, metric="cosine",
                                             width=int(librosa.time_to_frames(2, sr=SR, hop_length=VOCAL_HOP)))
    resid = np.clip(S - np.minimum(S, background), 0, None)
    return (resid ** 2).sum(0) / ((S ** 2).sum(0) + 1e-9)


def vocal_frames_from_stem(vocals_path):
    """Per-frame 'is someone singing', 0..1, from the real vocals track (Demucs). Hindi film songs are vocal-forward,
    so 'vocals as a share of the mix' is high almost everywhere and says little. What matters is when the singing
    STOPS: so each frame is judged against the song's own loud-singing level (its 90th-percentile frame):
    at that level or within 8 dB of it = singing (1.0), 24 dB below it or more = silent (0.0).
    And against an absolute floor (VOCAL_FLOOR_DB): an instrumental's stem is only separation noise, and judged
    against itself alone that noise read as confident singing. Real singing never comes near the floor (loud-singing
    levels measured -21 to -10 dBFS on 20 library songs, so their silent point is at -45 dBFS or above)."""
    v, _ = librosa.load(vocals_path, sr=SR, mono=True)
    db = 20 * np.log10(librosa.feature.rms(y=v, frame_length=2048, hop_length=VOCAL_HOP)[0] + 1e-9)
    rel = db - np.percentile(db, 90)
    lo, hi = VOCAL_FLOOR_DB
    sung = np.minimum(np.clip((rel + 24) / 16, 0, 1), np.clip((db - lo) / (hi - lo), 0, 1))
    return sung * VOCAL_SCALE  # scaled so the shared /VOCAL_SCALE below gives 0..1


def phrase_mix_tags(per_beat, y_harm, y_perc, beats, starts, vocal):
    """Per 16-beat phrase: loudness (dB, songs are normalized to -14 LUFS so it is comparable
    across songs), drum presence, and steadiness. Lets the mixer judge a window's edges, not just its hook score."""
    rms_db, perc, steady, voc, low = [], [], [], [], []
    for s in starts:
        seg = per_beat[s : s + PHRASE_BEATS]
        a, b = int(beats[s] * SR), int(beats[min(s + PHRASE_BEATS, len(beats) - 1)] * SR)
        h, p = (y_harm[a:b] ** 2).sum(), (y_perc[a:b] ** 2).sum()
        rms_db.append(round(float(20 * np.log10(seg.mean() + 1e-9)), 2))
        low.append(round(float(20 * np.log10(np.percentile(seg, 10) + 1e-9)), 2))  # quietest beats: a stop or break inside the phrase
        perc.append(round(clip01(float(p / (h + p + 1e-9)) / 0.4), 3))
        steady.append(round(1 - clip01(float(seg.std() / (seg.mean() + 1e-9)) * 2), 3))
        fa, fb = int(beats[s] * SR / VOCAL_HOP), int(beats[min(s + PHRASE_BEATS, len(beats) - 1)] * SR / VOCAL_HOP)
        voc.append(round(clip01(float(vocal[fa : max(fb, fa + 1)].mean()) / VOCAL_SCALE), 3))
    return {"rms_db": rms_db, "perc": perc, "steady": steady, "vocal": voc, "low_db": low}


def vibe_tags(key, tempo, energy, perc_ratio, centroid, per_beat, phrases, structure,
              beats, y_harm, y_perc, vocal, vocal_source):
    """Spotify-style audio descriptors, all 0..1, from DSP only (deterministic, no LLM).
    Heuristics with a known ceiling: valence especially is a proxy (mode + brightness +
    tempo), not a feeling. The mixer turns each into a rank within the library."""
    felt = felt_bpm(tempo["bpm"])
    bright = clip01(np.log2(centroid / 500) / np.log2(10))
    mode = 1.0 if key["camelot"].endswith("B") else 0.0
    mode_w = clip01(key["margin"] / 0.1)  # an uncertain key says little about mood
    valence = (0.45 * (0.5 + (mode - 0.5) * mode_w) + 0.30 * bright
               + 0.25 * clip01((felt - 70) / 70))
    iv = np.diff(beats)
    regularity = 1 - clip01(float(iv.std() / iv.mean()) * 5)
    danceability = (0.35 * regularity + 0.30 * clip01(tempo["confidence"] / 0.6)
                    + 0.20 * float(np.exp(-0.5 * ((felt - 118) / 20) ** 2))
                    + 0.15 * clip01(perc_ratio / 0.4))
    acousticness = 0.6 * (1 - clip01(perc_ratio / 0.5)) + 0.4 * (1 - bright)

    pe = np.array(phrases["energy"])
    jumps = np.diff(pe) if len(pe) > 1 else np.zeros(1)
    first_i = int(np.searchsorted(beats, structure["phrase_anchor_s"]))
    last_i = int(np.searchsorted(beats, structure["outro_start_s"]))
    return {
        "arousal": energy,
        "valence": round(clip01(valence), 3),
        "danceability": round(clip01(danceability), 3),
        "acousticness": round(clip01(acousticness), 3),
        "felt_bpm": round(float(felt), 1),
        "dynamics": {
            "peak_position": round(float(np.argmax(pe) / max(1, len(pe) - 1)), 3) if len(pe) else 0.0,
            "drop_contrast": round(clip01(float(jumps.max())), 3),
            "range": round(clip01(float(np.percentile(per_beat, 90) - np.percentile(per_beat, 10))
                                  / (per_beat.max() + 1e-9)), 3),
        },
        "vocal_activity": round(clip01(float(vocal.mean()) / VOCAL_SCALE), 3),
        "vocal_source": vocal_source,  # "demucs" = real vocals track, "proxy" = the repeating-background guess
        "phrase_mix": phrase_mix_tags(per_beat, y_harm, y_perc, beats, phrases["start_beats"], vocal),
        "mix_in_ease": round(clip01(window_ease(per_beat, y_harm, y_perc, beats, first_i)), 3),
        "mix_out_ease": round(clip01(window_ease(per_beat, y_harm, y_perc, beats,
                                                 max(0, min(last_i, len(per_beat)) - 32))), 3),
    }


def tag(path, rel_path=None, vocals_path=None):
    loudness = loudness_tags(path)
    y, _ = librosa.load(path, sr=SR, mono=True)
    # Analyze every song at the same loudness, so features describe the music
    # rather than how hot it was mastered.
    y = y * 10 ** ((-14 - loudness["lufs"]) / 20)
    y_harm, y_perc = librosa.effects.hpss(y)
    oenv = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP)
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    chroma_frames = librosa.feature.chroma_cqt(y=y_harm, sr=SR, hop_length=HOP)

    beats, tempo = tempo_tags(y, oenv)
    beat_frames = librosa.time_to_frames(beats, sr=SR, hop_length=HOP)
    per_beat = beat_loudness(rms, beat_frames)
    chroma, key = key_tags(chroma_frames)
    anchor, structure = structure_tags(per_beat, beats)
    phrases = phrase_tags(per_beat, chroma_frames, beat_frames, anchor)

    # Measure energy over the body of the song, skipping the intro/outro and
    # any non-music skit an official-video rip opens with.
    duration = len(y) / SR
    a, b = librosa.time_to_frames([structure["intro_end_s"], structure["outro_start_s"]],
                                  sr=SR, hop_length=HOP)
    if (b - a) * HOP / SR < 30:
        a, b = 0, len(rms)
    body = slice(a * HOP, b * HOP)
    body_s = (b - a) * HOP / SR
    centroid = float(librosa.feature.spectral_centroid(y=y[body], sr=SR, hop_length=HOP).mean())
    onsets = librosa.onset.onset_detect(onset_envelope=oenv[a:b], sr=SR, hop_length=HOP)
    onset_rate = len(onsets) / body_s
    harm, perc = (y_harm[body] ** 2).sum(), (y_perc[body] ** 2).sum()
    perc_ratio = float(perc / (harm + perc))

    # Absolute 0..1 energy; the mixer turns it into a percentile within the library.
    # Loudness is deliberately excluded: it's a property of the master, and playback
    # normalizes it away anyway.
    energy = (0.35 * clip01(np.log2(centroid / 500) / np.log2(10))
              + 0.35 * clip01(onset_rate / 8)
              + 0.30 * clip01(perc_ratio / 0.4))

    mfcc = librosa.feature.mfcc(y=y, sr=SR, n_mfcc=13, hop_length=HOP)
    contrast = librosa.feature.spectral_contrast(y=y, sr=SR, hop_length=HOP)
    title, artist, meta_source = read_metadata(path)

    t = {
        "schema_version": SCHEMA_VERSION,
        "file": rel_path or Path(path).name,
        "sha256": sha256(path),
        "title": title,
        "artist": artist,
        "metadata_source": meta_source,
        "duration_s": round(duration, 2),
        "tempo": tempo,
        "beats": rounded(beats, 3),
        "key": key,
        "loudness": loudness,
        "energy": {
            "score": round(energy, 3),
            "brightness_hz": round(centroid, 1),
            "onset_rate": round(onset_rate, 2),
            "percussive_ratio": round(perc_ratio, 3),
            "measured_over_s": [round(a * HOP / SR, 1), round(b * HOP / SR, 1)],
        },
        "structure": structure,
        "phrases": phrases,
        "hooks": best_windows(phrases, beats, HOOK_SECONDS),
        "vibe": vibe_tags(key, tempo, round(energy, 3), perc_ratio, centroid, per_beat,
                          phrases, structure, beats, y_harm, y_perc,
                          vocal_frames_from_stem(vocals_path) if vocals_path else vocal_frames(y),
                          "demucs" if vocals_path else "proxy"),
        # Vectors for song-to-song similarity
        "vectors": {
            "chroma": rounded(chroma / chroma.sum(), 4),
            "mfcc_mean": rounded(mfcc.mean(axis=1), 3),
            "mfcc_std": rounded(mfcc.std(axis=1), 3),
            "spectral_contrast": rounded(contrast.mean(axis=1), 3),
        },
        # Not computed in v2 (need Demucs / CLAP). The mixer uses them if present.
        "vocal_ratio": None,
        "embedding": None,
    }
    t["quality"] = {"flags": quality_flags(t, len(phrases["start_beats"]))}
    return t


# ---------------------------------------------------------------- batch runner

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tag_key(rel_path):
    return str(Path(rel_path).with_suffix("")).replace(os.sep, "__")


def write_json_atomic(path, obj):
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def is_current(out, path):
    try:
        old = json.loads(out.read_text())
        return old.get("schema_version") == SCHEMA_VERSION and old.get("sha256") == sha256(path)
    except (OSError, ValueError):
        return False


def _has_real_vocals(out):
    try:
        return json.loads(out.read_text())["vibe"].get("vocal_source") == "demucs"
    except (OSError, ValueError, KeyError):
        return False


def tag_one(library, rel_path, force, verbose):
    path = library / rel_path
    out_dir, err_dir = library / "tags", library / "tags" / "_errors"
    key = tag_key(rel_path)
    out, err = out_dir / f"{key}.json", err_dir / f"{key}.json"
    from algo_rhythm import media
    vocals = media.stem_path(library, key)
    vocals = vocals if vocals.exists() else None
    if not force and is_current(out, path) and not (vocals and not _has_real_vocals(out)):
        return "skipped", rel_path, None
    try:
        t = tag(str(path), rel_path, str(vocals) if vocals else None)
    except Exception as e:
        err_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(err, {"file": rel_path, "error": f"{type(e).__name__}: {e}",
                                "traceback": traceback.format_exc()})
        return "failed", rel_path, str(e)
    write_json_atomic(out, t)
    err.unlink(missing_ok=True)
    if verbose:
        summarize(t)
    return "tagged", rel_path, ", ".join(t["quality"]["flags"]) or None


def audio_files(library):
    def wanted(p):
        parts = p.relative_to(library).parts
        return (p.suffix.lower() in AUDIO_EXTS
                and not any(x in ("tags", "stems") or x.startswith((".", "_")) for x in parts))  # stems/ holds vocals, not songs
    return sorted(str(p.relative_to(library)) for p in library.rglob("*") if wanted(p))


def summarize(t):
    tp, k, ld, e, s = t["tempo"], t["key"], t["loudness"], t["energy"], t["structure"]
    print(f"\n{t['file']}  ({t['duration_s']}s)  artist={t['artist']!r}  title={t['title']!r}")
    print(f"  tempo     {tp['bpm']} BPM   conf {tp['confidence']}   beat strength {tp['beat_strength']}")
    print(f"  key       {k['name']} ({k['camelot']})   conf {k['confidence']}   margin {k['margin']}   runner-up {k['runner_up']}")
    print(f"  loudness  {ld['lufs']} LUFS   peak {ld['peak_dbfs']} dBFS")
    print(f"  energy    {e['score']}   brightness {e['brightness_hz']} Hz   onsets/s {e['onset_rate']}   percussive {e['percussive_ratio']}")
    print(f"  structure intro ends {s['intro_end_s']}s   outro starts {s['outro_start_s']}s")
    print(f"  phrases   {' '.join(f'{x:.2f}' for x in t['phrases']['energy'])}")
    for h in t["hooks"]:
        print(f"  hook      {h['start_s']:.1f}-{h['end_s']:.1f}s   score {h['score']}")
    print(f"  flags     {', '.join(t['quality']['flags']) or 'none'}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("library", type=Path)
    ap.add_argument("--files", nargs="*", help="paths relative to LIBRARY (default: all audio files)")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    library = args.library.resolve()
    (library / "tags").mkdir(exist_ok=True)
    files = args.files or audio_files(library)
    counts = {"tagged": 0, "skipped": 0, "failed": 0}

    def report(result):
        status, rel, note = result
        counts[status] += 1
        print(f"[{sum(counts.values())}/{len(files)}] {status:7s} {rel}" + (f"  ({note})" if note else ""),
              flush=True)

    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(tag_one, library, f, args.force, args.verbose) for f in files]
            for fut in futures:
                report(fut.result())
    else:
        for f in files:
            report(tag_one(library, f, args.force, args.verbose))

    print(f"\ndone: {counts['tagged']} tagged, {counts['skipped']} up to date, {counts['failed']} failed")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
