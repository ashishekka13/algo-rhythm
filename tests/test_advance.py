"""Runnable check of /advance when things go wrong, and of its timing log:

    python -m tests.test_advance [LIBRARY]      (default test_library, see tests/test_recipe.py)

Temp SQLite database (TEST_DATABASE_URL: PostgreSQL instead). A party is started, then:
  1. a normal join logs one JSON line saying where its time went (stages, rubberband runs);
  2. a join whose render throws is made again as an echo-out into the same song;
  3. a song that cannot be mixed into at all is dropped at once (no stale "rendering" mark), and the next call
     mixes into another song;
  4. a blend whose incoming part turns out too short for the overlap becomes an echo-out (it used to leave the join
     without a plan, and making its recipe crashed).
"""
import copy
import json
import logging
import os
import sys
import tempfile
from pathlib import Path


def main(library="test_library"):
    library = Path(library).resolve()
    tmp = Path(tempfile.mkdtemp())
    os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp / 'algo_rhythm.db'}"
    os.environ["LIBRARY_DIR"] = str(library)
    os.environ["MC_TTS"] = "none"
    os.environ.pop("STORAGE_URL", None)
    from algo_rhythm import importer, joins, library as lib_mod, party, recipe
    importer.main([str(library), "--database-url", os.environ["DATABASE_URL"]])
    lib_mod.reset()
    lines = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())
    logging.getLogger("algo_rhythm.party").addHandler(Grab())
    logging.getLogger("algo_rhythm.party").setLevel(logging.INFO)

    def go_on(code):
        """Put the playhead just after the newest join, so the next call renders the one after it."""
        tl = party.read(code)[1]["timeline"]
        party.sync(code, tl[-1]["start_s"] + 1, "playing")

    code, _ = party.create("medium")
    party.start(code)

    # 1. a normal join, and its timing line
    assert party.advance(code)["status"] == "ready"
    timing = [json.loads(m.split(" timing ", 1)[1]) for m in lines if " timing " in m]
    assert len(timing) == 1, lines
    tm = timing[0]
    assert tm["status"] == "ready" and {"claim", "rebuild", "render", "mc", "recipe", "attach", "clip"} <= set(tm["stages_s"]), tm
    assert tm["calls"].get("rubberband", {}).get("n", 0) >= 1 or tm["calls"].get("echo_try"), tm
    print(f"1. timing: {tm['total_s']:.1f} s, stages {tm['stages_s']}, calls {tm['calls']}")

    # 2. the render throws: an echo-out into the same song instead
    real = joins.render_join

    def blend_breaks(*args, **kw):
        if args[5]["transition_in"] == "blend":
            raise RuntimeError("test: the blend broke")
        return real(*args, **kw)
    go_on(code)
    joins.render_join = blend_breaks
    try:
        for _ in range(4):  # until the planned join is a blend (the fallback only shows there)
            st = party.read(code)[1]
            r = party.advance(code)
            last = party.read(code)[1]["timeline"][-1]
            if last["entry"]["transition_in"] == "echo" and any("could not be made" in x["msg"] if isinstance(x, dict) else "could not be made" in str(x)
                                                                for x in party.read(code)[1]["log"]):
                break
            go_on(code)
    finally:
        joins.render_join = real
    assert r["status"] == "ready" and last["item"]["join"]["type"] == "echo", (r, last["entry"]["transition_in"])
    print("2. a failed blend became an echo-out into the same song")

    # 3. nothing can be mixed into the next song: it is dropped at once, the next call mixes into another
    go_on(code)
    joins.render_join = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("test: nothing works"))
    n_before = len(party.read(code)[1]["timeline"])
    try:
        r = party.advance(code)
    finally:
        joins.render_join = real
    st = party.read(code)[1]
    assert r["status"] == "failed" and len(st["timeline"]) == n_before, (r, len(st["timeline"]), n_before)
    assert "rendering" not in st["timeline"][-1], "the claim is freed"
    dropped = [str(x) for x in st["log"] if "could not be mixed in" in str(x)]
    assert dropped, st["log"][-3:]
    r = party.advance(code)
    st = party.read(code)[1]
    assert r["status"] == "ready" and len(st["timeline"]) == n_before + 1, (r, len(st["timeline"]))
    print(f"3. {dropped[-1][:90]}... and the next call mixed into another song")

    # 4. an incoming part too short for the blend's overlap: an echo-out, and its recipe can be made
    lib = lib_mod.get()
    real_prep = joins.prepare_incoming
    calls = []

    def short_first(ex, tail):
        ex = real_prep(ex, tail)
        if not calls:  # only the first part auditioned: 8 beats
            n = int(ex.beats[8] * joins.SR)
            ex.audio, ex.voc = ex.audio[:n], (ex.voc[:n] if ex.voc is not None else None)
            ex.beats = ex.beats[:9].copy()
            ex.beats[-1] = n / joins.SR
        calls.append(1)
        return ex
    planner = lib.planner()
    a_entry = planner.first_song(0.5, "Synth 03")
    i = next(k for k, s in enumerate(lib.songs) if "Synth 06" in s["file"])
    b_entry = planner.entry(i, planner.win[i][0], {}, "blend", "test", 0.7, 0.0)
    a = joins.load(lib.root, lib.songs[a_entry["song"]], a_entry["window"])
    joins.prepare_incoming = short_first
    said = []
    try:
        j = joins.render_join(lib.root, lib.songs, a_entry, a, 0, copy.deepcopy(b_entry), say=said.append)
    finally:
        joins.prepare_incoming = real_prep
    item = recipe.join_item(a, j, recipe.next_join_at(recipe.start_item(a), 0, len(j.before)))
    assert j.plan["kind"] == "echo" and item["join"]["type"] == "echo" and any("too short" in m for m in said), (j.plan.get("kind"), said)
    print(f"4. {said[-1]}")
    print("ADVANCE PASS")


if __name__ == "__main__":
    main(*sys.argv[1:])
