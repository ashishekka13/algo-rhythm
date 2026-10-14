"""Song art: one generated image per song, made once ahead of time and animated by the page (no AI while music plays).

    python -m algo_rhythm.art LIBRARY --dry-run        what it would make, the prompts and the cost (no calls)
    python -m algo_rhythm.art LIBRARY [--limit N] [--force] [--jobs 2]

Images come from Gemini's image model through the gateway (same Vertex AI key and daily cap), prompted from the
song's Gemini details (mood, style, occasion, era, energy) with no faces, text or posters. Each is stored as
LIBRARY/art/<id>.jpg (id = first 16 hex characters of the audio's sha256) and listed in LIBRARY/art/index.json,
which the player reads at start-up. With STORAGE_URL set, both are also uploaded to the bucket.
"""
import argparse
import base64
import io
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from algo_rhythm import config, dj, genai, storage

log = logging.getLogger("algo_rhythm.art")
MODEL = "gemini-2.5-flash-image"
PRICE_PER_IMAGE = 0.039  # USD, Gemini 2.5 Flash Image (Oct 2026); check the Vertex AI pricing page before a big run
MAX_SIDE, QUALITY = 1024, 82
INDEX = "art/index.json"
WAITS_S = (20, 40, 80, 160, 300)  # after a 429: Vertex's shared image quota refills within minutes
sleep = time.sleep  # replaceable in tests


def art_id(song):
    return song["sha256"][:16]


def prompt(song):
    """What to draw: the song's feel, never its stars, title or poster."""
    llm = song.get("llm") or {}
    occasion = ", ".join(llm.get("occasion") or [])
    return (f"Abstract cinematic album artwork for a Bollywood song. Mood: {llm.get('lyric_mood') or song.get('mood', '')}; "
            f"style: {llm.get('style', '')}; occasion: {occasion}; era: {llm.get('year', '')}; "
            f"energy {llm.get('energy', '')} of 4. Evocative scene, colour and light; dreamy, rich, high contrast. "
            "No people's faces, no text, no letters, no logos, no film poster. Square.")


def load_index(library):
    """{art id: file name} of the images that exist, or {} (no art yet: the page keeps its generated visual)."""
    p = storage.local_path(INDEX, library)
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return {}


def to_jpeg(data):
    """Any image bytes -> a JPEG at most MAX_SIDE pixels on a side (Gemini returns ~2 MB PNGs; ~300 KB after)."""
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=QUALITY, optimize=True)
    return out.getvalue()


def generate(song, send=None):
    """One image for `song` as JPEG bytes. `send` is the gateway's POST (replaceable in tests)."""
    send = send or genai._send
    body = {"contents": [{"role": "user", "parts": [{"text": prompt(song)}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]}}
    j = send(genai.model_url(MODEL, "generateContent"), body, 120)
    part = next((p for p in j["candidates"][0]["content"]["parts"] if "inlineData" in p), None)
    if part is None:
        raise genai.GenAIError("Gemini returned no image")
    return to_jpeg(base64.b64decode(part["inlineData"]["data"]))


def run(library, songs, *, jobs=2, send=None):
    """Make art for `songs`, saving each image and the index as it goes. Returns (made, failed)."""
    library = Path(library)
    (library / "art").mkdir(exist_ok=True)
    index, made, failed = load_index(library), 0, 0

    def one(s):
        for wait in WAITS_S + (None,):
            try:
                return s, generate(s, send)
            except genai.RateLimited:
                if wait is None:
                    raise
                log.info("rate-limited; trying %s again in %d s", s["name"][:40], wait)
                sleep(wait)

    with ThreadPoolExecutor(jobs) as pool:
        for fut in [pool.submit(one, s) for s in songs]:
            try:
                s, jpg = fut.result()
            except genai.QuotaError as e:
                log.warning("stopping: %s", e)
                failed += 1
                break
            except Exception as e:
                log.warning("no art: %s", e)
                failed += 1
                continue
            name = f"{art_id(s)}.jpg"
            (library / "art" / name).write_bytes(jpg)
            index[art_id(s)] = name
            (library / INDEX).write_text(json.dumps(index, indent=0))
            if config.settings().storage_url:
                storage.upload(library / "art" / name, f"art/{name}")
                storage.upload(library / INDEX, INDEX)
            made += 1
            print(f"art {made}: {s['name'][:50]}", flush=True)
    return made, failed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("library", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="show what would be made and the cost; no calls")
    ap.add_argument("--limit", type=int, help="make at most this many images")
    ap.add_argument("--force", action="store_true", help="remake images that already exist")
    ap.add_argument("--jobs", type=int, default=2)
    args = ap.parse_args(argv)
    library = args.library.resolve()
    songs, _ = dj.load_library(library)
    index = load_index(library)
    todo = [s for s in songs if args.force or art_id(s) not in index]
    todo = todo[:args.limit] if args.limit else todo
    print(f"{len(songs)} songs, {len(index)} with art; to make: {len(todo)} "
          f"(about ${len(todo) * PRICE_PER_IMAGE:.2f} at ${PRICE_PER_IMAGE}/image with {MODEL})")
    if args.dry_run:
        for s in todo[:3]:
            print(f"\n{s['name']}\n  {prompt(s)}")
        return 0
    made, failed = run(library, todo, jobs=args.jobs)
    print(f"made {made}, failed {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
