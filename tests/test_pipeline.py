"""Runnable check for the Cloud Run jobs (algo_rhythm.pipeline), on this machine:

    python -m tests.test_pipeline

Originals: 6 synthetic tracks written as 48 kHz WAV in a temp "bucket" folder (file://), so decoding and resampling
run. Demucs is replaced by the tracks' own vocals (the real one needs PyTorch; it runs in the job image). Two tasks
split the work; then: the songs, analyses, features and files are where the server looks; a second run skips them;
a party plays from the result.
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

import numpy as np
import soundfile as sf


def main():
    tmp = Path(tempfile.mkdtemp())
    src, web, cache = tmp / "originals", tmp / "web", tmp / "cache"
    for d in (src, web, cache):
        d.mkdir()
    os.environ.update(DATABASE_URL=f"sqlite:///{tmp / 'db.sqlite'}", SOURCE_URL=f"file://{src}", STORAGE_URL=f"file://{web}",
                      LIBRARY_DIR=str(cache), MC_TTS="none")
    from tools import make_test_track as mt
    import librosa
    voices = {}
    for i in (0, 3, 6, 9, 12, 13):
        bpm, tonic, mode, energy = mt.LIBRARY[i]
        name = f"phase{i % 2 + 1}/Song {i:02d} - Film {i}.wav"
        (src / name).parent.mkdir(exist_ok=True)
        t = mt.make_track(str(tmp / "x.wav"), bpm, tonic, mode, energy, bars=48, seed=i, vocals=i != 13)
        x, sr = sf.read(tmp / "x.wav")
        sf.write(src / name, librosa.resample(x.T, orig_sr=sr, target_sr=48000).T, 48000)
        voices[Path(name).stem] = t.get("vocals")

    # Demucs stand-in: the track's true vocals (none for the instrumental one), through the real encoder
    from algo_rhythm import media
    fake = types.ModuleType("algo_rhythm.stems")
    fake.load_model = lambda: (None, "cpu")

    def separate_to(model, device, src_path, voc, meta, digest):
        v = voices[Path(src_path).stem]
        if v is None:
            v = np.zeros((int(sf.info(src_path).frames), 2))
        media.encode(v, media.SR, voc)
        Path(meta).write_text(json.dumps({"sha256": digest, "model": "fake", "sr": media.SR}))
    fake.separate_to = separate_to
    sys.modules["algo_rhythm.stems"] = fake
    import algo_rhythm
    algo_rhythm.stems = fake

    from sqlalchemy import func, select
    from algo_rhythm import db, library, party, pipeline, repository
    for task in (0, 1):
        os.environ.update(CLOUD_RUN_TASK_INDEX=str(task), CLOUD_RUN_TASK_COUNT="2")
        assert pipeline.run_songs() == 0, f"task {task} failed"
    with repository.session() as s:
        files = sorted(s.scalars(select(db.Song.file)))
        n_feat = s.scalar(select(func.count()).select_from(db.SongFeature))
        tag = s.scalar(select(db.SongAnalysis.tag).join(db.Song).where(db.Song.file == "phase1/Song 00 - Film 0.mp3"))
    assert files == sorted(f"phase{i % 2 + 1}/Song {i:02d} - Film {i}.mp3" for i in (0, 3, 6, 9, 12, 13)), files
    assert n_feat == 3 * len(files), n_feat
    assert abs(tag["tempo"]["bpm"] - 120) < 5 and tag["vibe"]["vocal_source"] == "demucs", (tag["tempo"], tag["vibe"]["vocal_source"])
    for f in files:
        info = sf.info(web / f)
        assert info.samplerate == 44100 and info.channels == 2 and info.format == "MP3", (f, info)
        assert (web / media.stem_rel(f[:-4].replace("/", "__"))).exists(), f
    assert not list(cache.rglob("*.mp3")), "local copies are removed once uploaded"
    print(f"{len(files)} songs analysed by 2 tasks, {n_feat} features, audio and stems in the bucket")

    os.environ.update(CLOUD_RUN_TASK_INDEX="0", CLOUD_RUN_TASK_COUNT="1")
    assert pipeline.run_songs() == 0 and pipeline.done_files() == set(files), "a rerun skips what is done"

    # a party plays from what the jobs made: the server fetches audio from the "bucket" into its cache
    library.reset()
    lib = library.get()
    assert len(lib.songs) == len(files), len(lib.songs)
    code, _ = party.create("medium")
    party.start(code)
    assert party.advance(code)["status"] == "ready"
    eng = party.view(code, host=True)["engine"]
    assert len(eng["items"]) == 2 and eng["items"][1]["join"]["plays"], eng
    print("PIPELINE PASS (a party played its first join from the jobs' output)")


if __name__ == "__main__":
    main()
