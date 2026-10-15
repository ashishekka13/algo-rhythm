"""Live interactive DJ: the session (what plays next), the engine (renders each join ~10 s ahead) and the sinks.

  python -m algo_rhythm.server LIBRARY                  the player: web page at http://127.0.0.1:8765, sound from this laptop
  python -m algo_rhythm.live LIBRARY --script events.json --seconds 600 --out session.wav
                                                 test mode: no speakers, runs faster than real time

How it stays live: the engine always keeps the NEXT transition rendered ahead of the audio
(~10 s buffer). Songs already rendered are locked. Feedback and requests re-plan only the
songs after them, so a change is heard about 2 songs later, and a request lands 1-3 songs out.

events.json (test mode): [{"t": 60, "vibe": "max"}, {"t": 120, "request": "Kesariya"}]
"""
import argparse
import collections
import json
import logging
import random
import sys
import threading
import time
from scipy.signal import butter, sosfilt
from pathlib import Path

import numpy as np
import soundfile as sf

from algo_rhythm import dj
from algo_rhythm import filler
from algo_rhythm import live_plan
from algo_rhythm import mc
from algo_rhythm import tagger
from algo_rhythm import transition

API_VERSION = 3              # the page compares this with its own; a mismatch means "reload the page"
AHEAD_S = 10                 # audio rendered ahead of the speakers; transitions are prepared inside this
EXCERPT_S = 80   # preferred length only; each song's window is free between dj.FLEX_MIN_S and FLEX_MAX_S
STATIC = Path(__file__).parent / "static" / "live.html"
OWN_DIP_MARGIN_DB = 2  # a blend's hole must be this much deeper than the outgoing song's own quietest moment
ECHO_MAX_HOLD_S = 8  # an echo-out may hold the incoming singer back this long to enter on a line start
ECHO_LOOK_BEATS = 16  # an echo-out cuts within the outgoing song's last 16 beats (twice that only if nothing is clean there)
ECHO_HOLD_MAX_BEATS = 8  # holding the incoming singer back longer than 2 bars sounds stretched: start the song later
ECHO_LEAD_BEATS = 4  # ...on a bar line about 1 bar before its singer's line, so the music leads in briefly
SKIP_LEAD_S = 10  # "skip to transition" lands this long before the join
IDLE_CLOSE_S = 150  # no page has asked for anything this long (a background tab polls about once a minute): stop
BYE_GRACE_S = 5  # the tab closed: stop unless a page comes back this soon (a reload)
EARLY_MIN_MATCH = 0.6     # a request jumps ahead of the locked next song only if it joins the song on air this well
EARLY_MIN_LEFT_S = 25     # ...and only if this much of the song on air is still to come (time to render the new join)
CLIP_EDGE_S = 8           # a join's clip: this much before the overlap, the overlap, this much after
MIX_FEEDBACK_S = 30       # guests may rate a mix while it plays and this long after it ends


# ---------------------------------------------------------------- sinks

class SpeakerSink:
    """Plays through sounddevice. push() blocks while more than AHEAD_S is already buffered."""

    def __init__(self, sr):
        import sounddevice as sd
        self.sr, self.written, self.played, self.buffered, self.underruns = sr, 0, 0, 0, 0
        self.paused = False
        self.chunks, self.offset, self.cond = collections.deque(), 0, threading.Condition()
        self.stream = sd.OutputStream(samplerate=sr, channels=2, dtype="float32", callback=self._callback)
        self.stream.start()

    def _callback(self, outdata, frames, time_info, status):
        with self.cond:
            outdata.fill(0)
            if self.paused:  # silence, and keep our place: nothing is consumed or counted
                return
            got = 0
            while got < frames and self.chunks:
                c = self.chunks[0]
                n = min(frames - got, len(c) - self.offset)
                outdata[got:got + n] = c[self.offset:self.offset + n]
                got, self.offset = got + n, self.offset + n
                if self.offset >= len(c):
                    self.chunks.popleft()
                    self.offset = 0
            if got < frames and self.written:
                self.underruns += 1
            self.played += got
            self.buffered -= got
            self.cond.notify_all()

    def push(self, x, stop):
        with self.cond:
            while self.buffered > AHEAD_S * self.sr and not stop.is_set():
                self.cond.wait(0.2)
            self.chunks.append(x.astype(np.float32))
            self.buffered += len(x)
            self.written += len(x)

    def playhead(self):
        return self.played / self.sr

    def drop_buffered(self):
        """Skip: forget the audio queued but not yet heard; the write position steps back to what was heard."""
        with self.cond:
            self.chunks.clear()
            self.offset = 0
            self.written -= self.buffered
            self.buffered = 0
            self.cond.notify_all()

    def reset(self):
        """Start over: drop everything buffered and count from zero again."""
        with self.cond:
            self.chunks.clear()
            self.offset = self.written = self.played = self.buffered = self.underruns = 0
            self.paused = False
            self.cond.notify_all()

    def close(self):
        self.stream.stop()
        self.stream.close()


class FileSink:
    """Test sink: writes a wav as fast as it can; the 'playhead' trails the write position by AHEAD_S."""
    realtime = False

    def __init__(self, sr, path):
        self.sr, self.written = sr, 0
        self.out = sf.SoundFile(path, "w", sr, 2, subtype="PCM_16")

    def push(self, x, stop):
        self.out.write(x)
        self.written += len(x)

    def playhead(self):
        return max(0.0, self.written / self.sr - AHEAD_S)

    def close(self):
        self.out.close()


# ---------------------------------------------------------------- session state

def energy_band(pct):
    """The same four names as the level buttons: low / medium / high / max (library energy quartiles)."""
    return ("low", "medium", "high", "max")[min(3, int(pct * 4))]


