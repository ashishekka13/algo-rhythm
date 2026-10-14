"""AI MC: a short hype line over the music, now and then. Decides WHEN, WHAT and HOW it sounds; live.py mixes it in.

    m = mc.MC()                      # on by default; m.enabled = False turns it off
    m.warm(level)                    # background: line pool (Gemini once per party) + the next line's voice
    got = m.take(now_s, level)       # (line, audio) when one is due and rendered, else None
    x, at = mc.mix(music, segs, voice, sr)   # place it between sung lines, music ducked under it

WHEN: on a level change, or at most once every 20 minutes otherwise; never in the first 2 minutes; never two lines
within 10 minutes. WHAT: lines of at most 8 words per level (and a "build" set for a rise), written by Gemini once per
party in one call; built-in lines when Gemini is unavailable. HOW: speak() = Google Cloud Text-to-Speech when there are
credentials (env GOOGLE_TTS_API_KEY, or Application Default Credentials with MC_TTS_PROJECT), else macOS `say`, else silent.
"""
import base64
import functools
import io
import logging
import os
import re
import random
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np
import requests
import soundfile as sf
from scipy.signal import resample_poly

from algo_rhythm import dj, genai, transition
from algo_rhythm.live_plan import LEVELS

log = logging.getLogger("algo_rhythm.mc")
START_S = 120          # never in the first 2 minutes
EVERY_S = 20 * 60      # without a level change, at most once every 20 minutes
MIN_GAP_S = 10 * 60    # never two lines within 10 minutes, level change or not
MAX_WORDS = 8
MAX_LINE_S = 6.0       # a longer rendering is skipped
DUCK_DB = 8            # music dips this much under the voice
RAMP_S = 0.15          # duck ramps
PAD_IN_S = 0.2         # instrumental needed before the voice starts (covers the ramp down)
PAD_OUT_S = transition.CUT_GUARD_S  # ...and after it ends: clear of the breath before the next sung line
SEARCH_S = 30          # look for a gap this far past the start of the join
VOICE_OVER_DB = 0      # voice RMS relative to the music before ducking (so ~8 dB over the ducked music)
PROMPT_VERSION = "mc-v1"
KEYS = list(LEVELS) + ["build"]
SCHEMA = {"type": "OBJECT", "required": KEYS,
          "properties": {k: {"type": "ARRAY", "items": {"type": "STRING"}} for k in KEYS}}
PROMPT = """You are the MC at a live Bollywood party. Write 12 short hype lines for each key below, to be spoken by a
text-to-speech voice over the music between sung lines. Mix Hinglish and English. At most 8 words each.
No song lyrics, no song, film or artist names, no promises about which song comes next, nothing rude.
 low: warm and easy, the night is young or winding down. medium: groove, get moving.
 high: party, dance floor energy. max: peak time, hands up, loudest moment.
 build: the energy is rising right now, building toward the peak.
"""
TEMPLATES = {
    "low": ["Thoda relax karo, enjoy the vibe", "Easy now, the night is young", "Bas feel karo, slow and smooth",
            "Grab a drink, sit back, enjoy"],
    "medium": ["Chalo, thoda groove ho jaaye", "Feel that groove, get moving", "Ab mood ban raha hai",
               "Nice and steady, keep it flowing"],
    "high": ["Dance floor pe aa jao sab", "Party shuru ho gayi hai", "Let's go, everybody move",
             "Haath upar, energy upar"],
    "max": ["Hands up, this is peak time", "Pura dhamaka, full volume", "Ab rukna mana hai",
            "Make some noise, Bollywood"],
    "build": ["Energy badh rahi hai, ready ho jao", "Here we go, taking it higher", "Thoda aur upar chalte hain",
              "Building it up, stay with me"],
}
TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"


