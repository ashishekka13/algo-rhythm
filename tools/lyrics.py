"""Transcribe the sung words of each song (from its Demucs vocal stem) with timestamps. Offline, resumable.
Run it with the stems environment (mlx-whisper is installed there, Apple Silicon only):

    .venv-stems/bin/python tools/lyrics.py LIBRARY                 every stem without a lyrics file yet
    .venv-stems/bin/python tools/lyrics.py LIBRARY --worker 0/2    half of them (run 0/2 and 1/2 side by side)
    .venv-stems/bin/python tools/lyrics.py LIBRARY --limit 20      stop after 20 songs

Writes LIBRARY/lyrics/<key>.json: {"model", "lang", "title", "words": [[start_s, end_s, word], ...],
"lines": [[start_s, end_s, text], ...]}. Hindi and Punjabi come out in Devanagari (romanising is done when the
file is read, algo_rhythm/lyrics.py). A song whose file exists is skipped, so stopping and re-running resumes.
"""
import argparse
import collections
import json
import sys
import time
from pathlib import Path

import mlx_whisper

from algo_rhythm import media

MODEL = "mlx-community/whisper-large-v3-turbo"
# Whisper's Punjabi output is Gurmukhi and worse; the Hindi hint gives readable Devanagari for both.
LANG = {"english": "en", "hindi": "hi", "punjabi": "hi", "mixed": "hi"}  # anything else: let Whisper detect
MIN_PROB = 0.3        # a word Whisper is less sure of than this is dropped
LOOP_REPEAT = 5       # a line where one word is said this often, and is half the words, is a decoding loop


def clean(segments):
    words, lines = [], []
    for s in segments:
        if s["avg_logprob"] < -1.0:  # gibberish over instrumental bleed
            continue
        ws = [w for w in s.get("words", []) if w["probability"] >= MIN_PROB and w["word"].strip()]
        if not ws:
            continue
        top = collections.Counter(w["word"].strip() for w in ws).most_common(1)[0][1]
        if top >= LOOP_REPEAT and top * 2 >= len(ws):
            continue
        words += [[round(w["start"], 2), round(w["end"], 2), w["word"].strip()] for w in ws]
        lines.append([round(ws[0]["start"], 2), round(ws[-1]["end"], 2), " ".join(w["word"].strip() for w in ws)])
    return words, lines


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("library", type=Path)
    ap.add_argument("--worker", default="0/1", help="k/n: take every n-th song starting at k")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    lib = args.library.resolve()
    out_dir = lib / "lyrics"
    out_dir.mkdir(exist_ok=True)
    k, n = map(int, args.worker.split("/"))
    keys = sorted({p.name.split(".vocals.")[0] for p in (lib / "stems").glob("*.vocals.*")})[k::n]
    todo = [key for key in keys if not (out_dir / f"{key}.json").exists()]
    print(f"{len(keys) - len(todo)} done, {len(todo)} to do", flush=True)
    for done, key in enumerate(todo[:args.limit or None], 1):
        meta = lib / "enrich" / f"{key}.json"
        info = json.loads(meta.read_text()) if meta.exists() else {}
        t = time.time()
        try:
            r = mlx_whisper.transcribe(str(media.stem_path(lib, key)), path_or_hf_repo=MODEL,
                                       language=LANG.get(info.get("language")), word_timestamps=True,
                                       condition_on_previous_text=False, temperature=0.0,  # no slow retries
                                       hallucination_silence_threshold=2.0, verbose=None)
        except Exception as e:  # one bad file must not stop the batch; it is retried on the next run
            print(f"FAIL {key}: {e}", file=sys.stderr, flush=True)
            continue
        words, lines = clean(r["segments"])
        out = {"model": MODEL, "lang": r.get("language"), "title": info.get("title") or key.split("__")[-1],
               "words": words, "lines": lines}
        tmp = out_dir / f"{key}.json.tmp"
        tmp.write_text(json.dumps(out, ensure_ascii=False))
        tmp.rename(out_dir / f"{key}.json")  # never leave a half-written file that would be skipped as done
        print(f"[{done}/{len(todo)}] {time.time() - t:.0f}s {len(words)} words  {key}", flush=True)


if __name__ == "__main__":
    main()