class Session:
    """Everything the planner and the web page share. All access goes through `lock`."""

    def __init__(self, songs, planner, level, library, history_path):
        self.lock = threading.RLock()
        self.songs, self.planner, self.level = songs, planner, level
        self.library, self.history_path = library, history_path
        self.counts = self._load_history()       # file -> times played, kept across restarts so songs stay fresh
        self.history = {}  # file -> plays in the host's earlier parties (the web app; counts holds this party's own)
        self.seed = None   # the party's random seed: varies the opener and near-tie picks (None: always the best)
        self.by_file = {s["file"]: i for i, s in enumerate(songs)}
        self.queue, self.timeline, self.played, self.requests = [], [], [], []
        self.deadline, self._logged = {}, None   # request song -> last timeline index it may play at
        self.early = None                        # a request the engine may swap in for the locked next song
        self.log = collections.deque(maxlen=12)
        self.on_play = None  # called with (entry, transition info) when a song becomes audible
        self.art = {}  # art id -> image file name (algo_rhythm.art index); empty = no song art yet
        self.positions = 0   # plays so far; each audible entry gets the next number as entry["position"]
        self.clip_dir = None  # folder for each join's FLAC clip (None = no clips)
        self.on_clip = None   # called with (position, clip path) once a clip is written
        self.mc = None       # the AI MC (algo_rhythm.mc.MC) when there is one
        self.playhead = lambda: 0.0

    # -- play history
    def _load_history(self):
        try:
            return {k: int(v) for k, v in json.loads(self.history_path.read_text()).items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_history(self):
        if self.history_path is None:  # the web player keeps the counts in the party's state instead
            return
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.history_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.counts))
            tmp.replace(self.history_path)
        except OSError:
            pass  # history is a convenience; never let it stop the music

    def _played(self, i):
        self.played.append(i)
        self.counts[self.songs[i]["file"]] = self.counts.get(self.songs[i]["file"], 0) + 1
        self._save_history()

    def _penalties(self):
        """Songs heard before lose points, more for repeats and for very recent ones. Fresh songs win ties."""
        recent = set(self.played[-30:])
        pen = {}
        for i, s in enumerate(self.songs):
            n = self.counts.get(s["file"], 0) + self.history.get(s["file"], 0)
            if n:
                pen[i] = min(3.0, 1.5 + 0.5 * (n - 1)) + (0.5 if i in recent else 0.0)
        return pen

    def reset(self):
        """Start over from the beginning: no song is playing, the level stays, the play history is kept (songs stay fresh)."""
        with self.lock:
            self.queue, self.timeline, self.requests = [], [], []
            self.deadline, self._logged, self.early = {}, None, None
            if self.mc:
                self.mc.reset()
            self._say("started over: pick a level and press Play")

    # -- helpers (call with the lock held)
    def played_hook(self, entry, info):
        """Tell `on_play` (the server's database log) that a song became audible; a failure never stops the music."""
        with self.lock:
            self.positions += 1
            entry["position"] = self.positions
        if self.on_play is None:
            return
        try:
            self.on_play(entry, info)
        except Exception:
            logging.getLogger("algo_rhythm.session").exception("could not record the play")

    def _say(self, msg):
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        logging.getLogger("algo_rhythm.session").info(msg)

    def _last(self):
        return self.timeline[-1]["entry"]

    def _plan(self):
        if not self.timeline:  # nothing has started yet; the level and requests are kept for when it does
            return
        last = self._last()
        avoid = set(self.played[-12:])  # never right after itself; older plays are only a penalty
        req = self.requests[0] if self.requests else None
        slots = [p for p in live_plan.REQUEST_SLOTS if len(self.timeline) + p - 1 <= self.deadline.get(req, 0)] or [1]
        self.planner.recent = [t["entry"]["song"] for t in self.timeline]
        # the same inputs give the same pick, so re-planning does not reshuffle what the page shows as next
        self.planner.rng = random.Random(f"{self.seed}:{len(self.timeline)}:{last['song']}:{self.level}:{self.planner.mood}") \
            if self.seed else None
        self.queue = self.planner.plan(last["song"], last["window"], avoid, live_plan.LEVELS[self.level], req, slots,
                                       penalty=self._penalties())
        if not self.queue:  # every song that could follow was heard lately: only the last two are off limits now
            self.queue = self.planner.plan(last["song"], last["window"], set(self.played[-2:]), live_plan.LEVELS[self.level],
                                           req, slots, penalty=self._penalties())
        for p, e in enumerate(self.queue):
            if e["request"]:
                locked = sum(t["start_s"] > self.playhead() for t in self.timeline)
                seen = (req, p, e["transition_in"])
                if seen != self._logged:  # say it once per change, not on every re-plan
                    self._logged = seen
                    self._say(f"request '{self.songs[req]['name']}' is song #{locked + p + 1} from now "
                              f"(match {e['match']:.2f}, {e['transition_in']})")

    # -- engine side
    def start(self, start_name):
        with self.lock:
            self.planner.rng = random.Random(f"{self.seed}:open:{self.level}:{self.planner.mood}") if self.seed else None
            e = self.planner.first_song(live_plan.LEVELS[self.level], start_name, self._penalties())
            self.timeline.append({"entry": e, "start_s": 0.0})
            self._played(e["song"])
            self._plan()
            return e

    def commit_next(self, start_s):
        """The engine takes the first planned song; from here it is locked."""
        with self.lock:
            if not self.queue:
                self._plan()
            e = self.queue.pop(0)
            # start_s is unknown until the transition is rendered; inf = "not on the air yet"
            self.timeline.append({"entry": e, "start_s": float("inf"), "overlap_s": 0.0})
            self._played(e["song"])
            if e["request"] and e["song"] in self.requests:
                self.requests.remove(e["song"])
            self._plan()
            return e

    # -- listener side
    def set_level(self, name):
        with self.lock:
            if name not in live_plan.LEVELS:
                raise ValueError("unknown level")
            self.level = name
            self._say(f"vibe set to {name}")
            self._plan()

    def set_vibe(self, name):
        """One of live_plan.VIBES: sets the energy level and the mood together."""
        with self.lock:
            if name not in live_plan.VIBES:
                raise ValueError("unknown vibe")
            self.level, mood = live_plan.VIBES[name]
            self.planner.mood = mood
            self._say(f"vibe set to {name}")
            self._plan()

    def vibe(self):
        """The VIBES name matching the current level and mood, or None (e.g. Ask the DJ set an odd pair)."""
        return next((n for n, (lv, m) in live_plan.VIBES.items() if lv == self.level and m == self.planner.mood), None)

    def set_mood(self, name):
        """auto = keep whatever mood the room is in; a family = hold that mood, like the energy level."""
        with self.lock:
            if name not in live_plan.MOODS:
                raise ValueError("unknown mood")
            self.planner.mood = None if name == "auto" else name
            self._say(f"mood set to {name}")
            self._plan()

    def request(self, file):
        with self.lock:
            i = self.by_file.get(file)
            if i is None:
                raise ValueError("unknown song")
            now = [t["entry"]["song"] for t in self.timeline if t["start_s"] + 2 >= self.playhead()][-2:]
            if i in self.requests or i in now:
                self._say(f"'{self.songs[i]['name']}' is already requested or about to play")
                return
            self.requests.append(i)
            self.deadline[i] = len(self.timeline) + max(live_plan.REQUEST_SLOTS) - 1
            self._plan()
            self._try_early(i)

    def _try_early(self, i):
        """The song after the one on air is locked (already rendered) but not audible yet. If the request joins the song
        on air well, offer it to the engine to take that place, so it plays next instead of two songs from now."""
        if len(self.timeline) < 2 or self.timeline[-1]["start_s"] != float("inf"):
            return
        on_air = self.timeline[-2]["entry"]
        avoid = set(self.played[-12:]) - {i}
        chain = self.planner.plan(on_air["song"], on_air["window"], avoid, live_plan.LEVELS[self.level], i, (1,),
                                  h=1, penalty=self._penalties())
        if chain and chain[0]["request"] and (chain[0]["transition_in"] == "blend" or chain[0]["match"] >= EARLY_MIN_MATCH):
            self.early = chain[0]

    def swap_next(self, new, old):
        """The engine rendered `new` in place of the locked `old`: the request plays next."""
        with self.lock:
            self.early = None
            if self.timeline[-1]["entry"] is not old:
                return False
            self.timeline[-1]["entry"] = new
            if self.played and self.played[-1] == old["song"]:
                self.played.pop()
                f = self.songs[old["song"]]["file"]
                self.counts[f] = max(0, self.counts.get(f, 1) - 1)
            self._played(new["song"])
            if new["song"] in self.requests:
                self.requests.remove(new["song"])
            self._say(f"request '{self.songs[new['song']]['name']}' moved up: it plays next (match {new['match']:.2f})")
            self._plan()
            return True

    # -- songs that are not in the library yet
    def _entry_for_test(self, i):
        """A plain planner entry for song i (used by check_live to build a fake timeline)."""
        return self.planner.entry(i, self.planner.win[i][0], {}, "blend", "test", 0.7, 0.0)

    def _now_index(self, ph):
        """The timeline index of the loudest song: it takes over at the MIDDLE of its transition."""
        mid = lambda t: t["start_s"] + t["overlap_s"] / 2 if "overlap_s" in t else t["start_s"]
        return max((i for i, t in enumerate(self.timeline) if mid(t) <= ph), default=None)

    def feedback_targets(self):
        """What a guest vote attaches to: the play position of the song on air, and of the latest join that has started
        (mix_open while it plays and for MIX_FEEDBACK_S after it ends)."""
        with self.lock:
            ph, tl = self.playhead(), self.timeline
            idx = self._now_index(ph)
            joins = [t for t in tl[1:] if t["start_s"] <= ph]
            j = joins[-1] if joins else None
            return {"song": tl[idx]["entry"].get("position") if idx is not None else None,
                    "mix": j["entry"].get("position") if j else None,
                    "mix_open": bool(j) and ph <= j["start_s"] + j["overlap_s"] + MIX_FEEDBACK_S}

    def view(self, status="playing"):
        """What the page shows. 'Now playing' is the song that is loudest: it changes at the MIDDLE of a transition,
        not at its start, and an active transition is reported separately."""
        with self.lock:
            ph, tl = self.playhead(), self.timeline
            idx = self._now_index(ph)
            now = tl[idx] if idx is not None else None
            mixing = None
            for j in range(1, len(tl)):
                t = tl[j]
                if t["start_s"] <= ph < t["start_s"] + t["overlap_s"]:
                    a, b = self.songs[tl[j - 1]["entry"]["song"]], self.songs[t["entry"]["song"]]
                    mixing = {"from": a["name"], "to": b["name"], "kind": t["entry"]["transition_in"],
                              "progress": round((ph - t["start_s"]) / max(t["overlap_s"], 0.1), 2),
                              "from_art": self.art_url(a), "to_art": self.art_url(b)}
            coming = [t["entry"] for j, t in enumerate(tl) if j > (idx if idx is not None else -1) and t["start_s"] > ph]
            return {
                "api": API_VERSION, "status": status,
                "level": self.level, "levels": list(live_plan.LEVELS),
                "mood": self.planner.mood or "auto", "moods": list(live_plan.MOODS),
                "vibe": self.vibe(), "vibes": {n: lv for n, (lv, _) in live_plan.VIBES.items()},
                "now": self._view_entry(now["entry"]) if now else None,
                "position_s": round(max(0.0, ph - now["start_s"]), 1) if now else 0,
                "length_s": round(now["entry"]["window"]["end_s"] - now["entry"]["window"]["start_s"], 1) if now else 0,
                "mixing": mixing,
                "next": [self._view_entry(e, "locked") for e in coming] + [self._view_entry(e) for e in self.queue],
                "requests": [self.songs[i]["name"] for i in self.requests],
                "history": [self.songs[i]["name"] for i in self.played[-10:]][::-1],
                "log": list(self.log)[::-1],
            }

    def _view_entry(self, e, status="planned"):
        s = self.songs[e["song"]]
        return {"name": s["name"], "bpm": round(s["tempo"]["bpm"]), "key": s["key"]["camelot"],
                "energy": energy_band(s["energy_pct"]), "mood": s["mood"], "kind": e["transition_in"],
                "match": e["match"], "request": e["request"], "status": status, "explain": self._explain(e, s),
                "art": self.art_url(s)}

    def art_url(self, s):
        """The song's generated image (algo_rhythm.art), or None: the page then keeps its own generated visual."""
        name = self.art.get(s["sha256"][:16])
        return f"/art/{name}" if name else None

    def _explain(self, e, s):
        """Why the planner chose this song, for the page's "under the hood" view. Numbers are 0..1 unless noted."""
        w, llm = e["window"], s.get("llm")
        return {
            "why": e["why"], "score": round(e["score"], 2),
            "terms": {k: round(float(v), 2) for k, v in e["terms"].items()},
            "energy": round(s["energy_pct"], 2), "target": live_plan.LEVELS[self.level],
            "plays": self.counts.get(s["file"], 0), "penalty": round(self._penalties().get(e["song"], 0.0), 1),
            "window": {"from_s": round(w["start_s"]), "to_s": round(w["end_s"]), "from_start": bool(w["natural_start"]),
                       "vocal_swap": bool(w["stems"])},
            "gemini": {"energy": llm["energy"], "confidence": llm["confidence"], "occasion": llm["occasion"],
                       "familiarity": llm["familiarity"], "mood": dj.mood_family(llm)} if llm else None,
            "alts": [{"name": self.songs[a["song"]]["name"], "behind": a["behind"]} for a in e.get("alts", [])],
        }


