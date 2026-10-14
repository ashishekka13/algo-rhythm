"""Deterministic DJ: plan a set from a tagged library, then render it.

Usage:
  python dj.py LIBRARY_DIR                    default length: ~75% of the library, once each
  python dj.py LIBRARY_DIR --minutes 30       aim for a ~30-minute mix
  python dj.py LIBRARY_DIR --plan-only        print the setlist and the reasons, render nothing
  python dj.py LIBRARY_DIR --profile build --start "Payphone" --out party.wav

Reads LIBRARY_DIR/tags/*.json written by tagger.py (contract: TAGGING_CONTRACT.md).
Writes LIBRARY_DIR/_mixes/mix.wav and mix.json unless --out is given.

No randomness and no LLM calls: the same library and options always produce the
same mix. Each next song is chosen greedily by

  score = 1.0 * mixability  (tempo x key x vocals: can these two be blended?)
        + 1.2 * curve_fit   (does its energy match where the night should be?)
        + 0.5 * timbre      (does it sound like what's playing?)
        + 0.4 * hook        (how strong is the excerpt we'd play?)
        + 1.2 * vibe        (same feel as what's playing? valence/danceability/acousticness ranks)
        + 1.2 * mood        (same crowd mood? Gemini's lyric mood family + dance-floor moment)
        + 0.8 * blend       (can we beat-match it, or only echo out?)
        + 0.4 * groove      (when the night is peaking, is it danceable?)
        + 0.2 * ease        (is the song's own intro / outro easy to mix over?)
        + 0.8 * join        (do the exact excerpt edges meet: loudness, drums, steadiness?)
        + 0.8 * smooth      (no jump in the room's energy bigger than QA_MAX_ENERGY_STEP)
        + 1.0 * clean       (never two voices at once: outgoing end vs incoming start)
        - 0.8 * repeat      (only if it already played)

Every term is scaled to 0..1 so the weights mean what they say.
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

import librosa
import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy.signal import butter, sosfilt

from algo_rhythm import storage
from algo_rhythm import tagger
from algo_rhythm import transition

OUT_SR = 44100
TARGET_LUFS = -14
EXCERPT_S = 60                  # target excerpt length; whole 32-beat blocks
WEIGHTS = {"mixability": 1.0, "curve_fit": 1.2, "timbre": 0.5, "hook": 0.4,
           "vibe": 1.2, "groove": 0.4, "ease": 0.2, "join": 0.8, "blend": 0.8, "smooth": 0.8, "clean": 1.0,
           "exit": 0.8, "entry": 0.5, "feel": 0.8, "style": 0.4, "best": 0.4, "mood": 1.2}
BEAM, BRANCH = 24, 4            # planner keeps the BEAM best partial sets, each tried with BRANCH next songs
MAX_ATTEMPTS = 8                # re-plan (banning the worst pair) until the QA gate passes
FLEX_MIN_S, FLEX_MAX_S = 50, 140   # live songs play between these many seconds; the best exit decides where
MIN_SUNG_PHRASES = 4           # a played part holds at least this many sung 16-beat phrases: a real verse, not just the intro
QA_LOUDNESS_JUMP_DB, QA_BASS_PILEUP_DB, QA_NUDGE_MS, QA_STRETCH = 3.5, 3.0, 25, 0.10
QA_MAX_ECHO_SHARE, QA_MIN_VIBE, QA_MAX_CURVE_ERR, QA_MIN_JOIN = 0.5, 0.75, 0.25, 0.7
MIN_MATCH = 0.65               # a song may only follow another if their pair score is at least this
MIN_VIBE = 0.8                 # ...and their vibe match (valence/danceability/acousticness) at least this
QA_MAX_DIP_DB = 10             # no 0.5 s window may fall this far below the level around a transition
QA_MAX_VOCAL_CLASH = 0.7      # min(outgoing end, incoming start) vocal activity; above = two voices overlap
LLM_WEIGHT = 0.6              # how much Gemini's energy class counts against the measured rank (x its confidence)
QA_MAX_ENERGY_STEP = 0.35      # a transition may not jump the room's energy by more than this
REPEAT_PENALTY = 0.8
MAX_PLAYS = 2                   # a song can come back once, with a different excerpt
BLEND_MAX_TEMPO_GAP = 0.10      # beyond this, stretching sounds wrong: echo-out instead
# feature_study.py (220 rendered blends): the Camelot key label barely predicted measured chord clash; the songs' own
# pitch-class profiles did (clash rate 54% below 0.0 correlation, 18% above). Straight vs swung feel predicted beat drift.
BLEND_MIN_CHROMA = 0.0          # below: the two songs' chords fight during a long overlap
GRID_MAX_SLIP_MS = 40  # a blend edge wandering further off the beat grid drifts audibly (transition.grid_slip_ms)
GRID_SLIP_GUARD = False  # act on slip (refuse such blends, mark such edges poor); off: it pushed joins onto the echo path
BLEND_MAX_FEEL_GAP = 0.3        # triplet-feel difference above this: drift rate 41-58% vs 22-24%
DEFAULT_MAX_MINUTES = 90
DEFAULT_LIBRARY_SHARE = 0.75    # default mix uses ~75% of songs, so poor fits can be skipped
PROFILES = {                    # target energy percentile vs fraction of the mix
    "house_party": [(0, 0.3), (0.3, 0.8), (0.45, 0.85), (0.6, 0.55), (0.8, 0.95), (0.92, 0.9), (1, 0.5)],
    "build": [(0, 0.2), (1, 0.95)],
    "flat": [(0, 0.5), (1, 0.5)],
}


# ---------------------------------------------------------------- library

def read_tag(library, f, seen):
    """One tag file -> (song dict or None, reason it was skipped). `seen` holds audio hashes already loaded."""
    e = library / "enrich" / f.name   # optional Gemini answer (enrich.py)
    return song_from(library, json.loads(f.read_text()), json.loads(e.read_text()) if e.exists() else None, seen)


def song_from(library, t, llm, seen, features=None):
    """Tag dict + Gemini answer (or None) -> (song dict or None, reason it was skipped).
    features=None (file mode): audio must be in LIBRARY, analyses come from the LIBRARY/_mixes caches.
    Otherwise (database mode) the database decides which songs are active, and `features` maps
    rhythm_feel / grid_slip / vocal_segments to stored values; a missing one is computed and added to it."""
    reason = None
    if not str(t.get("schema_version", "")).startswith("2."):
        reason = f"schema {t.get('schema_version')} (need 2.x)"
    elif features is None and not (library / t["file"]).exists():
        reason = "audio file missing"
    elif "too_few_phrases" in t["quality"]["flags"]:
        reason = "too short to hold two 32-beat overlaps"
    elif t["sha256"] in seen:
        reason = "duplicate audio"
    if reason:
        return None, reason
    seen.add(t["sha256"])
    t["beats"] = np.array(t["beats"])
    if features is None:
        t["triplet"] = cached_feel(library, t)
        t["slips"] = cached_slips(library, t)
        t["segs"] = cached_segments(library, t)
    else:
        for kind, key, compute in (("rhythm_feel", "triplet", lambda: round(triplet_feel(library, t), 4)),
                                   ("grid_slip", "slips", lambda: grid_slips(library, t)),
                                   ("vocal_segments", "segs", lambda: vocal_segments(library, t))):
            if kind not in features:
                features[kind] = compute()
            t[key] = features[kind]
    t["llm"] = llm
    return t, None


def load_library(library):
    songs, skipped, seen = [], [], set()
    for f in sorted((library / "tags").glob("*.json")):
        if f.name.startswith("_"):
            continue
        song, reason = read_tag(library, f, seen)
        if song is None:
            skipped.append((json.loads(f.read_text()).get("file", f.name), reason))
        else:
            songs.append(song)
    finalize(songs)
    return songs, skipped


def finalize(songs):
    """Fill in the library-relative fields (ranks, mood, timbre). Safe to call again after a song is added:
    it updates every dict in place, so indexes into `songs` stay valid."""
    if songs:
        energy = np.array([s["energy"]["score"] for s in songs])
        ranks = energy.argsort(kind="stable").argsort()
        # felt energy: blend the measured rank with Gemini's 1-4 class where known (weight scaled by its confidence),
        # then re-rank so the four levels stay quartiles. No answer = measured only.
        if any(s.get("llm") for s in songs):
            m = ranks / max(1, len(songs) - 1)
            blend = np.array([(1 - LLM_WEIGHT * s["llm"]["confidence"]) * m[i]
                              + LLM_WEIGHT * s["llm"]["confidence"] * (s["llm"]["energy"] - 0.5) / 4
                              if s.get("llm") else m[i] for i, s in enumerate(songs)])
            ranks = blend.argsort(kind="stable").argsort()
        timbre = np.array([s["vectors"]["mfcc_mean"][1:] + s["vectors"]["mfcc_std"][1:]
                           + s["vectors"]["spectral_contrast"] for s in songs])
        timbre = (timbre - timbre.mean(axis=0)) / (timbre.std(axis=0) + 1e-6)
        timbre /= np.linalg.norm(timbre, axis=1, keepdims=True) + 1e-9
        def pct(vals):  # rank within this library, 0..1
            return np.array(vals).argsort(kind="stable").argsort() / max(1, len(songs) - 1)

        vibes = [s.get("vibe") for s in songs]
        has_vibe = all(vibes)
        if has_vibe:
            val_pct = pct([v["valence"] for v in vibes])
            dance_pct = pct([v["danceability"] for v in vibes])
            acou_pct = pct([v["acousticness"] for v in vibes])
        for i, s in enumerate(songs):
            s["energy_pct"] = float(ranks[i] / max(1, len(songs) - 1))
            s["valence_pct"] = float(val_pct[i]) if has_vibe else 0.5
            s["dance_pct"] = float(dance_pct[i]) if has_vibe else 0.5
            s["acoustic_pct"] = float(acou_pct[i]) if has_vibe else 0.5
            s["pm"] = vibes[i].get("phrase_mix") if has_vibe else None
            s["in_ease"] = vibes[i]["mix_in_ease"] if has_vibe else 0.5
            s["out_ease"] = vibes[i]["mix_out_ease"] if has_vibe else 0.5
            # DJ-style mood label: valence x arousal, each split at the library median
            warm = "bright" if s["valence_pct"] >= 0.5 else "moody"
            drive = "driving" if s["energy_pct"] >= 0.5 else "laid-back"
            s["mood"] = mood_family(s.get("llm")) or (f"{warm}/{drive}" if has_vibe else "n/a")  # Gemini's family when known
            s["timbre"] = timbre[i]
            # feature_study: blends with a flagged song drifted off the beat 73% of the time (30% otherwise)
            s["tempo_uncertain"] = "tempo_disagreement" in s["quality"]["flags"]
            s["name"] = display_name(s, s.get("llm"))


def clean_title(raw):
    """File-name title -> (title, film), no AI: 'Mehrama_From_Love_Aaj_Kal [yF2bRe2oCd8]' -> ('Mehrama', 'Love Aaj Kal')."""
    t = re.sub(r"\s*\[[\w-]{6,}\]\s*$", "", raw).replace("_", " ")
    t = re.sub(r"\s+", " ", t).strip()
    m = re.match(r"(.+?)\s+\(?From\s+(.+?)\)?$", t)  # "X (From Film)" or "X From Film"
    return (m.group(1), m.group(2)) if m else (t, "")


def display_name(t, llm):
    """'Title - Film' (or just 'Title'): Gemini's answer when it has one, else the file name cleaned.
    t is a tag: tagger stores 'Song - Film.mp3' as artist='Song', title='Film'."""
    title, film = (t["artist"], t["title"]) if t.get("artist") else clean_title(t["title"])
    if (llm or {}).get("title"):
        title, film = llm["title"], llm.get("film", "")
    return f"{title} - {film}" if film else title
# ---------------------------------------------------------------- scoring

def tempo_gap(a, b):
    """Fractional tempo difference, treating half/double time as the same tempo."""
    return min(abs(b * r - a) / a for r in (0.5, 1, 2))


def camelot(c):
    return int(c[:-1]), c[-1]


def key_steps(a, b):
    (na, la), (nb, lb) = camelot(a), camelot(b)
    return min((na - nb) % 12, (nb - na) % 12) + (la != lb)


def key_score(a, b):
    (na, la), (nb, lb) = camelot(a), camelot(b)
    d = min((na - nb) % 12, (nb - na) % 12)
    if d == 0 and la == lb:
        return 1.0
    if (d == 0) or (d == 1 and la == lb):   # relative major/minor, or one step round the wheel
        return 0.9
    if d == 2 and la == lb:                 # energy boost / drop
        return 0.75
    if d == 1:                              # diagonal
        return 0.6
    return 0.4


def mixability(a, b):
    gap = tempo_gap(a["tempo"]["bpm"], b["tempo"]["bpm"])
    vocal = 1.0
    if a.get("vocal_ratio") is not None and b.get("vocal_ratio") is not None:
        vocal = 0.6 if a["vocal_ratio"] > 0.5 and b["vocal_ratio"] > 0.5 else 1.0
    harmony = 0.3 * key_score(a["key"]["camelot"], b["key"]["camelot"]) + 0.7 * min(1.0, max(0.0, (chroma_corr(a, b) + 0.1) / 0.7))
    return math.exp(-gap / 0.04) * harmony * vocal


def chroma_corr(a, b):
    """Correlation of the two songs' average pitch-class profiles: do they use the same notes?"""
    return float(np.corrcoef(a["vectors"]["chroma"], b["vectors"]["chroma"])[0, 1])


