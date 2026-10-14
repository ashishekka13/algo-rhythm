"""Check a tagged library against the contract (TAGGING_CONTRACT.md).

Usage: python validate_tags.py LIBRARY_DIR [--no-hash]

Checks every audio file has exactly one outcome (a valid tag file or an error
file), that each tag file matches tag_schema.json, and that the values are
consistent with each other and with the audio file. Writes
LIBRARY_DIR/tags/_report.json and exits non-zero if anything is invalid.
"""
import argparse
import json
import sys
from pathlib import Path

import jsonschema
import numpy as np

from algo_rhythm import tagger

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "algo_rhythm" / "tag_schema.json").read_text())
CAMELOT = {f"{tonic} {mode}": code for mode, m in tagger.CAMELOT.items() for tonic, code in m.items()}


def semantic_errors(t, library, check_hash):
    errs = []
    audio = library / t["file"]
    if not audio.exists():
        return [f"audio file missing: {t['file']}"]
    if check_hash and tagger.sha256(audio) != t["sha256"]:
        errs.append("sha256 does not match the audio file (re-tag it)")

    beats, dur = np.array(t["beats"]), t["duration_s"]
    if len(beats) != t["tempo"]["beat_count"]:
        errs.append(f"beat_count {t['tempo']['beat_count']} != len(beats) {len(beats)}")
    if np.any(np.diff(beats) <= 0):
        errs.append("beats are not strictly increasing")
    if len(beats) and beats[-1] > dur + 0.5:
        errs.append("beats run past the end of the song")
    if len(beats) > 1:
        grid_bpm = 60 / np.median(np.diff(beats))
        if abs(grid_bpm - t["tempo"]["bpm"]) / t["tempo"]["bpm"] > 0.03:
            errs.append(f"bpm {t['tempo']['bpm']} disagrees with beat grid ({grid_bpm:.1f})")

    if CAMELOT.get(t["key"]["name"]) != t["key"]["camelot"]:
        errs.append(f"key {t['key']['name']} is not Camelot {t['key']['camelot']}")

    s = t["structure"]
    if not 0 <= s["intro_end_s"] <= s["outro_start_s"] <= dur + 0.5:
        errs.append("structure times out of order or out of range")

    p = t["phrases"]
    n = len(p["start_beats"])
    if not len(p["energy"]) == len(p["repetition"]) == len(p["score"]) == n:
        errs.append("phrase arrays have different lengths")
    if n and (np.any(np.diff(p["start_beats"]) != tagger.PHRASE_BEATS)
              or p["start_beats"][-1] + tagger.PHRASE_BEATS > len(beats)):
        errs.append("phrase starts are not consecutive 16-beat blocks inside the beat grid")
    pm = t.get("vibe", {}).get("phrase_mix")
    if pm and not all(len(pm[k]) == n for k in ("rms_db", "perc", "steady", "vocal", "low_db") if k in pm):
        errs.append("vibe.phrase_mix arrays do not match the phrase count")
    if (n < 4) != ("too_few_phrases" in t["quality"]["flags"]):
        errs.append("too_few_phrases flag does not match phrase count")

    for h in t["hooks"]:
        if not (0 <= h["start_beat"] < h["end_beat"] <= len(beats)) or h["start_s"] >= h["end_s"] > dur + 0.5:
            errs.append(f"hook {h} out of range")
        elif (h["end_beat"] - h["start_beat"]) % (2 * tagger.PHRASE_BEATS):
            errs.append(f"hook {h['start_s']}-{h['end_s']}s is not whole 32-beat blocks")
    if n >= 4 and not t["hooks"]:
        errs.append("no hooks despite enough phrases")
    return errs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("library", type=Path)
    ap.add_argument("--no-hash", action="store_true", help="skip the sha256 check (faster)")
    args = ap.parse_args(argv)
    library = args.library.resolve()
    tags_dir = library / "tags"
    if not tags_dir.is_dir():
        print(f"FAIL: {tags_dir} does not exist (run tagger.py first)")
        return 1

    audio = set(tagger.audio_files(library))
    tagged, invalid, flagged = {}, {}, {}
    for f in sorted(tags_dir.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            t = json.loads(f.read_text())
        except ValueError as e:
            invalid[f.name] = [f"not valid JSON: {e}"]
            continue
        errs = [f"schema: {e.message} at /{'/'.join(map(str, e.absolute_path))}"
                for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(t)]
        if not errs:
            errs = semantic_errors(t, library, not args.no_hash)
        if f.stem != tagger.tag_key(t.get("file", "")):
            errs.append(f"file name should be {tagger.tag_key(t.get('file', ''))}.json")
        tagged[t.get("file", f.name)] = f.name
        if errs:
            invalid[f.name] = errs
        elif t["quality"]["flags"]:
            flagged[t["file"]] = t["quality"]["flags"]

    failed = {json.loads(f.read_text())["file"] for f in (tags_dir / "_errors").glob("*.json")} \
        if (tags_dir / "_errors").exists() else set()
    missing = sorted(audio - set(tagged) - failed)
    orphans = sorted(set(tagged) - audio)

    report = {"audio_files": len(audio), "tagged": len(tagged), "valid": len(tagged) - len(invalid),
              "failed_to_tag": sorted(failed), "missing": missing, "orphan_tags": orphans,
              "invalid": invalid, "flagged_for_review": flagged}
    (tags_dir / "_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print(f"{len(audio)} audio files: {report['valid']} valid, {len(invalid)} invalid, "
          f"{len(failed)} failed, {len(missing)} missing, {len(orphans)} orphan tags, "
          f"{len(flagged)} flagged for review")
    for name, errs in invalid.items():
        print(f"  INVALID {name}: " + "; ".join(errs))
    for name in missing:
        print(f"  MISSING {name}")
    for name, flags in flagged.items():
        print(f"  review  {name}: {', '.join(flags)}")
    ok = not (invalid or missing or orphans)
    print("PASS" if ok else "FAIL", f"(report: {tags_dir / '_report.json'})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