# ---------------------------------------------------------------- engine

def gain_curve_params(tail, head, sr, win_s, ramp_s, limit_db, hold_s=0):
    """DJ gain staging: (n, d_db, n_hold, n_ramp) to start the incoming excerpt d_db off and ease back, or None."""
    db = lambda x: 20 * np.log10(np.sqrt(np.mean(x.mean(axis=1) ** 2)) + 1e-9)
    w = int(win_s * sr)
    d = float(np.clip(db(tail[-w:]) - db(head[:w]), -limit_db, limit_db))
    n_hold, n_ramp = int(hold_s * sr), int(ramp_s * sr)
    n = min(len(head), n_hold + n_ramp)
    peak = float(np.abs(head[:n]).max()) if n else 0.0
    if d > 0 and peak > 0:
        d = min(d, 20 * np.log10(1.5 / peak))  # the soft limiter absorbs peaks up to ~1.5; lift no further than that
    if abs(d) < 1:
        return None
    return n, d, n_hold, n_ramp


def stage_gain(n, d, n_hold, n_ramp):
    """The gain of gain_curve_params' answer, sample by sample: d_db held for n_hold, then eased to 0 dB over n_ramp."""
    curve = np.concatenate([np.ones(n_hold), 1 - np.linspace(0, 1, n_ramp)])[:n]  # hold at full lift, then ease out
    return 10 ** (d * curve / 20)