class MC:
    """One per party. Plain state: the scheduler, the line pool and the next rendered line. Thread-safe."""

    def __init__(self, enabled=True, *, generate=genai.generate, ai=None, speak_fn=None, rng=None, titles=()):
        self.titles = song_titles(titles)
        self.enabled, self.generate, self.ai = enabled, generate, ai or {}
        self.speak = speak_fn or speak
        self.rng = rng or random.Random()
        self.lock = threading.Lock()
        self.pool, self.used, self.ready, self.job = None, set(), None, None  # ready = (key, line, audio)
        self.level, self.prev, self.changed = None, None, False
        self.last_line, self.last_at = None, None

    # -- WHEN
    def note_level(self, level):
        """Track level changes between checks. A rise is remembered so the line can be a 'build' line."""
        with self.lock:
            if self.level is not None and level != self.level:
                self.changed, self.prev = True, self.level
            self.level = level

    def next_eligible(self):
        """Earliest session second a line may play (a level change can use it; otherwise the 20-minute rule)."""
        floor = START_S if self.last_at is None else self.last_at + MIN_GAP_S
        return floor if self.changed else max(floor, (self.last_at or 0) + EVERY_S)

    def due(self, now_s, level):
        self.note_level(level)
        return self.enabled and now_s >= self.next_eligible()

    def key(self, level):
        """Which pool: "build" right after a rise (e.g. building to max), else the level's own."""
        rise = self.changed and self.prev in LEVELS and LEVELS[level] > LEVELS[self.prev]
        return "build" if rise else level

    # -- WHAT and HOW, in the background
    def lines(self):
        """The line pool, made once: Gemini (one call) or the built-in templates."""
        if self.pool is None:
            self.pool = make_pool(self.generate, titles=self.titles, **self.ai)
        return self.pool

    def warm(self, level, wait=False):
        """Make sure the next line for `level` is rendered or rendering, in a background thread. Never blocks unless
        wait=True (the test sink writes faster than real time)."""
        if not self.enabled:
            return
        self.note_level(level)
        with self.lock:
            k = self.key(level)
            if (self.ready and self.ready[0] == k) or (self.job and self.job.is_alive()):
                job = self.job
            else:
                job = self.job = threading.Thread(target=self._render, args=(k,), daemon=True)
                job.start()
        if wait and job:
            job.join()

    def _render(self, k):
        try:
            pool = self.lines()[k]
            fresh = [x for x in pool if x not in self.used] or pool
            for line in self.rng.sample(fresh, len(fresh)):
                audio = self.speak(line)
                if audio is not None and len(audio) <= MAX_LINE_S * dj.OUT_SR:
                    with self.lock:
                        self.ready = (k, line, audio)
                    return
        except Exception:
            log.exception("MC could not prepare a line")

    def take(self, now_s, level, wait=False):
        """(line, audio) if a line is due now and its voice is ready for this level; else None (and get one ready)."""
        if not self.due(now_s, level):
            return None
        self.warm(level, wait)
        with self.lock:
            if not self.ready or self.ready[0] != self.key(level):
                return None
            return self.ready[1], self.ready[2]

    def spoke(self, line, now_s):
        """The line was mixed in: start the clocks again."""
        with self.lock:
            self.used.add(line)
            self.last_line, self.last_at, self.changed, self.ready = line, now_s, False, None

    def reset(self):
        with self.lock:
            self.last_at, self.changed, self.level, self.ready = None, False, None, None

    def view(self):
        return {"enabled": self.enabled, "last_line": self.last_line, "last_at_s": self.last_at,
                "next_eligible_s": round(self.next_eligible())}


def plain(text):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split())


def song_titles(names):
    """Song titles to keep out of MC lines: each name's part before " - ", without a [youtube id]; 4+ letters."""
    out = {plain(re.sub(r"\[[^\]]*\]", "", n).replace("_", " ").split(" - ")[0]) for n in names}
    return {t for t in out if len(t) >= 4}


def clean_line(x, titles=()):
    """A usable hype line, or None: at most MAX_WORDS words, and no song title (the MC never names songs)."""
    x = " ".join(str(x).replace('"', "").split())
    if any(f" {t} " in f" {plain(x)} " for t in titles):
        return None
    return x if 0 < len(x.split()) <= MAX_WORDS and len(x) <= 80 else None


