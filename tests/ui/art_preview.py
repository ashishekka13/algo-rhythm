"""Look at the visuals without the backend: serves live.html with a fake /state, sample art and a fake beat "pulse".

Run:  python3 tests/ui/art_preview.py            then open http://127.0.0.1:8791/ , /guest or /presenter
      python3 tests/ui/art_preview.py --at 25    freezes the loop at 25 s (mid-mix), for screenshots
      or add ?freeze=25 to the page address (it reaches /state through the Referer), e.g. /presenter?freeze=25&kaleido=1
      add ?vibe=sad (chill, sad, romantic, groove, party, peak) to see that vibe's mood theme and weather

The loop (28 s per song): a song plays for 20 s, then an 8 s mix into the next; four songs, one per energy level
(low -> medium -> high -> max, so each world and each transition shows), then repeat; max -> low is an echo-out.
Each song has a beat grid at its BPM, a downbeat every 4 beats, a phrase every 16, a hook at 5 s and a drop at 12 s,
and a synthetic loudness envelope that builds up to the drop. Nothing is played; POSTs are accepted and ignored.
"""
import argparse
import json
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATIC = Path(__file__).resolve().parents[2] / "algo_rhythm" / "static"
SAMPLES = Path("/Users/sambhav.jain/Downloads/song-matcher/library_100/_mixes/listen/art_samples")
SONGS = [  # (name, art file served as /art/<file>, bpm, energy)
    ("Baarish Ban Jaana - Payal Dev", "Baarish_Ban_Jaana.jpg", 82, "low"),
    ("Kesariya - Brahmastra", "Kesariya.jpg", 94, "medium"),
    ("Kala Chashma - Baar Baar Dekho", "Kala_Chashma.jpg", 100, "high"),
    ("Malhari - Bajirao Mastani", "gemini-2.5-flash-image__Kala_Chashma.png", 128, "max"),
]
PLAY, MIX = 20.0, 8.0
CYCLE = PLAY + MIX
HOOK_S, DROP_S = 5.0, 12.0  # seconds into the song's own part
START = time.time()
AT = None


def entry(k):
    name, art, bpm, energy = SONGS[k % len(SONGS)]
    return {"name": name, "art": f"/art/{art}", "bpm": bpm, "key": "8A", "energy": energy,
            "mood": "bright/laid-back", "kind": "echo" if k % len(SONGS) == 0 else "blend",  # how it comes in
            "match": 0.7, "request": False, "status": "planned"}


def grid(k):
    """Song k's beats: it rises in MIX seconds before its own part starts at k * CYCLE."""
    bpm = SONGS[k % len(SONGS)][2]
    return k * CYCLE - MIX, 60.0 / bpm


def snap(k, s):
    """The downbeat nearest to s seconds into song k's part."""
    o, b = grid(k)
    return o + round((k * CYCLE + s - o) / (4 * b)) * 4 * b


def pulse(t):
    beats, downs, drops = [], [], []
    for k in range(max(0, int((t - 2) // CYCLE)), int((t + 10) // CYCLE) + 1):
        o, b = grid(k)
        lo, hi = max(t - 2, k * CYCLE), min(t + 10, (k + 1) * CYCLE)  # song k leads during its own 28 s
        for j in range(max(0, math.ceil((lo - o) / b)), int((hi - o) / b) + 1):
            x = round(o + j * b, 3)
            if not lo <= x < hi:
                continue
            beats.append(x)
            if j % 4 == 0:
                downs.append(x)
            if j % 16 == 0 and j:
                drops.append({"t": x, "kind": "phrase"})
        for s, kind in ((HOOK_S, "hook"), (DROP_S, "drop")):
            x = round(snap(k, s), 3)
            if lo <= x < hi:
                drops = [d for d in drops if abs(d["t"] - x) > 0.01] + [{"t": x, "kind": kind}]
    t0 = round(t - 1, 1)
    rms, bass, treble = [], [], []
    for i in range(120):
        x = t0 + i * 0.1
        k = int(x // CYCLE)
        o, b = grid(k)
        drop = snap(k, DROP_S)
        since = (x - o) % b  # seconds since the last beat
        build = min(1.0, max(0.0, (x - (drop - 6)) / 6)) if x < drop else 0.0
        after = x >= drop
        rms.append(round(0.35 + 0.45 * build + (0.5 if after else 0.0) * math.exp(-(x - drop) / 12) + (0.2 if after else 0), 3))
        bass.append(round((1.0 if after else 0.55) * math.exp(-since / 0.18), 3))
        treble.append(round(0.2 + 0.7 * build + (0.35 if after else 0), 3))
    return {"server_s": round(t, 3), "bpm": SONGS[int(t // CYCLE) % len(SONGS)][2], "beats": beats, "downbeats": downs,
            "drops": sorted(drops, key=lambda d: d["t"]), "env": {"t0": t0, "dt": 0.1, "rms": rms, "bass": bass, "treble": treble}}


VIBES = {"chill": ("low", "chill"), "sad": ("low", "sad"), "romantic": ("medium", "romantic"),
         "groove": ("medium", "party"), "party": ("high", "party"), "peak": ("max", "party")}  # as in live_plan.VIBES


def state(freeze=None, vibe=None):
    t = freeze if freeze is not None else AT if AT is not None else time.time() - START
    k, into = int(t // CYCLE), t % CYCLE
    now, nxt = entry(k), entry(k + 1)
    mixing = None
    if into >= PLAY:  # the mix: progress 0 -> 1 over MIX seconds
        mixing = {"from": now["name"], "to": nxt["name"], "kind": nxt["kind"], "progress": round((into - PLAY) / MIX, 3),
                  "from_art": now["art"], "to_art": nxt["art"]}
    level, mood = VIBES.get(vibe, (now["energy"], "auto"))
    return {"api": 2, "status": "playing", "level": level, "vibe": vibe if vibe in VIBES else None, "mood": mood, "vibes": {k: v[0] for k, v in VIBES.items()}, "levels": ["low", "medium", "high", "max"],
            "now": now, "position_s": round(into, 1), "length_s": CYCLE, "mixing": mixing,
            "next": [nxt, entry(k + 2)], "requests": [], "history": [], "log": ["00:00:00  art preview (fake state)"],
            "pulse": pulse(t)}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if ctype.startswith("image/") else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/guest", "/presenter"):
            return self.send(200, (STATIC / "live.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/static/visuals.js":
            return self.send(200, (STATIC / "visuals.js").read_bytes(), "text/javascript")
        if path == "/state":
            q = parse_qs(urlparse(self.headers.get("Referer", "")).query)
            freeze = float(q["freeze"][0]) if "freeze" in q else None
            vibe = q["vibe"][0] if "vibe" in q else None
            return self.send(200, json.dumps(state(freeze, vibe)).encode(), "application/json")
        if path == "/songs":
            return self.send(200, b"[]", "application/json")
        files = {f"/art/{art}": art for _, art, _, _ in SONGS}
        if path in files:
            return self.send(200, (SAMPLES / files[path]).read_bytes(), "image/png" if path.endswith(".png") else "image/jpeg")
        self.send(404, b'{"error": "not found"}', "application/json")

    def do_POST(self):
        self.send(200, json.dumps(state()).encode(), "application/json")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", type=float, help="freeze the loop at this many seconds (20 to 28 is the first mix)")
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()
    if args.port == 8765:
        raise SystemExit("8765 is the real player; use another port")
    AT, port = args.at, args.port
    print(f"art preview on http://127.0.0.1:{port}/  (also /guest, /presenter); Ctrl+C to stop")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