def gain_curve(tail, head, sr, win_s, ramp_s, limit_db, hold_s=0):
    """DJ gain staging: (n, gain) to start the incoming excerpt at the outgoing one's level and ease back, or None."""
    p = gain_curve_params(tail, head, sr, win_s, ramp_s, limit_db, hold_s)
    return None if p is None else (p[0], stage_gain(*p))


def trim_quiet_head(tail, head, beats, sr, drop_db=10, max_beats=16, extras=(), plan=None):
    """Start the incoming excerpt on the first beat that is not far quieter than where the outgoing one ends.
    `extras` (e.g. its vocals) are cut identically so they stay in step. Returns (head, beats, extras).
    plan: a dict that gets "trim": [beats dropped, samples dropped] (the web player replays the same cut)."""
    if plan is not None:
        plan["trim"] = [0, 0]
    db = lambda x: 20 * np.log10(np.sqrt(np.mean(x.mean(axis=1) ** 2)) + 1e-9)
    ref = db(tail[-3 * sr:])
    for k in range(min(max_beats, len(beats) - 2)):
        if db(head[int(beats[k] * sr):int(beats[k + 1] * sr)]) >= ref - drop_db:
            break
    else:
        return head, beats, extras  # never gets loud enough in the first beats: leave it alone
    if k == 0:
        return head, beats, extras
    off = int(round(beats[k] * sr))
    trimmed = head[off:]
    new = beats[k:] - off / sr
    new[-1] = len(trimmed) / sr  # the stretcher needs the last beat to sit exactly on the last sample
    if plan is not None:
        plan["trim"] = [int(k), off]
    return trimmed, new, tuple(x[off:] for x in extras)


def stage_both(tail, head, sr, extras=(), plan=None):
    """Slow match over the first 20 s (+-4 dB), then hold a quiet opening at the outgoing level for 3 s (up to +10 dB).
    The same gain is applied to `extras`. Returns (head, extras).
    plan: a dict that gets "stage": the gain_curve_params of each pass that changed something, in order."""
    if plan is not None:
        plan["stage"] = []
    for win_s, ramp_s, limit_db, hold_s in ((8, 20, 4, 0), (3, 4, 10, 3)):
        p = gain_curve_params(tail, head, sr, win_s, ramp_s, limit_db, hold_s)
        if p is not None:
            if plan is not None:
                plan["stage"].append([int(p[0]), float(p[1]), int(p[2]), int(p[3])])
            n, g = p[0], stage_gain(*p)
            head = head.copy()
            head[:n] *= g[:, None]
            extras = tuple(x.copy() for x in extras)
            for x in extras:
                x[:n] *= g[:, None]
    return head, extras


def level_swap(before, overlap, nxt, nxt_skip, sr, max_lift_db=4, plan=None):
    """Taking the voices out of the middle of a blend lowers the level (the voice is the loudest part). Lift the
    overlap back toward the level of the music either side, smoothly and never by more than max_lift_db.
    plan: a dict that gets "lift": [[seconds into the overlap, dB], ...], linear in dB between the points."""
    db = lambda x: 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)
    mono = lambda x: x.mean(axis=1)
    target = (db(mono(before[-6 * sr:])) + db(mono(nxt[nxt_skip:nxt_skip + 6 * sr]))) / 2
    w = sr // 2
    env = np.array([db(mono(overlap[i:i + w])) for i in range(0, len(overlap), w)])
    lift = np.clip(target - env, 0, max_lift_db)
    lift = np.convolve(np.pad(lift, 2, mode="edge"), np.ones(5) / 5, mode="valid")  # ~2.5 s smoothing: no pumping
    centres = (np.arange(len(env)) + 0.5) * w
    curve = np.interp(np.arange(len(overlap)), centres, lift)
    if plan is not None:
        plan["lift"] = [[round(float(c / sr), 4), round(float(v), 3)] for c, v in zip(centres, lift)]
    return overlap * (10 ** (curve / 20))[:, None]


def dip_db(before, overlap, nxt, nxt_skip, sr):
    """Deepest drop (dB) of any 0.5 s window below the median level of the 6 s either side of the join."""
    x = np.concatenate([before[-6 * sr:], overlap, nxt[nxt_skip:nxt_skip + 6 * sr]]).mean(axis=1)
    w = sr // 2
    lv = np.array([20 * np.log10(np.sqrt(np.mean(x[i:i + w] ** 2)) + 1e-9) for i in range(0, len(x) - w, w)])
    return float(np.median(lv) - lv.min())