def make_pool(generate=genai.generate, titles=(), **ai):
    """{key: [lines]} from one Gemini call; any key with fewer than 3 usable lines takes the built-in ones."""
    try:
        got = generate("mc", PROMPT_VERSION, [{"text": PROMPT}], SCHEMA, **ai)
        source = "gemini"
    except genai.GenAIError as e:
        log.info("MC uses built-in lines: %s", e)
        got, source = {}, "templates"
    pool = {}
    for k in KEYS:
        lines = list(dict.fromkeys(filter(None, (clean_line(x, titles) for x in got.get(k) or []))))
        pool[k] = lines if len(lines) >= 3 else TEMPLATES[k]
    log.info("MC line pool ready (%s)", source)
    return pool


# ---------------------------------------------------------------- text to speech

def to_out(audio, sr):
    """Any mono/stereo rendering -> float32 stereo at dj.OUT_SR, silence trimmed from both ends."""
    a = np.asarray(audio, dtype=np.float64)
    a = a.mean(axis=1) if a.ndim > 1 else a
    if sr != dj.OUT_SR:
        g = np.gcd(int(sr), dj.OUT_SR)
        a = resample_poly(a, dj.OUT_SR // g, int(sr) // g)
    loud = np.flatnonzero(np.abs(a) > 10 ** (-45 / 20) * max(np.abs(a).max(), 1e-9))
    if not len(loud):
        return None
    a = a[loud[0]:loud[-1] + 1]
    return np.repeat(a[:, None], 2, axis=1).astype(np.float32)


def cloud_headers():
    """Auth headers for Cloud Text-to-Speech, or None. Opt-in only: env GOOGLE_TTS_API_KEY, or Application Default
    Credentials when MC_TTS_PROJECT names the GCP project to bill. Machine-wide credentials are never picked up on
    their own: they may belong to another (e.g. work) project."""
    key = os.environ.get("GOOGLE_TTS_API_KEY")
    if key:
        return {"x-goog-api-key": key}
    project = os.environ.get("MC_TTS_PROJECT")
    if not project:
        return None
    try:
        import google.auth
        import google.auth.transport.requests
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
    except Exception:
        return None
    return {"Authorization": f"Bearer {creds.token}", "x-goog-user-project": project}


def cloud_tts(text, headers, post=requests.post):
    """Google Cloud Text-to-Speech REST (LINEAR16 WAV). Voice: env MC_CLOUD_VOICE (default en-IN-Wavenet-C; hi-IN-... works)."""
    voice = os.environ.get("MC_CLOUD_VOICE", "en-IN-Wavenet-C")
    body = {"input": {"text": text}, "voice": {"languageCode": voice[:5], "name": voice},
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000, "speakingRate": 1.05}}
    r = post(TTS_URL, headers=headers, json=body, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Cloud Text-to-Speech answered HTTP {r.status_code}")
    audio, sr = sf.read(io.BytesIO(base64.b64decode(r.json()["audioContent"])), always_2d=True)
    return to_out(audio, sr)


@functools.lru_cache(maxsize=1)
def say_voice():
    """An Indian English voice if installed (Rishi, Veena, ...), else None (the system default)."""
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names = [ln.split("  ")[0].strip() for ln in out.splitlines() if "en_IN" in ln]
    if os.environ.get("MC_SAY_VOICE"):
        return os.environ["MC_SAY_VOICE"]
    return next((v for v in ("Rishi", "Veena") if v in names), names[0] if names else None)


def say_tts(text):
    """macOS `say` into a temp AIFF (never out loud), read back. None when not on macOS."""
    if sys.platform != "darwin" or not shutil.which("say"):
        return None
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "line.txt", Path(d) / "line.aiff"
        src.write_text(text)  # read from a file: the line is never parsed as a command-line option
        cmd = ["say", "-o", str(out), "-f", str(src)] + (["-v", say_voice()] if say_voice() else [])
        subprocess.run(cmd, check=True, timeout=30, capture_output=True)
        audio, sr = sf.read(out, always_2d=True)
    return to_out(audio, sr)


_cloud = {"off": False}


GEMINI_TTS_MODEL = "gemini-2.5-flash-tts"
MC_STYLE = "Say it like an energetic Bollywood wedding DJ hyping up the crowd, with an Indian accent: "


def gemini_tts(text):
    """Gemini text-to-speech through the gateway's endpoint and key (works with the Vertex AI key). Voice: env MC_VOICE
    (a Gemini voice: Puck, Fenrir, Charon, Kore, Aoede, Zephyr...; default Aoede). 24 kHz 16-bit mono PCM back."""
    body = {"contents": [{"role": "user", "parts": [{"text": os.environ.get("MC_STYLE", MC_STYLE) + text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": os.environ.get("MC_VOICE", "Aoede")}}}}}
    j = genai._send(genai.model_url(os.environ.get("MC_TTS_MODEL", GEMINI_TTS_MODEL), "generateContent"), body, 90)
    data = j["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    return to_out(np.frombuffer(base64.b64decode(data), "<i2").astype(np.float32) / 32768, 24000)


@functools.lru_cache(maxsize=128)
def speak(text):
    """text -> float32 stereo audio at dj.OUT_SR, or None (MC silent). Engine: env MC_TTS = gemini (default) | cloud |
    say. Gemini or Cloud first; a failure turns that engine off for the run and the Mac voice takes over."""
    engine = os.environ.get("MC_TTS", "gemini")
    if engine == "gemini" and not _cloud["off"]:
        try:
            return gemini_tts(text)
        except Exception as e:
            log.warning("Gemini voice failed, using the local voice from now on: %s", e)
            _cloud["off"] = True
    if engine == "cloud" and not _cloud["off"]:
        h = cloud_headers()
        if h:
            try:
                return cloud_tts(text, h)
            except Exception as e:
                log.warning("Cloud Text-to-Speech failed, using the local voice from now on: %s", e)
        _cloud["off"] = True
    try:
        return say_tts(text)
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        log.warning("local voice failed: %s", e)
        return None


# ---------------------------------------------------------------- placement and mixing

def find_gap(segs, need_s, start_s=0.0, end_s=SEARCH_S):
    """Earliest time in [start_s, end_s] where `need_s` seconds hold no sung segment, with PAD_IN_S before and
    PAD_OUT_S after. Returns where the voice starts, or None."""
    t = start_s + PAD_IN_S
    for a, b in sorted(segs) + [(1e9, 1e9)]:
        if b <= t - PAD_IN_S:
            continue
        if a - PAD_OUT_S >= t + need_s and t + need_s + PAD_OUT_S <= end_s:
            return t
        t = max(t, b + PAD_IN_S)
        if t > end_s:
            return None
    return None


def mix(music, segs, voice, sr, start_s=0.0, end_s=SEARCH_S):
    """Put `voice` over the first instrumental stretch of `music` (sung segments `segs`, in seconds of `music`),
    with the music ducked DUCK_DB under it. Returns (new music, voice start in seconds) or (music, None)."""
    end_s = min(end_s, len(music) / sr)
    at = find_gap(segs, len(voice) / sr, start_s, end_s)
    if at is None:
        return music, None
    i, n, r = int(at * sr), len(voice), int(RAMP_S * sr)
    t = np.arange(len(music))
    depth = 1 - 10 ** (-DUCK_DB / 20)
    g = 1 - depth * np.clip(np.minimum((t - (i - r)) / r, (i + n + r - t) / r), 0, 1)
    rms = lambda x: np.sqrt(np.mean(np.square(x))) + 1e-9
    ref = rms(music[:int(end_s * sr)])  # the music's usual level here, not just the (maybe quiet) gap
    v = voice * (ref * 10 ** (VOICE_OVER_DB / 20) / rms(voice))
    v *= min(1.0, 0.9 / (np.abs(v).max() + 1e-9))
    out = music * g[:, None]
    out[i:i + n] += v
    return out, at