def triplet_feel(library, song):
    """0 = straight eighths, 1 = triplet/compound feel (6/8, dadra, shuffle): onset strength at beat thirds vs halves."""
    t0 = song["duration_s"] * 0.3
    y, sr = librosa.load(storage.local_path(song["file"], library), sr=22050, mono=True, offset=t0, duration=40)
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=256)
    beats = np.asarray(song["beats"])
    beats = beats[(beats > t0) & (beats < t0 + 39)] - t0
    if len(beats) < 9:
        return 0.5
    at = lambda t: env[np.clip(np.round(t * sr / 256).astype(int), 0, len(env) - 1)]
    p = np.diff(beats)
    half, third = at(beats[:-1] + p / 2), (at(beats[:-1] + p / 3) + at(beats[:-1] + 2 * p / 3)) / 2
    return float(np.mean(third) / (np.mean(third) + np.mean(half) + 1e-9))


_FEEL = {}


def cached_feel(library, song):
    """triplet_feel, cached per audio hash in LIBRARY/_mixes/rhythm_feel.json (about 0.3 s per new song)."""
    path = library / "_mixes" / "rhythm_feel.json"
    if path not in _FEEL:
        _FEEL[path] = json.loads(path.read_text()) if path.exists() else {}
    cache = _FEEL[path]
    if song["sha256"] not in cache:
        cache[song["sha256"]] = round(triplet_feel(library, song), 4)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(cache))
    return cache[song["sha256"]]