def echo_bass_pile_db(before, overlap, nxt, nxt_skip, sr, beat_s):
    """Bass of an echo-out's first 4 beats above the louder of the 8 s either side: the final QA check's windows.
    The echo itself is high-passed, so a pile-up here is the incoming song's own opening bass: try another part."""
    x = np.concatenate([before[-8 * sr:], overlap, nxt[nxt_skip:nxt_skip + 10 * sr]]).mean(axis=1)
    low = sosfilt(butter(4, 150, "lowpass", fs=sr, output="sos"), x)
    db = lambda v: 20 * np.log10(np.sqrt(np.mean(v ** 2)) + 1e-9)
    t0 = min(8 * sr, len(before)); t1 = t0 + int(4 * beat_s * sr)
    return float(db(low[t0:t1]) - max(db(low[:t0]), db(low[t1:t1 + 8 * sr])))


def own_dip_db(before, sr, span_s=20):
    """The outgoing song's own deepest 0.5 s drop over its last span_s, measured like dip_db: soft songs breathe, and a
    blend is only to blame for a hole deeper than that (the fixed limit turned 4 of 12 Chill blends into echo-outs)."""
    x = before[-span_s * sr:].mean(axis=1)
    w = sr // 2
    if len(x) < 4 * w:
        return 0.0
    lv = np.array([20 * np.log10(np.sqrt(np.mean(x[i:i + w] ** 2)) + 1e-9) for i in range(0, len(x) - w, w)])
    return float(np.median(lv) - lv.min())


def before_tail(seg, skip):
    return seg[skip:]


def jump_db(before, overlap, nxt, nxt_skip, sr):
    """How much louder (+) or quieter (-) the join is than the audio either side of it."""
    db = lambda x: 20 * np.log10(np.sqrt(np.mean(x.mean(axis=1) ** 2)) + 1e-9)
    ov = db(overlap)  # distance to the nearer neighbour: same definition as the final check
    return min(ov - db(before[-8 * sr:]), ov - db(nxt[nxt_skip:nxt_skip + 8 * sr]), key=abs)


def echo_exit(seg, seg_voc, beats, sr, look_beats=ECHO_LOOK_BEATS):
    """Where the outgoing song stops for an echo-out: the latest beat in its last look_beats where the singer is not
    mid-word and no line starts within transition.CUT_GUARD_S, a bar line first; else the latest clean line end there;
    else the same over twice as many beats; else the softest beat in the last look_beats. Returns (cut sample, info)."""
    end = len(seg) / sr
    if seg_voc is None:
        return len(seg), {"vocal_out_at_s": None, "out_midline": False}
    # a segment still open at the excerpt's last sample goes on singing past it: its "end" is not a gap
    segs = [(a, b if b < end - 2 * transition.VOX_HOP else end + 60) for a, b in transition.sung_segments(seg_voc, sr)]
    period = float(np.median(np.diff(beats)))
    last = len(beats) - 1  # the excerpt is whole 32-beat blocks: its end is a phrase end, every 4th beat back a bar line
    for look in (look_beats, 2 * look_beats):  # a cut 32 beats early is heard as "too early": only if nothing is clean
        lo = end - look * period
        on_beat = [(b, (last - k) % 4 == 0) for k, b in enumerate(beats) if lo <= b <= end and transition.clean_cut(segs, b)]
        on_bar = [b for b, bar in on_beat if bar]
        line_ends = [b + 0.05 for a, b in segs if lo <= b + 0.05 <= end and transition.clean_cut(segs, b + 0.05)]
        if on_beat or line_ends:
            t = max(on_bar) if on_bar else max(b for b, _ in on_beat) if on_beat else max(line_ends)
            return int(round(t * sr)), {"vocal_out_at_s": round(t - end, 2), "out_midline": False}
    # sung straight through: stop on the beat where the voice is softest (between words), not mid-syllable
    db, h = transition._levels(seg_voc, sr)
    cand = [b for b in beats if end - look_beats * period <= b <= end] or [end]
    t = min(cand, key=lambda b: db[min(len(db) - 1, int(b * sr / h))])
    return int(round(t * sr)), {"vocal_out_at_s": round(t - end, 2), "out_midline": True}


ENTRY_FADE_S = 0.04  # the held-back incoming singer comes in over this long


def echo_entry(nxt, nxt_voc, nxt_beats, sr, fade_s=ENTRY_FADE_S):
    """The incoming song starts on its first beat (that beat lands on the cut). If it would start mid-line, hold its
    voice back until its next line starts (the music plays, the singer comes in on a line). A hold over
    ECHO_HOLD_MAX_BEATS sounds stretched: start the song later instead, on a bar line about ECHO_LEAD_BEATS before
    that line. Returns (nxt, nxt_voc, nxt_beats, info), all three starting at the chosen first beat."""
    k, t_in, midline = 0, nxt_beats[0], False
    if nxt_voc is not None:
        # a segment open at the first sample started before it: the song is mid-line there
        segs = [(a if a > 2 * transition.VOX_HOP else -60, b) for a, b in transition.sung_segments(nxt_voc, sr)]
        if not transition.clean_cut(segs, nxt_beats[0], guard_s=0.0):  # singing at the first beat
            starts = [a for a in transition.line_starts(segs) if a > nxt_beats[0]]
            midline = not starts
            if starts:
                t_in = max(nxt_beats[0], starts[0] - transition.LEAD_S)
                p_in = float(np.median(np.diff(nxt_beats)))
                if t_in - nxt_beats[0] > ECHO_HOLD_MAX_BEATS * p_in:
                    # beats 0.5-2 bars before the line that leave a full overlap of song after them; bar lines first
                    # ponytail: bars counted from the window start, off if trim_quiet_head trimmed a non-4 number of beats
                    cand = [j for j, b in enumerate(nxt_beats[:-1]) if t_in - 2 * ECHO_LEAD_BEATS * p_in <= b
                            <= t_in - 0.5 * p_in and len(nxt_beats) - 1 - j >= transition.OVERLAP_BEATS]
                    target = t_in - ECHO_LEAD_BEATS * p_in
                    if cand:
                        k = min(cand, key=lambda j: (j % 4 != 0, abs(nxt_beats[j] - target)))
    off = int(round(nxt_beats[k] * sr))
    nxt, nxt_beats = nxt[off:], nxt_beats[k:] - off / sr
    nxt_beats[-1] = len(nxt) / sr  # the stretcher needs the last beat to sit exactly on the last sample
    hold = t_in - off / sr
    info = {"vocal_in_at_s": None if nxt_voc is None else round(max(0.0, hold), 2), "in_midline": midline,
            "in_skip_s": round(off / sr, 2), "_entry": [int(k), off, None if nxt_voc is None or hold <= 0 else float(hold)]}
    if nxt_voc is None:
        return nxt, nxt_voc, nxt_beats, info
    nxt_voc = nxt_voc[off:]
    if hold <= 0:
        return nxt, nxt_voc, nxt_beats, info
    g = np.clip((np.arange(len(nxt)) / sr - hold) / fade_s, 0, 1)[:, None]
    return nxt - nxt_voc * (1 - g), nxt_voc * g, nxt_beats, info


