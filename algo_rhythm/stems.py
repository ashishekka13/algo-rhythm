"""Separate the vocals of each song with Demucs (htdemucs). Run it with the stems environment, not the main one:

    .venv-stems/bin/python -m algo_rhythm.stems LIBRARY                    every song that has no vocals file yet
    .venv-stems/bin/python -m algo_rhythm.stems LIBRARY --files a/b.mp3    just these (paths relative to LIBRARY)
    .venv-stems/bin/python -m algo_rhythm.stems LIBRARY --force            redo everything

(.venv-stems needs this package too: .venv-stems/bin/pip install -e . --no-deps.) Runs on the Apple GPU when there is
one, else the CPU (the Cloud Run job, algo_rhythm.pipeline).

Writes LIBRARY/stems/<key>.vocals.mp3 (algo_rhythm.media: the same encoding as the songs, so the browser can subtract
it sample for sample) and <key>.json {sha256, model}. The instrumental is not stored: it is exactly (song - vocals),
so the mixer computes it when it needs it.
<key> is the tagger's key: the relative path without its extension, "/" replaced by "__".
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import truststore

truststore.inject_into_ssl()  # the model downloads once; use the macOS trust store so certificate checks stay on
import julius  # noqa: E402
import soundfile as sf  # noqa: E402

from algo_rhythm import media  # noqa: E402
import torch  # noqa: E402
from demucs.apply import apply_model  # noqa: E402
from demucs.pretrained import get_model  # noqa: E402

MODEL = "htdemucs"
EXTS = {".mp3", ".wav", ".flac", ".ogg"}


def tag_key(rel):
    return str(Path(rel).with_suffix("")).replace("/", "__")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_audio(lib):
    out = []
    for p in sorted(lib.rglob("*")):
        rel = p.relative_to(lib)
        if p.suffix.lower() in EXTS and p.is_file() and not any(part[0] in "._" for part in rel.parts) \
                and rel.parts[0] not in ("tags", "stems", "requested_tmp"):
            out.append(rel.as_posix())
    return out


def separate(model, path, device):
    wav, sr = sf.read(path, always_2d=True, dtype="float32")
    x = torch.from_numpy(wav.T.copy())
    if x.shape[0] == 1:
        x = x.repeat(2, 1)
    if sr != model.samplerate:
        x = julius.resample_frac(x, sr, model.samplerate)
    ref = x.mean(0)
    x = ((x - ref.mean()) / (ref.std() + 1e-8))[None]
    with torch.no_grad():
        out = apply_model(model, x, device=device, split=True, overlap=0.25, progress=False)[0]
    out = out * (ref.std() + 1e-8) + ref.mean()
    return out[model.sources.index("vocals")].cpu().numpy().T, model.samplerate


def load_model():
    """(model, device): htdemucs on the Apple GPU when there is one, else the CPU."""
    model, device = load_model()
    return model, device


def separate_to(model, device, src, voc, meta, digest):
    """Separate `src` and write its vocals (canonical MP3, media.encode) and the meta file {sha256, model, sr}."""
    vocals, sr = separate(model, str(src), device)
    if sr != media.SR:
        vocals = julius.resample_frac(torch.from_numpy(vocals.T.copy()), sr, media.SR).numpy().T
    media.encode(vocals, media.SR, voc)
    meta.write_text(json.dumps({"sha256": digest, "model": MODEL, "sr": media.SR}))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("library", type=Path)
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    lib = args.library.resolve()
    out_dir = lib / "stems"
    out_dir.mkdir(exist_ok=True)
    model, device = load_model()

    files = args.files if args.files else list_audio(lib)
    failed = done = skipped = 0
    t0 = time.time()
    for i, rel in enumerate(files, 1):
        key, src = tag_key(rel), lib / rel
        voc, meta = lib / media.stem_rel(key), out_dir / f"{key}.json"
        try:
            digest = sha256(src)
            if not args.force and voc.exists() and meta.exists() and json.loads(meta.read_text()).get("sha256") == digest:
                skipped += 1
                continue
            t = time.time()
            separate_to(model, device, src, voc, meta, digest)
            done += 1
            print(f"[{i}/{len(files)}] {time.time() - t:5.1f}s  {rel}", flush=True)
        except Exception as e:  # one bad file must not stop the rest
            failed += 1
            print(f"[{i}/{len(files)}] FAILED {rel}: {type(e).__name__}: {e}", flush=True)
    print(f"\ndone: {done} separated, {skipped} up to date, {failed} failed, {time.time() - t0:.0f}s on {device}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