def grid_slips(library, song):
    """Per phrase i: how far the audio wanders off the tagged beat grid over phrases i..i+1 (an overlap's length), in
    ms. A tag's beat list can be wrong in places (a missed or extra beat, a jump to the off-beat); blending there is a
    train-wreck however well the warp works, so those edges are poor mix points (transition.grid_slip_ms)."""
    from algo_rhythm import transition
    y, sr = librosa.load(storage.local_path(song["file"], library), sr=22050, mono=True)
    beats, starts, pb = np.asarray(song["beats"]), song["phrases"]["start_beats"], tagger.PHRASE_BEATS
    out = []
    for sb in starts:
        eb = min(sb + 2 * pb, len(beats) - 1)
        a, b = int(beats[sb] * sr), int(beats[eb] * sr)
        out.append(round(transition.grid_slip_ms(y[a:b, None], beats[sb:eb + 1] - beats[sb], sr), 1) if eb > sb else 0.0)
    return out


_SLIPS = {}


def cached_slips(library, song):
    """grid_slips, cached per audio hash in LIBRARY/_mixes/grid_slip.json (a few seconds per new song)."""
    path = library / "_mixes" / "grid_slip.json"
    if path not in _SLIPS:
        _SLIPS[path] = json.loads(path.read_text()) if path.exists() else {}
    cache = _SLIPS[path]
    if song["sha256"] not in cache:
        cache[song["sha256"]] = grid_slips(library, song)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(cache))
    return cache[song["sha256"]]


def vocal_segments(library, song):
    """Where the song is sung, as [start, end] seconds (transition.sung_segments on its Demucs vocal stem, 20 ms
    steps), or None without a stem. Gaps of transition.LINE_GAP_S or more between segments are where lines end."""
    from algo_rhythm import media, transition
    p = media.stem_path(library, tagger.tag_key(song["file"]), storage.local_path)
    if not p.exists():
        return None
    v, sr = sf.read(p, always_2d=True)
    return [[round(a, 2), round(b, 2)] for a, b in transition.sung_segments(v, sr)]


_SEGS = {}


def cached_segments(library, song):
    """vocal_segments, cached per audio hash in LIBRARY/_mixes/vocal_segments.json (about a second per new song)."""
    path = library / "_mixes" / "vocal_segments.json"
    if path not in _SEGS:
        _SEGS[path] = json.loads(path.read_text()) if path.exists() else {}
    cache = _SEGS[path]
    if song["sha256"] not in cache:
        cache[song["sha256"]] = vocal_segments(library, song)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(cache))
    return cache[song["sha256"]]


def target_energy(profile, frac):
    xs, ys = zip(*PROFILES[profile])
    return float(np.interp(min(max(frac, 0), 1), xs, ys))


def transition_kind(a, b):
    gap = tempo_gap(a["tempo"]["bpm"], b["tempo"]["bpm"])
    steps = key_steps(a["key"]["camelot"], b["key"]["camelot"])
    if a["tempo_uncertain"] or b["tempo_uncertain"]:
        return "echo", "tempo uncertain (tempo_disagreement flag)"
    if gap > BLEND_MAX_TEMPO_GAP:
        return "echo", f"tempo gap {gap:.0%} > {BLEND_MAX_TEMPO_GAP:.0%}"
    feel = abs(a.get("triplet", 0.5) - b.get("triplet", 0.5))
    if feel > BLEND_MAX_FEEL_GAP:
        return "echo", f"rhythm feel differs (straight vs swung, {feel:.2f})"
    chords = chroma_corr(a, b)
    if chords < BLEND_MIN_CHROMA:
        return "echo", f"chords clash (pitch profiles {chords:+.2f})"
    return "blend", f"tempo gap {gap:.0%}, keys {a['key']['camelot']}->{b['key']['camelot']} ({steps} steps)"


# ---------------------------------------------------------------- planning

def pair_base(prev, s):
    """Song-level terms for playing `s` after `prev`, each 0..1. Shared by the planner and the matrix."""
    kind, why = transition_kind(prev, s) if prev else ("start", "opening track")
    feel = (1 - (abs(prev["valence_pct"] - s["valence_pct"]) + abs(prev["dance_pct"] - s["dance_pct"])
                 + abs(prev["acoustic_pct"] - s["acoustic_pct"])) / 3) if prev else 1.0
    terms = {
        "mixability": mixability(prev, s) if prev else 1.0,
        "timbre": (float(prev["timbre"] @ s["timbre"]) + 1) / 2 if prev else 1.0,
        "vibe": feel,
        "ease": (prev["out_ease"] + s["in_ease"]) / 2 if prev else 1.0,
        "blend": 1.0 if kind in ("blend", "start") else 0.0,
        "smooth": 1 - min(abs(prev["energy_pct"] - s["energy_pct"]) / QA_MAX_ENERGY_STEP, 1) if prev else 1.0,
        "feel": 1 - min(abs(prev.get("triplet", 0.5) - s.get("triplet", 0.5)) / BLEND_MAX_FEEL_GAP, 1) if prev else 1.0,
        "style": style_fit(prev, s) if prev else 1.0,
        "mood": mood_fit(prev, s) if prev else 1.0,
    }
    return terms, kind, why


PAIR_KEYS = ("mixability", "timbre", "vibe", "ease", "blend", "smooth", "feel", "style", "mood")
# Gemini's style labels, grouped the way a DJ would let one follow another (enrich.py). Unknown = neutral.
STYLE_GROUPS = ({"ballad", "sufi", "indie_acoustic", "qawwali"}, {"dance_pop", "club_edm", "item_number", "hiphop"},
                {"punjabi", "folk"}, {"rock"})