def echo_out(seg, beats, nxt, nxt_beats, before, sr, seg_voc=None, nxt_voc=None, plan=None):
    """Echo-out that checks itself and keeps the lyrics whole: the outgoing song stops on a beat where its singer is
    between lines, the echo repeats only its music (no word is repeated) on the incoming song's beat grid, the
    incoming singer comes in on a line start; then try three echo strengths and keep the first with neither a hole
    nor a bump. Returns (before, overlap, nxt_skip, info, cost, nxt, nxt_voc, nxt_beats): the incoming song may
    start later than its window, so play the returned nxt (and keep its beats and vocals) from nxt_skip."""
    period = float(np.median(np.diff(beats)))
    cut, out_info = echo_exit(seg, seg_voc, beats, sr)
    nxt, nxt_voc, nxt_beats, in_info = echo_entry(nxt, nxt_voc, nxt_beats, sr)
    p_in = float(np.median(np.diff(nxt_beats)))
    step_a, step_b = transition.beat_steps(beats, nxt_beats)
    rep = p_in * step_b / step_a  # one outgoing beat on the incoming grid (2:1 for double/half tempo)
    a = seg[:cut]
    music = a - seg_voc[:cut] if seg_voc is not None else a
    before = before[:max(0, len(before) - (len(seg) - cut))]
    best = None
    for feedback, repeats in ((0.5, 4), (0.65, 6), (0.8, 8)):
        tail = transition.echo_tail(music, rep, sr, repeats=repeats, feedback=feedback, src_period=period)
        n = min(len(tail), len(nxt))
        overlap = nxt[:n] + tail[:n]
        dip = dip_db(before, overlap, nxt, n, sr)
        jump = abs(jump_db(before, overlap[:int(4 * p_in * sr)], nxt, n, sr))  # QA looks at the first 4 beats
        pile = echo_bass_pile_db(before, overlap, nxt, n, sr, p_in)
        cost = max(dip / (0.75 * dj.QA_MAX_DIP_DB), jump / (0.85 * dj.QA_LOUDNESS_JUMP_DB),  # margin: the final check
                   pile / (0.85 * dj.QA_BASS_PILEUP_DB))                                 # measures slightly differently
        if best is None or cost < best[0]:
            best = (cost, overlap, n, feedback, repeats)
        if cost <= 1:
            break
    cost = best[0] + (1.0 if out_info["out_midline"] or in_info["in_midline"] else 0.0)  # a mid-line cut is worse than a small bump
    wait = (in_info["vocal_in_at_s"] or 0) + in_info["in_skip_s"]  # singer held back, or that much of the part skipped
    cost += 0.5 if wait > ECHO_MAX_HOLD_S else 0.0  # a long wait for the singer: try another part
    entry = in_info.pop("_entry")
    if plan is not None:
        plan.update(kind="echo", cut=int(cut), entry=entry, rep_s=float(rep), src_period_s=period, feedback=best[3],
                    repeats=best[4], n=int(best[2]), music_only=seg_voc is not None)
    return a, best[1], best[2], {"type": "echo-out", **out_info, **in_info}, cost, nxt, nxt_voc, nxt_beats


def blend_is_clean(before, overlap, nxt, nxt_skip, info, sr):
    """The offline QA gate's checks, run on one freshly rendered blend. False -> use an echo-out instead."""
    if len(before) < 8 * sr or len(nxt) - nxt_skip < 8 * sr:  # too little audio either side to verify it: do not trust it
        return False
    sa, sb = (int(x) for x in info["beat_pairing"].split(":"))
    out_bpm, in_bpm = info["bpm_out"] / sa, info["bpm_in"] / sb
    if abs(out_bpm - in_bpm) / out_bpm > dj.QA_STRETCH or abs(info["phase_nudge_ms"]) > dj.QA_NUDGE_MS:
        return False
    mono = lambda x: x.mean(axis=1)
    low = lambda x: sosfilt(butter(4, 150, "lowpass", fs=sr, output="sos"), x)
    db = lambda x: 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)
    a, o, b = mono(before[-8 * sr:]), mono(overlap), mono(nxt[nxt_skip:nxt_skip + 8 * sr])
    if abs(jump_db(before, overlap, nxt, nxt_skip, sr)) > dj.QA_LOUDNESS_JUMP_DB:
        return False
    return db(low(o)) - max(db(low(a)), db(low(b))) <= dj.QA_BASS_PILEUP_DB


def alternatives(planner, songs, entry, nxt_entry, limit=2):
    """Other parts of the incoming song, best first: the hook is not the only place a mix can go."""
    song, cur = songs[nxt_entry["song"]], nxt_entry["window"]
    seen, scored = {(cur["start_beat"], cur["end_beat"])}, []
    for w in planner.win[nxt_entry["song"]] + planner.all_windows(nxt_entry["song"]):
        key = (w["start_beat"], w["end_beat"])
        if key in seen:
            continue
        seen.add(key)
        if w["last_rel"] < -live_plan.QUIET_EDGE_DB or w["out_low_rel"] < -live_plan.DIP_EDGE_DB \
                or w["in_low_rel"] < -live_plan.DIP_EDGE_DB or w["first_rel"] < -live_plan.QUIET_EDGE_DB:
            continue
        t = dj.window_terms(song, song["energy_pct"], w, entry["window"])
        scored.append((t["join"] + t["clean"] + 0.5 * t["hook"] + t["exit"] + t["entry"] + t["best"], -w["start_beat"], w))
    return [w for _, _, w in sorted(scored, key=lambda x: (x[0], x[1]), reverse=True)[:limit]]


def mc_room(overlap, nxt, nxt_skip, nxt_voc, info, sr=dj.OUT_SR, search_s=None):
    """Where an MC line may go near this join: (music, sung segments in it, seconds to search) with music = the overlap
    and the incoming song after it; None when there is no place (no stem, a fill, a blend without the vocal swap, or
    too little song before the next join). The line goes over an instrumental stretch (the echo tail, the incoming
    lead-in, the gap in a vocal swap), never over a sung line."""
    # with a fill the incoming song starts late, so its sung lines are not where the MC would look
    if nxt_voc is None or info.get("filler") or (info.get("type") == "blend" and not info.get("vocal_swap")):
        return None
    n_ov = len(overlap)
    room = min(int((search_s or mc.SEARCH_S) * sr), n_ov + len(nxt) - nxt_skip - 20 * sr)  # stay clear of the next join
    if room <= 0:
        return None
    ref = transition.vocal_ref_db(nxt_voc, sr)
    if info.get("type") == "blend":  # sung: the outgoing voice until its cut, the incoming one's lines once it is in
        out_at, in_at = info["vocal_out_at_s"], info["vocal_in_at_s"]
        gate = out_at if in_at is None else in_at
        # ponytail: linear map of nxt[:nxt_skip] onto the stretched overlap, +-0.3 s for the tempo ramp; exact = warped stems
        scale = n_ov / max(1, nxt_skip)
        segs = [(0.0, out_at)] + [(max(gate, a * scale - 0.3), b * scale + 0.3)
                                  for a, b in transition.sung_segments(nxt_voc[:nxt_skip], sr, ref) if b * scale + 0.3 > gate]
        segs += [(a + n_ov / sr, b + n_ov / sr) for a, b in
                 transition.sung_segments(nxt_voc[nxt_skip:nxt_skip + max(0, room - n_ov)], sr, ref)]
    else:  # echo-out: the echo is music only, and the overlap runs on the incoming song's own clock
        segs = transition.sung_segments(nxt_voc[:room], sr, ref)
    k = max(0, room - n_ov)
    return np.concatenate([overlap, nxt[nxt_skip:nxt_skip + k]]), segs, room / sr