def style_fit(a, b):
    """1 = same style, 0.7 = same family (e.g. club and item number), 0.3 = a jump a crowd notices; plus language
    and era continuity. 0.6 when Gemini has not labelled one of them."""
    la, lb = a.get("llm") or {}, b.get("llm") or {}
    if "style" not in la or "style" not in lb:
        return 0.6
    sa, sb = la["style"], lb["style"]
    fit = 1.0 if sa == sb else 0.7 if any(sa in g and sb in g for g in STYLE_GROUPS) else 0.3
    if la.get("language") != lb.get("language") and "mixed" not in (la.get("language"), lb.get("language")):
        fit -= 0.15
    if abs(la.get("year", 2015) - lb.get("year", 2015)) > 15:
        fit -= 0.1
    return max(0.0, fit)


# Crowd mood from Gemini's answers (enrich.py): the lyric mood picks a family, the moment says how much of a dance
# floor it is. A dance song should be followed by another dance/party song, a sad one by sad or romantic.
MOOD_WORDS = {  # substrings of Gemini's free-text lyric_mood (113 distinct values; these cover ~85% of songs)
    "party": ("party", "energ", "pepp", "fun", "hype", "swag", "festiv", "celebrat", "upbeat", "wild", "attitude",
              "groov", "catchy", "high", "playful", "quirky", "joy", "rebel", "bold", "fierce", "confiden", "power",
              "cheer", "banger", "sass", "aggress", "intoxic", "drink", "glamor", "sizzl", "motivat"),
    "romantic": ("romant", "sweet", "flirt", "sensual", "passion", "tender", "dream", "lov", "heartfelt", "seduct",
                 "devoted", "soulful",  # devoted in love, not devotional: checked before the devotional words "longing",
                 "yearn", "desire"),
    "sad": ("melanch", "heartbr", "sorrow", "sad", "nostalg", "sentiment", "pain", "lonel", "grief", "bitter",
            "moody", "thoughtful", "mysteri"),
    "devotional": ("devotion", "spiritu", "divine", "mystic", "prayer", "bhakti"),
    "chill": ("peace", "free", "smooth", "poetic", "calm", "uplift", "hope", "reflect", "breez", "carefree", "relax", "sooth", "chill", "content",
              "cool", "melodic"),
}
STYLE_MOOD = {"dance_pop": "party", "club_edm": "party", "item_number": "party", "hiphop": "party", "punjabi": "party",
              "ballad": "romantic", "sufi": "devotional", "qawwali": "devotional", "indie_acoustic": "chill"}
FAMILY_FIT = {  # symmetric; a missing pair is 0.5
    ("party", "romantic"): 0.4, ("party", "chill"): 0.3, ("party", "sad"): 0.0, ("party", "devotional"): 0.2,
    ("romantic", "sad"): 0.7, ("romantic", "chill"): 0.7, ("romantic", "devotional"): 0.6,
    ("sad", "chill"): 0.6, ("sad", "devotional"): 0.6, ("chill", "devotional"): 0.7,
}
FLOOR = {"slow_dance": 0.0, "closing": 0.1, "warmup": 0.35, "singalong": 0.5, "groove": 0.75, "peak": 1.0}
MOOD_FAMILY_W = 0.6  # rest is the dance-floor match
MIN_MOOD = 0.5       # live planner: below this a crowd hears the mood break (party -> sad, dance floor -> slow dance)


def mood_family(llm):
    """party | romantic | sad | devotional | chill, or None when Gemini has not answered. A peak-hour song, or a groove
    in a dance style, is a party song whatever its lyrics say (Jai Jai Shivshankar is 'devotional' and a banger)."""
    if not llm or "moment" not in llm:
        return None
    style = llm.get("style")
    if llm["moment"] == "peak" or (llm["moment"] == "groove" and STYLE_MOOD.get(style) == "party"):
        return "party"
    lyric = str(llm.get("lyric_mood", "")).lower()
    for fam, words in MOOD_WORDS.items():
        if any(w in lyric for w in words):
            # Gemini says "devotion" for love ballads too (Kaun Tujhe); a devotional song here is sufi, qawwali or folk
            return "romantic" if fam == "devotional" and style in ("ballad", "indie_acoustic") else fam
    return STYLE_MOOD.get(style)


def family_fit(fa, fb):
    return 1.0 if fa == fb else FAMILY_FIT.get((fa, fb), FAMILY_FIT.get((fb, fa), 0.5))


def mood_fit(a, b):
    """0..1: does b keep the crowd's mood after a? Family match blended with dance-floor match. 0.6 = unknown."""
    la, lb = a.get("llm") or {}, b.get("llm") or {}
    fa, fb = mood_family(la), mood_family(lb)
    if fa is None or fb is None:
        return 0.6
    fam = family_fit(fa, fb)
    floor = 1 - abs(FLOOR.get(la["moment"], 0.5) - FLOOR.get(lb["moment"], 0.5))
    return MOOD_FAMILY_W * fam + (1 - MOOD_FAMILY_W) * floor


def pair_score(terms):
    """0..1 compatibility of 'B after A': weighted mean of the song-level terms. A ranking, not a probability."""
    return sum(WEIGHTS[k] * terms[k] for k in PAIR_KEYS) / sum(WEIGHTS[k] for k in PAIR_KEYS)


def window_terms(s, goal, w, prev_w, swap_ok=True):
    """Terms that depend on which excerpt is chosen, not just which song."""
    if prev_w is None or prev_w["last_db"] is None or w["first_db"] is None:
        match = 1.0 if prev_w is None else 0.5
    else:  # outgoing tail vs incoming head: a loudness step shows up as a bump in the mix
        match = 1 - min(abs(prev_w["last_db"] - w["first_db"]) / 6, 1)
    join = 1.0 if prev_w is None else 0.5 * match + 0.25 * prev_w["out_q"] + 0.25 * w["in_q"]
    clash = 0.0 if prev_w is None else min(prev_w["out_vocal"], w["in_vocal"])  # both singing = lyrics collide
    if swap_ok and prev_w is not None and prev_w.get("stems") and w.get("stems"):
        clash *= 0.25  # with the vocal swap two voices cannot overlap, so only a mild preference for quieter edges remains
    clean = 1.0 - clash
    return {"curve_fit": math.exp(-((s["energy_pct"] - goal) / 0.2) ** 2),
            "groove": s["dance_pct"] if goal >= 0.6 else 1.0,
            "hook": w["score"], "join": join, "clean": clean,
            # A DJ leaves a song where the singer is not singing, and brings the next one in the same way.
            "exit": 1.0 - w["out_vocal"], "entry": max(w.get("lead_in", 0.0), 1.0 - w["in_vocal"]),
            "best": w.get("has_best", 0.0)}