class Engine(threading.Thread):
    """Renders the audio one transition ahead and feeds the sink."""

    def __init__(self, sess, library, sink, start=None, script=None, max_s=None):
        super().__init__(daemon=True)
        self.sess, self.library, self.sink, self.start_name = sess, library, sink, start
        self.script = sorted(script or [], key=lambda e: e["t"])
        self.max_s = max_s
        self.stop_flag = threading.Event()
        self.skip = threading.Event()
        self.error = None
        self.clip_threads = []

    def _clip(self, position, before, skip, overlap, nxt, nxt_skip):
        """Write the join's clip (CLIP_EDGE_S either side of the overlap) in a background thread; never stops the music."""
        sess, sr = self.sess, dj.OUT_SR
        if sess.clip_dir is None:
            return

        def work():
            try:
                x = np.concatenate([before[max(skip, len(before) - CLIP_EDGE_S * sr):], overlap,
                                    nxt[nxt_skip:nxt_skip + CLIP_EDGE_S * sr]])
                path = Path(sess.clip_dir) / f"{position}.flac"
                path.parent.mkdir(parents=True, exist_ok=True)
                sf.write(path, 0.97 * dj.soft_limit(x.copy()), sr, format="FLAC")  # as heard (see _write)
                rel = path.resolve().relative_to(Path(self.library).resolve()).as_posix()
                from algo_rhythm import config, storage
                if config.settings().storage_url:
                    storage.upload(path, rel)
                if sess.on_clip:
                    sess.on_clip(position, rel)
            except Exception:
                logging.getLogger("algo_rhythm.engine").exception("could not write the clip of join %s", position)
        t = threading.Thread(target=work, daemon=True)
        self.clip_threads = [c for c in self.clip_threads if c.is_alive()] + [t]
        t.start()

    def _write(self, x):
        step = dj.OUT_SR // 2
        for i in range(0, len(x), step):
            if self.stop_flag.is_set():
                raise StopIteration
            self.sink.push(0.97 * dj.soft_limit(x[i:i + step].copy()), self.stop_flag)  # 0.97: peak stays under 0.98
            self._run_script()
            if self.max_s and self.sink.written / dj.OUT_SR >= self.max_s:
                raise StopIteration

    def _run_script(self):
        while self.script and self.script[0]["t"] <= self.sink.playhead():
            ev = self.script.pop(0)
            if "vibe" in ev:
                self.sess.set_level(ev["vibe"])
            if "request" in ev:
                match = [s["file"] for s in self.sess.songs if ev["request"].lower() in s["name"].lower()]
                if not match:
                    raise ValueError(f"script: no song matches {ev['request']!r}")
                self.sess.request(match[0])
            if "mc" in ev:  # {"t": 0, "mc": "on"}: the AI MC for this test session (off unless a script asks)
                self.sess.mc = self.sess.mc or mc.MC(enabled=False)
                self.sess.mc.enabled = ev["mc"] == "on"

    def run(self):
        try:
            self._loop()
        except StopIteration:
            pass
        except Exception as e:  # surface it to the page and the console instead of dying silently
            self.error = e
            self.sess._say(f"ENGINE ERROR: {e}")
            raise

    def _alternatives(self, entry, nxt_entry, limit=2):
        return alternatives(self.sess.planner, self.sess.songs, entry, nxt_entry, limit)

    def _render_join(self, entry, a, skip, nxt_entry):
        """Render the transition into nxt_entry (joins.render_join, the same code the web server runs).
        Returns (nxt, nxt_beats, nxt_voc, before, overlap, nxt_skip, info, nxt_excerpt)."""
        from algo_rhythm import joins
        j = joins.render_join(self.library, self.sess.songs, entry, a, skip, nxt_entry,
                              alternatives=lambda limit: self._alternatives(entry, nxt_entry, limit), say=self.sess._say)
        return j.nxt.audio, j.nxt.beats, j.nxt.voc, j.before, j.overlap, j.nxt_skip, j.info, j.nxt

    def _loop(self):
        from algo_rhythm import joins
        sess, lib, sr = self.sess, self.library, dj.OUT_SR
        songs = sess.songs
        entry = sess.start(self.start_name)
        sess.played_hook(entry, None)
        a = joins.load(lib, songs[entry["song"]], entry["window"])
        skip = 0
        while True:
            nxt_entry = sess.commit_next(self.sink.written / sr)   # locks the next song, then renders into it
            r = self._render_join(entry, a, skip, nxt_entry)
            pos, job = skip, None
            while pos < len(r[3]):  # play the song on air; a request may still take the next slot meanwhile
                if self.skip.is_set():  # jump to just before the transition, to hear it without the whole song
                    self.skip.clear()
                    getattr(self.sink, "drop_buffered", lambda: None)()
                    pos = max(pos, len(r[3]) - SKIP_LEAD_S * sr)
                self._write(r[3][pos:pos + sr // 2])
                pos += sr // 2
                if sess.mc:
                    sess.mc.warm(sess.level)  # background: the voice is ready before the join needs it
                job = self._early(job, entry, a, skip, (len(r[3]) - pos) / sr)
                if job and "result" in job:
                    new = job["result"]
                    if isinstance(new, tuple) and len(new[3]) - pos >= sr and pos <= len(new[3]) \
                            and np.allclose(new[3][max(skip, pos - sr // 10):pos], r[3][max(skip, pos - sr // 10):pos]) \
                            and sess.swap_next(job["entry"], nxt_entry):
                        nxt_entry, r = job["entry"], new
                    job = None
            nxt, nxt_beats, nxt_voc, before, overlap, nxt_skip, info, nxt_ex = r
            overlap, nxt = self._mc(overlap, nxt, nxt_skip, nxt_voc, info)
            nxt_ex.audio = nxt  # with the MC's line in it, as heard
            with sess.lock:  # the next song becomes audible where its overlap starts
                sess.timeline[-1]["start_s"] = self.sink.written / sr
                sess.timeline[-1]["overlap_s"] = len(overlap) / sr
                nxt_entry["transition_info"] = info
            sess.played_hook(nxt_entry, info)
            self._clip(nxt_entry["position"], before, skip, overlap, nxt, nxt_skip)
            self._write(overlap)
            entry, a, skip = nxt_entry, nxt_ex, nxt_skip

    def _mc(self, overlap, nxt, nxt_skip, nxt_voc, info):
        """AI MC: when a line is due and its voice is ready, put it over an instrumental stretch near this join (the
        echo tail, the incoming lead-in, the gap in a vocal swap), never over a sung line. No such stretch: next join."""
        sess, sr = self.sess, dj.OUT_SR
        now = self.sink.written / sr
        got = sess.mc and nxt_voc is not None and not info.get("filler") and sess.mc.take(now, sess.level, wait=not getattr(self.sink, "realtime", True))
        room = got and mc_room(overlap, nxt, nxt_skip, nxt_voc, info, sr)
        if not room:
            return overlap, nxt
        music, segs, end_s = room
        n_ov = len(overlap)
        k = len(music) - n_ov
        mixed, at = mc.mix(music, segs, got[1], sr, end_s=end_s)
        if at is None:
            return overlap, nxt
        nxt = nxt.copy()
        nxt[nxt_skip:nxt_skip + k] = mixed[n_ov:]
        sess.mc.spoke(got[0], now + at)
        sess._say(f"MC: {got[0]}")
        return mixed[:n_ov], nxt

    def _early(self, job, entry, a, skip, left_s):
        """Render a moved-up request in the background while the song on air keeps playing."""
        sess = self.sess
        if job is None:
            with sess.lock:
                new, sess.early = sess.early, None
            if new is None or left_s < EARLY_MIN_LEFT_S:
                return None
            job = {"entry": new}

            def work():
                try:
                    job["result"] = self._render_join(entry, a, skip, new)
                except Exception as e:  # keep the planned song
                    job["result"] = e
            job["thread"] = threading.Thread(target=work, daemon=True)
            job["thread"].start()
            if not getattr(self.sink, "realtime", True):  # test sink writes faster than real time: wait for the render
                job["thread"].join()
        return job


# ---------------------------------------------------------------- web

# ---------------------------------------------------------------- run

def build(library, level, excerpt_s=EXCERPT_S, history=None):
    from algo_rhythm import repository  # database when DATABASE_URL is set, else the library's files
    songs, skipped = repository.load_songs(library)
    if len(songs) < 8:
        raise SystemExit(f"need at least 8 usable tagged songs in {library}, found {len(songs)}")
    from algo_rhythm import search
    search.attach(library, songs)  # vectors for search by meaning (none yet = name search only)
    sess = Session(songs, live_plan.Planner(songs, excerpt_s), level, library,
                   history or library / "_mixes" / "live_history.json")
    from algo_rhythm import art
    sess.art = art.load_index(library)
    return sess


def simulate(library, script, seconds, out, level="medium", start=None):
    """Run a scripted session without speakers. Returns the session (timeline, log) for checking."""
    history = Path(out).with_suffix(".history.json")  # tests keep their own history: your real one stays untouched
    history.unlink(missing_ok=True)
    sess = build(library, level, history=history)
    sink = FileSink(dj.OUT_SR, out)
    sess.playhead = sink.playhead
    eng = Engine(sess, library, sink, start, script, seconds)
    eng.start()
    eng.join()
    sink.close()
    if eng.error:
        raise eng.error
    return sess


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("library", type=Path)
    ap.add_argument("--level", choices=list(live_plan.LEVELS), default="medium")
    ap.add_argument("--start", help="open with the first song whose file name contains this text")
    ap.add_argument("--script", type=Path)
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--out", type=Path, default=Path("session.wav"))
    args = ap.parse_args(argv)
    library = args.library.resolve()

    script = json.loads(args.script.read_text()) if args.script else []
    sess = simulate(library, script, args.seconds, args.out, args.level, args.start)
    for t in sess.timeline:
        e = t["entry"]
        print(f"{t['start_s']:7.1f}s  {sess.songs[e['song']]['name'][:34]:34s} {e['transition_in']:6s} "
              f"match {e['match']:.2f}{'  REQUEST' if e['request'] else ''}")
    print("\n".join(reversed(sess.log)))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