def candidate_windows(song, excerpt_s, exclude, keep=8, force_first=False, spacing=None, flex=False):
    """Every excerpt a song could play, scored on more than the hook.

    A window's edges are what get mixed, so each is also rated on how clean they are:
    in_q = drums present and steady in its first 2 phrases, out_q = same for its last 2.
    Returns the `keep` best, spaced at least half a window apart so they are real alternatives.
    flex=True: the length is free too (FLEX_MIN_S..FLEX_MAX_S, in 2-phrase steps), so each song ends where its own
    best exit is. `excerpt_s` is then only the preferred length, as a mild pull.
    """
    ph, beats, pm = song["phrases"], song["beats"], song.get("pm")
    starts, scores = ph["start_beats"], np.array(ph["score"])
    if len(starts) < 4:
        return []
    period = float(np.median(np.diff(beats)))
    pair_s = 2 * tagger.PHRASE_BEATS * period  # two phrases
    n0 = 2 * max(1, round(excerpt_s / pair_s))
    n0 = int(min(max(n0, 4), len(starts)))
    ns = [n0]
    if flex:
        lo = max(4, 2 * round(FLEX_MIN_S / pair_s))
        hi = max(lo, 2 * round(FLEX_MAX_S / pair_s))
        ns = sorted({int(min(n, len(starts))) for n in range(lo, hi + 1, 2)} | {n0})
    med_db = float(np.median(pm["rms_db"])) if pm else 0.0  # the song's typical level: edges far below it are fades or breaks
    voc = np.array(pm["vocal"]) if pm and "vocal" in pm else None
    # "the best bit": the two strongest phrases next to each other (usually the hook); a window should hold it
    best = int(np.argmax(scores[:-1] + scores[1:])) if len(scores) > 1 else 0
    sung = (voc >= 0.5) if voc is not None else None
    # a played part must reach a proper verse: at least MIN_SUNG_PHRASES sung phrases (or as many as the song has)
    need_sung = min(MIN_SUNG_PHRASES, int(sung.sum())) if sung is not None else 0
    slips = song.get("slips") or [0.0] * len(starts)
    segs = song.get("segs")
    from algo_rhythm import transition
    # an edge whose beat grid slips cannot be beat-matched, however good its drums
    edge = (lambda i: (0.5 * np.mean(pm["perc"][i:i + 2]) + 0.5 * np.mean(pm["steady"][i:i + 2]))
            * (1.0 if not GRID_SLIP_GUARD or slips[i] <= GRID_MAX_SLIP_MS else 0.2)) if pm else (lambda i: 0.5)
    cands = []
    for n, i in ((n, i) for n in ns for i in range(len(starts) - n + 1)):
        sb, eb = starts[i], starts[i] + tagger.PHRASE_BEATS * n
        if any(sb < y and x < eb for x, y in exclude):
            continue
        if sung is not None and sung[i:i + n].sum() < need_sung:
            continue  # it would end before the singing really gets going
        hook, in_q, out_q = float(scores[i:i + n].mean()), float(edge(i)), float(edge(i + n - 2))
        has_best = float(i <= best and best + 1 < i + n)
        # lead-in: the overlap (first two phrases) is instrumental and the singing starts right after it, so the
        # first words come in at full level once the mix is done
        lead_in = float(voc is not None and i + 2 < len(voc) and voc[i:i + 2].max() < 0.35 and voc[i + 2] >= 0.5)
        # edges from the 20 ms sung segments: the part starts between lines, and it can stop on one of its last 4
        # beats with the singer between lines (transition.clean_cut), so neither join cuts a lyric
        entry_clean = float(segs is None or transition.clean_cut(segs, float(beats[sb]), guard_s=0.0))
        exit_clean = float(segs is None or any(transition.clean_cut(segs, float(beats[k]))
                                               for k in range(max(sb, eb - 4), min(eb, len(beats) - 1) + 1)))
        in_v = float(np.mean(pm["vocal"][i:i + 2])) if pm and "vocal" in pm else 0.5
        out_v = float(np.mean(pm["vocal"][i + n - 2:i + n])) if pm and "vocal" in pm else 0.5
        length_pull = 0.0 if not flex else 0.08 * (1 - min(1.0, abs(n * pair_s / 2 - excerpt_s) / (FLEX_MAX_S - FLEX_MIN_S)))
        cands.append((0.3 * hook + 0.15 * in_q + 0.15 * out_q + 0.1 * (1 - in_v) + 0.3 * (1 - out_v) + length_pull
                      + 0.2 * has_best + 0.15 * lead_in + 0.15 * entry_clean + 0.15 * exit_clean, -i, i, {
            "n": n, "has_best": has_best, "lead_in": lead_in, "entry_clean": entry_clean, "exit_clean": exit_clean,
            "start_beat": sb, "end_beat": eb, "start_s": round(float(beats[sb]), 3),
            "end_s": round(float(beats[eb] if eb < len(beats) else beats[-1] + period), 3),
            "score": round(hook, 3), "in_q": round(in_q, 3), "out_q": round(out_q, 3),
            "in_vocal": round(in_v, 3), "out_vocal": round(out_v, 3), "natural_start": i == 0,
            "stems": (song.get("vibe") or {}).get("vocal_source") == "demucs",  # the vocal swap is available for this song
            "first_db": float(np.mean(pm["rms_db"][i:i + 2])) if pm else None,
            "last_db": float(np.mean(pm["rms_db"][i + n - 2:i + n])) if pm else None,
            "in_low_rel": round(float(np.min(pm["low_db"][i:i + 2])) - med_db, 1) if pm and "low_db" in pm else 0.0,
            "out_low_rel": round(float(np.min(pm["low_db"][i + n - 2:i + n])) - med_db, 1) if pm and "low_db" in pm else 0.0,
            "first_rel": round(float(np.mean(pm["rms_db"][i:i + 2])) - med_db, 1) if pm else 0.0,
            "last_rel": round(float(np.mean(pm["rms_db"][i + n - 2:i + n])) - med_db, 1) if pm else 0.0}))
    picked = []
    for c in sorted(cands, reverse=True):
        sp = spacing or max(1, n0 // 2)  # alternatives differ in where they start or where they end
        if all(abs(c[2] - p[2]) >= sp or abs(c[2] + c[3]["n"] - p[2] - p[3]["n"]) >= sp for p in picked):
            picked.append(c)
        if len(picked) == keep:
            break
    if force_first and not any(c[2] == 0 for c in picked):  # keep the song's own beginning as an option
        picked += [c for c in cands if c[2] == 0]
    return [c[3] for c in picked]


def plan_set(songs, target_s, profile, excerpt_s, start=None, banned=frozenset(), min_match=MIN_MATCH, min_vibe=MIN_VIBE):
    """Deterministic beam search over (song, excerpt) choices. Returns the best list of plan entries.

    Greedy picking chooses each song for the next step only and strands itself (a great
    song now, nothing blendable after). The beam keeps several partial sets alive and
    picks the set with the best average step score, so the whole chain hangs together.
    The excerpt is part of the choice: the best-hook window is not always the one that
    joins best, so each song offers several and the planner takes whichever mixes best.
    """
    repeat_gap = max(600, 0.3 * target_s)
    artist_gap = min(45 * 60, 0.25 * target_s)
    windows, bases = {}, {}

    def window_for(i, used):
        key = (i, tuple(used))
        if key not in windows:
            windows[key] = candidate_windows(songs[i], excerpt_s, used)
        return windows[key]

    def base_for(prev, i):
        key = (prev["file"] if prev else None, i)
        if key not in bases:
            terms, kind, why = pair_base(prev, songs[i])
            bases[key] = (terms, kind, why, pair_score(terms))
        return bases[key]

    def eligible(h, i, relax):
        plan, plays, elapsed = h["plan"], h["plays"], h["elapsed"]
        if plan and plan[-1]["song"] == i:
            return False
        if plays.get(i, 0) >= MAX_PLAYS:
            return False
        if plays.get(i) and (relax < 2 and elapsed - h["last_play"][i] < repeat_gap):
            return False
        a = songs[i]["artist"]
        if a and relax < 1 and a in h["last_artist"] and elapsed - h["last_artist"][a] < artist_gap:
            return False
        return True

    def expand(h):
        goal = target_energy(profile, h["elapsed"] / target_s)
        prev = songs[h["plan"][-1]["song"]] if h["plan"] else None
        prev_w = h["plan"][-1]["window"] if h["plan"] else None
        for relax in range(3):  # 0: all rules, 1: allow same artist sooner, 2: allow repeats sooner
            out = []
            for i, s in enumerate(songs):
                if not eligible(h, i, relax):
                    continue
                if not h["plan"] and start and start.lower() not in s["file"].lower():
                    continue
                if prev and (prev["file"], s["file"]) in banned:
                    continue
                base, kind, why, match = base_for(prev, i)
                if prev and (match < min_match or base["vibe"] < min_vibe):
                    continue
                best = None
                for w in window_for(i, h["used"].get(i, ())):
                    terms = {**base, **window_terms(s, goal, w, prev_w)}
                    score = sum(WEIGHTS[k] * v for k, v in terms.items()) - (REPEAT_PENALTY if h["plays"].get(i) else 0)
                    if best is None or score > best[0]:
                        best = (score, -i, i, w, terms, kind, why, goal)  # -i: earlier file wins ties
                if best:
                    out.append(best)
            if out:
                return sorted(out, reverse=True)[:BRANCH]
        return []

    def extend(h, cand):
        score, _, i, w, terms, kind, why, goal = cand
        s = songs[i]
        period = float(np.median(np.diff(s["beats"])))
        overlap = 0.0 if not h["plan"] else (32 * period if kind == "blend" else transition.ECHO_REPEATS * period)
        elapsed = h["elapsed"] + (w["end_s"] - w["start_s"]) - overlap
        entry = {"song": i, "window": w, "terms": terms, "score": score, "transition_in": kind, "why": why,
                 "target_energy": goal, "repeat": bool(h["plays"].get(i)), "planned_at_s": h["elapsed"] - overlap}
        return {"plan": h["plan"] + [entry], "elapsed": elapsed, "total": h["total"] + score,
                "plays": {**h["plays"], i: h["plays"].get(i, 0) + 1},
                "used": {**h["used"], i: [*h["used"].get(i, ()), (w["start_beat"], w["end_beat"])]},
                "last_play": {**h["last_play"], i: elapsed},
                "last_artist": {**h["last_artist"], **({s["artist"]: elapsed} if s["artist"] else {})}}

    active = [{"plan": [], "elapsed": 0.0, "total": 0.0, "plays": {}, "used": {}, "last_play": {}, "last_artist": {}}]
    done = []
    for _ in range(80):
        children = []
        for h in active:
            cands = expand(h)
            if not cands and h["plan"]:
                done.append(h)
            children += [extend(h, c) for c in cands]
        children.sort(key=lambda h: (-h["total"] / len(h["plan"]), tuple(e["song"] for e in h["plan"])))
        active = []
        for h in children[:BEAM]:
            (done if h["elapsed"] >= target_s else active).append(h)
        if not active:
            break
    if not done:
        return []
    # Prefer sets that fill the requested time (the cutoff can dead-end a chain), then the best average score.
    best = min(done, key=lambda h: (-min(h["elapsed"], target_s), -h["total"] / len(h["plan"]),
                                    tuple(e["song"] for e in h["plan"])))
    return best["plan"]


def write_matrix(songs, path):
    """Pair-quality matrix: row = song playing, column = song that could follow (0..1, higher = better)."""
    lines = ["from\\to," + ",".join(f'"{s["name"]}"' for s in songs)]
    best = {}
    for a in songs:
        row = []
        for b in songs:
            if a is b:
                row.append(0.0)
                continue
            row.append(pair_score(pair_base(a, b)[0]))
        best[a["name"]] = sorted(zip(row, (s["name"] for s in songs)), reverse=True)[:3]
        lines.append(f'"{a["name"]}",' + ",".join(f"{v:.2f}" for v in row))
    path.write_text("\n".join(lines))
    return best


# ---------------------------------------------------------------- rendering

def load_excerpt(library, song, window):
    seg, beats, _ = load_excerpt_vocals(library, song, window)
    return seg, beats


def load_excerpt_vocals(library, song, window):
    """(excerpt, beat times, vocals or None). The vocals are the same stretch of the song's Demucs vocals track,
    scaled exactly like the excerpt, so (excerpt - vocals) is the instrumental. None if the song has no stem yet."""
    audio, sr = sf.read(storage.local_path(song["file"], library), always_2d=True, dtype="float64")
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    audio = audio[:, :2]
    if sr != OUT_SR:
        audio = librosa.resample(audio.T, orig_sr=sr, target_sr=OUT_SR).T
    start, end = window["start_s"], window["end_s"]
    lo, hi = int(start * OUT_SR), int(end * OUT_SR)
    seg = audio[lo:hi].copy()
    lufs = pyln.Meter(OUT_SR).integrated_loudness(seg)
    gain = min(10 ** ((TARGET_LUFS - lufs) / 20), 1.0 / (np.abs(seg).max() + 1e-9))  # never push a peak past full scale
    seg *= gain
    ramp = int(0.01 * OUT_SR)
    seg[:ramp] *= np.linspace(0, 1, ramp)[:, None]

    from algo_rhythm import media
    voc = None
    key = tagger.tag_key(song["file"])
    stem, meta = media.stem_path(library, key, storage.local_path), storage.local_path(f"stems/{key}.json", library)
    try:
        if stem.exists() and json.loads(meta.read_text()).get("sha256") == song["sha256"]:
            v, vsr = sf.read(stem, always_2d=True, dtype="float64")
            if vsr != OUT_SR:
                v = librosa.resample(v.T, orig_sr=vsr, target_sr=OUT_SR).T
            voc = np.zeros_like(seg)
            part = v[lo:hi, :2] * gain
            voc[:len(part)] = part[:len(seg)]
            voc[:ramp] *= np.linspace(0, 1, ramp)[:, None]
    except (OSError, ValueError, KeyError):
        voc = None  # a missing or stale stem just means no vocal swap for this song

    b = song["beats"]
    beats = b[window["start_beat"] : window["end_beat"]] - start
    return seg, np.append(beats, len(seg) / OUT_SR), voc


def soft_limit(x, knee=0.9):
    """Leave everything under the knee alone; squash anything above it smoothly below 1.0."""
    a = np.abs(x)
    over = a > knee
    x[over] = np.sign(x[over]) * (knee + (1 - knee) * np.tanh((a[over] - knee) / (1 - knee)))
    return x


def render(library, songs, plan, out_path):
    written = 0
    with sf.SoundFile(out_path, "w", OUT_SR, 2, subtype="PCM_16") as out:
        def write(x):
            nonlocal written
            out.write(soft_limit(x.copy()))
            written += len(x)

        seg, beats = load_excerpt(library, songs[plan[0]["song"]], plan[0]["window"])
        skip = 0
        plan[0]["position_s"] = 0.0
        for i, entry in enumerate(plan):
            if i == len(plan) - 1:
                write(seg[skip:])
                period = float(np.median(np.diff(beats)))
                write(transition.echo_tail(seg, period, OUT_SR, repeats=8, feedback=0.6))
                break
            nxt_entry = plan[i + 1]
            nxt, nxt_beats = load_excerpt(library, songs[nxt_entry["song"]], nxt_entry["window"])
            kind = nxt_entry["transition_in"]
            if kind == "blend":
                step_a, _ = transition.beat_steps(beats, nxt_beats)
                tail_start = beats[-(transition.OVERLAP_BEATS * step_a + 1)] * OUT_SR
                if tail_start < skip:  # excerpt too short for both an incoming and outgoing overlap
                    kind = nxt_entry["transition_in"] = "echo"
                    nxt_entry["why"] += "; excerpt too short for a blend"
            before, overlap, nxt_skip, info = transition.transition(seg, beats, nxt, nxt_beats, OUT_SR, kind)
            write(before[skip:])
            nxt_entry["position_s"] = written / OUT_SR
            nxt_entry["transition_info"] = info
            write(overlap)
            seg, beats, skip = nxt, nxt_beats, nxt_skip
    return written / OUT_SR


def qa(wav, rows):
    """Measure the rendered mix like a picky listener would. Returns checks, and the pair to ban if any fail.

    A proxy for 'would a DJ wince', not a replacement for ears: it catches level jumps,
    bass pile-ups, warbling stretches, mistimed grids, dead air and clipping, but not
    two vocals fighting or a song that is simply the wrong choice.
    """
    y, sr = sf.read(wav, dtype="float32", always_2d=True)
    m = y.mean(axis=1)
    low = sosfilt(butter(4, 150, "lowpass", fs=sr, output="sos"), m)
    db = lambda x: 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)
    seg = lambda x, a, b: x[int(max(a, 0) * sr): int(max(b, a + 0.1) * sr)]
    checks, bad = [], {}

    def add(name, value, ok, limit, pair=None):
        if not np.isfinite(value):  # a transition too close to the end of the audio to measure
            return
        checks.append({"check": name, "value": round(float(value), 2), "limit": limit, "ok": bool(ok)})
        if not ok and pair:
            bad[pair] = bad.get(pair, 0) + 1

    for k in range(1, len(rows)):
        r, info = rows[k], rows[k].get("transition_info", {})
        t0 = r["position_s"]
        t1 = t0 + (info.get("overlap_s") or 4 * 60 / r["bpm"])
        pair = (rows[k - 1]["file"], r["file"])
        # The join must not spike or dip relative to BOTH neighbours. Matching one side is fine: the live engine starts a
        # louder song at the outgoing level and eases it up, so the join legitimately sits near the "before" level.
        ov = db(seg(m, t0, t1))
        jump = min(ov - db(seg(m, t0 - 8, t0)), ov - db(seg(m, t1, t1 + 8)), key=abs)
        add(f"T{k} loudness jump dB", abs(jump), abs(jump) <= QA_LOUDNESS_JUMP_DB, QA_LOUDNESS_JUMP_DB, pair)
        # 0.5 s windows: a drum rest or breath is music; a hole is a second or more
        lv = lambda a, b: [db(m[i: i + sr // 2]) for i in range(int(max(a, 0) * sr), int(b * sr) - sr // 2, sr // 2)]
        around_lv, join_lv = lv(t0 - 6, t1 + 6), lv(t0 - 1, t1 + 1)
        if around_lv and join_lv:
            dip = float(np.median(around_lv) - min(join_lv))
            add(f"T{k} dip dB", dip, dip <= QA_MAX_DIP_DB, QA_MAX_DIP_DB, pair)
        pile = db(seg(low, t0, t1)) - max(db(seg(low, t0 - 8, t0)), db(seg(low, t1, t1 + 8)))
        add(f"T{k} bass pile-up dB", pile, pile <= QA_BASS_PILEUP_DB, QA_BASS_PILEUP_DB, pair)
        step = abs(r["energy_pct"] - rows[k - 1]["energy_pct"])
        add(f"T{k} energy step", step, step <= QA_MAX_ENERGY_STEP, QA_MAX_ENERGY_STEP, pair)
        if info.get("type") == "blend" and info.get("vocal_swap"):  # stems: measure the voices actually left overlapping
            add(f"T{k} vocal overlap s", info["vocal_overlap_s"], info["vocal_overlap_s"] <= 1.0, 1.0, pair)
        elif info.get("type") == "blend" and "window" in r and "window" in rows[k - 1]:
            clash = min(rows[k - 1]["window"]["out_vocal"], r["window"]["in_vocal"])
            add(f"T{k} vocal overlap", clash, clash <= QA_MAX_VOCAL_CLASH, QA_MAX_VOCAL_CLASH, pair)
        if info.get("type") == "blend":
            sa, sb = (int(x) for x in info["beat_pairing"].split(":"))  # 2:1 = every 2nd beat of the faster song
            out_bpm, in_bpm = info["bpm_out"] / sa, info["bpm_in"] / sb
            stretch = abs(out_bpm - in_bpm) / out_bpm
            add(f"T{k} tempo stretch", stretch, stretch <= QA_STRETCH, QA_STRETCH, pair)
            add(f"T{k} beat-grid nudge ms", abs(info["phase_nudge_ms"]), abs(info["phase_nudge_ms"]) <= QA_NUDGE_MS,
                QA_NUDGE_MS, pair)

    n_tr = max(1, len(rows) - 1)
    echoes = sum(r["transition_in"] == "echo" for r in rows)
    add("echo-out share", echoes / n_tr, echoes / n_tr <= QA_MAX_ECHO_SHARE, QA_MAX_ECHO_SHARE)
    vibe = float(np.mean([r["terms"]["vibe"] for r in rows[1:]])) if len(rows) > 1 else 1.0
    add("mean vibe match", vibe, vibe >= QA_MIN_VIBE, QA_MIN_VIBE)
    join = float(np.mean([r["terms"]["join"] for r in rows[1:]])) if len(rows) > 1 else 1.0
    add("mean edge join", join, join >= QA_MIN_JOIN, QA_MIN_JOIN)
    curve = float(np.mean([abs(r["energy_pct"] - r["target_energy"]) for r in rows]))
    add("energy-curve error", curve, curve <= QA_MAX_CURVE_ERR, QA_MAX_CURVE_ERR)
    add("peak", float(np.abs(y).max()), float(np.abs(y).max()) <= 0.98, 0.98)
    # only silence the mixing made: within 10 s of a join (a stop written into a song, e.g. before a drop, is music)
    joins = [r["position_s"] for r in rows[1:]]
    quiet = sum(db(m[i: i + sr]) < -45 for i in range(0, max(0, len(m) - 8 * sr), sr)
                if any(abs(i / sr - j) <= 10 for j in joins))
    add("dead-air seconds", quiet, quiet == 0, 0)
    return {"checks": checks, "pass": all(c["ok"] for c in checks),
            "worst_pair": max(bad, key=bad.get) if bad else None}


# ---------------------------------------------------------------- CLI

def describe(songs, plan):
    rows = []
    for n, e in enumerate(plan, 1):
        s = songs[e["song"]]
        t = e.get("position_s", e["planned_at_s"])
        rows.append({
            "n": n, "at": f"{int(t // 60)}:{int(t % 60):02d}", "position_s": round(t, 1),
            "file": s["file"], "name": s["name"], "mood": s["mood"],
            "excerpt_s": [e["window"]["start_s"], e["window"]["end_s"]],
            "window": {k: e["window"][k] for k in ("score", "in_q", "out_q", "in_vocal", "out_vocal")},
            "bpm": s["tempo"]["bpm"], "key": s["key"]["camelot"],
            "energy_pct": round(s["energy_pct"], 2), "target_energy": round(e["target_energy"], 2),
            "transition_in": e["transition_in"], "why": e["why"], "repeat": e["repeat"],
            "score": round(e["score"], 3), "match": round(pair_score(e["terms"]), 2), "vibe": round(e["terms"]["vibe"], 2), "terms": {k: round(v, 3) for k, v in e["terms"].items()},
            **({"transition_info": e["transition_info"]} if "transition_info" in e else {}),
        })
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("library", type=Path)
    ap.add_argument("--minutes", type=float, help="approximate mix length (default: from library size)")
    ap.add_argument("--profile", choices=PROFILES, default="house_party")
    ap.add_argument("--excerpt-seconds", type=float, default=EXCERPT_S)
    ap.add_argument("--start", help="open with the first song whose file name contains this text")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--min-match", type=float, default=MIN_MATCH,
                    help="only play B after A if their pair score is at least this (0 turns the cutoff off)")
    ap.add_argument("--min-vibe", type=float, default=MIN_VIBE,
                    help="only play B after A if their vibe match is at least this (0 turns it off)")
    ap.add_argument("--matrix", action="store_true", help="also write LIBRARY/_mixes/matrix.csv (pair quality)")
    ap.add_argument("--out", type=Path, help="output .wav (default LIBRARY/_mixes/mix.wav)")
    args = ap.parse_args(argv)

    library = args.library.resolve()
    songs, skipped = load_library(library)
    for f, why in skipped:
        print(f"skip  {f}: {why}")
    if not songs:
        print("no usable tagged songs; run tagger.py first")
        return 1

    excerpt_s = args.excerpt_seconds
    if args.minutes:
        target_s = args.minutes * 60
        # Small library, long mix: play longer excerpts before resorting to repeats.
        excerpt_s = max(excerpt_s, target_s / len(songs) + 15)
    else:
        n = max(2, round(len(songs) * DEFAULT_LIBRARY_SHARE))
        target_s = min(DEFAULT_MAX_MINUTES * 60, max(120, n * (excerpt_s - 15)))

    if args.matrix:
        mx = library / "_mixes" / "matrix.csv"
        mx.parent.mkdir(parents=True, exist_ok=True)
        best = write_matrix(songs, mx)
        print(f"matrix: {mx}")
        for name, top in list(best.items())[:5]:
            print(f"  {name[:34]:34s} -> " + " | ".join(f"{n[:22]} {v:.2f}" for v, n in top))

    out = args.out or library / "_mixes" / "mix.wav"
    banned, best = set(), None
    for attempt in range(1 if args.plan_only else MAX_ATTEMPTS):
        plan = plan_set(songs, target_s, args.profile, excerpt_s, args.start, frozenset(banned), args.min_match, args.min_vibe)
        if not plan:
            break
        if args.plan_only:
            best = (None, plan, describe(songs, plan), None, 0.0)
            break
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"{out.stem}.try{attempt}.wav")
        length = render(library, songs, plan, tmp)
        rows = describe(songs, plan)
        report = qa(tmp, rows)
        fails = [c for c in report["checks"] if not c["ok"]]
        print(f"attempt {attempt + 1}: {len(fails)} QA failure(s)" + "".join(f"\n    FAIL {c['check']} = {c['value']} (limit {c['limit']})" for c in fails))
        if best is None or len(fails) < len(best[3]["checks"]) - sum(c["ok"] for c in best[3]["checks"]):
            best = (tmp, plan, rows, report, length)
        if report["pass"] or report["worst_pair"] is None:
            break
        banned.add(report["worst_pair"])
        print(f"    banning pair: {report['worst_pair'][0][:30]} -> {report['worst_pair'][1][:30]}")
    if best is None:
        print("nothing to play (check --start)")
        return 1
    tmp, plan, rows, report, length = best

    planned_s = plan[-1]["planned_at_s"] + plan[-1]["window"]["end_s"] - plan[-1]["window"]["start_s"]
    if planned_s < 0.9 * target_s:
        print(f"note: library ran out at ~{planned_s / 60:.1f} of {target_s / 60:.1f} min")

    if not args.plan_only:
        tmp.replace(out)
        for extra in out.parent.glob(f"{out.stem}.try*.wav"):
            extra.unlink()
        out.with_suffix(".json").write_text(json.dumps(
            {"library": str(library), "profile": args.profile, "target_s": target_s,
             "length_s": round(length, 1), "qa": report, "tracks": rows}, indent=2, ensure_ascii=False))
        verdict = "PASS" if report["pass"] else "FAIL (best of %d attempts)" % MAX_ATTEMPTS
        print(f"\n{out}  {length / 60:.1f} min  ({len(plan)} excerpts)  QA {verdict}")
    else:
        print(f"\nplan only: ~{target_s / 60:.1f} min target, {len(plan)} excerpts, profile {args.profile}")

    print(f"\n {'#':>3} {'at':>6}  {'song':40s} {'bpm':>6} {'key':>4} {'E':>5} {'goal':>5} {'mood':12s} {'match':>5} {'vibe':>5} transition")
    for r in rows:
        rep = " (repeat)" if r["repeat"] else ""
        print(f" {r['n']:>3} {r['at']:>6}  {r['name'][:40]:40s} {r['bpm']:>6.1f} {r['key']:>4} "
              f"{r['energy_pct']:>5.2f} {r['target_energy']:>5.2f} {r['mood']:12s} {r['match']:>5.2f} {r['vibe']:>5.2f} {r['transition_in']}: {r['why']}{rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
