"""Runnable check of the MC's line library, places and dedications (Gemini and the voice are fakes):

    python -m tests.test_mc_lines [LIBRARY]      (default test_library, see tests/test_recipe.py)

Temp SQLite database (TEST_DATABASE_URL: PostgreSQL instead).
  1. places: spellings tidied, a well-known city is said by name, anywhere else the country;
  2. the first lines for a moment are fresh and kept; once MIN_POOL fit, about one in FRESH_EVERY is fresh;
  3. a line is not offered again to the party that heard it, nor for HEARD_DAYS to its host; another host may get it;
  4. a line naming the place is kept for that place only; generic lines go anywhere; another voice reuses nothing;
  5. the checks: no other place, no names in a generic line, nothing rude or too long, no song titles;
  6. Gemini busy or too slow: a stored line, at once;
  7. a dedication through Talk to the DJ: the MC says the name over the requested song's join, a personal line kept
     for that party only; the host's MC location is set, shown, and used by the lines.
"""
import collections
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


def main(library="test_library"):
    library = Path(library).resolve()
    tmp = Path(tempfile.mkdtemp())
    os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp / 'algo_rhythm.db'}"
    os.environ["LIBRARY_DIR"] = str(library)
    os.environ["MC_TTS"] = "gemini"
    os.environ.pop("STORAGE_URL", None)
    from sqlalchemy import func, select
    from algo_rhythm import (accounts, db, genai, importer, library as lib_mod, live, mc, mc_lines, party, places,
                             repository)
    importer.main([str(library), "--database-url", os.environ["DATABASE_URL"]])
    lib_mod.reset()
    lib = lib_mod.get()

    # ---- fakes: a voice (1.5 s of tone per line) and Gemini (numbered lines, or what a check asks for)
    spoken = collections.Counter()

    def fake_voice(text):
        spoken[text] += 1
        t = np.arange(int(1.5 * 44100)) / 44100
        return np.repeat((0.2 * np.sin(2 * np.pi * 220 * t))[:, None], 2, axis=1).astype(np.float32)
    mc.gemini_tts = fake_voice
    mc._voices.clear()
    n_written = [0]
    answer = {"next": None}

    def fake_generate(task, version, parts, schema, **k):
        if answer["next"]:
            a = answer["next"]
            if isinstance(a, Exception):
                raise a
            if callable(a):
                return a(parts)
            return a
        n_written[0] += 1
        return {"line": f"Number {n_written[0]} let us keep moving", "uses_place": False, "names": []}

    # 1. places
    assert places.spoken(places.parse("Bangalore, India")) == ("Bengaluru", "bengaluru")
    assert places.spoken(places.parse("Nashik, India")) == ("India", "india")
    assert places.spoken(places.parse("bombay")) == ("Mumbai", "mumbai")
    assert places.spoken(places.tidy("New Delhi (block N)", "India")) == ("Delhi", "delhi")
    assert places.parse("") is None and places.spoken(None) == (None, None)
    assert places.lookup("10.1.2.3") is None and places.lookup("not an ip") is None
    print("1. places: Bangalore -> Bengaluru, Nashik -> India, Bombay -> Mumbai; private addresses -> none")

    accounts.create("dj_a", "a@example.com", "password-a", active=True)
    accounts.create("dj_b", "b@example.com", "password-b", active=True)
    with repository.session() as s:
        a_id, b_id = (s.scalar(select(db.User.id).where(db.User.username == n)) for n in ("dj_a", "dj_b"))
    voice = mc_lines.voice_id()
    M = lambda party_id, owner, **kw: mc_lines.Moment(party_id=party_id, owner_id=owner, level=kw.pop("level", "high"),
                                                      voice=kw.pop("voice", voice), **kw)
    pick = lambda m, rng: mc_lines.pick(m, rng, party.speak, generate=fake_generate)

    # 2. fresh until the pool fills, then mostly reused
    rng = random.Random(1)
    fresh_first = []
    for k in range(mc_lines.MIN_POOL):
        row, audio = pick(M(1000 + k, None), rng)
        fresh_first.append(row.text)
        mc_lines.heard(row.id, M(1000 + k, None))
    assert n_written[0] == mc_lines.MIN_POOL and len(set(fresh_first)) == mc_lines.MIN_POOL, (n_written[0], fresh_first)
    before = n_written[0]
    for k in range(60):
        row, _ = pick(M(2000 + k, None), rng)
        mc_lines.heard(row.id, M(2000 + k, None))
    share = (n_written[0] - before) / 60
    assert 0.2 <= share <= 0.5, share
    with repository.session() as s:
        n_lines = s.scalar(select(func.count()).select_from(db.MCLine))
    renders = sum(spoken.values())
    assert renders == n_lines, (renders, n_lines)  # a stored line is never rendered again
    print(f"2. first {mc_lines.MIN_POOL} lines fresh; then {share:.0%} fresh over 60 picks; {n_lines} lines kept, each rendered once")

    # 3. heard by this party, or by its host this week: not again; another host may hear it
    p1, _ = party.create("medium", a_id)
    pid1 = party.read(p1)[0]
    m1 = M(pid1, a_id)
    got = [pick(m1, random.Random(k)) for k in range(3)]
    for row, _ in got:
        mc_lines.heard(row.id, m1)
    heard_ids = {row.id for row, _ in got}
    p2, _ = party.create("medium", a_id)
    later = {r.id for r in mc_lines.candidates(M(party.read(p2)[0], a_id), limit=500)}
    other = {r.id for r in mc_lines.candidates(M(party.read(p2)[0] + 1000, b_id), limit=500)}
    assert not heard_ids & later and heard_ids <= other, (heard_ids & later, heard_ids - other)
    print("3. lines a party heard are kept from it and from its host for a week; another host still gets them")

    # 4. place lines stay at their place; another voice reuses nothing
    answer["next"] = {"line": "What's up Bengaluru, get ready for the next banger", "uses_place": True, "names": []}
    blr = M(5000, None, place=("Bengaluru", "bengaluru"), level="max")
    row, _ = mc_lines.pick(blr, random.Random(0), party.speak, generate=fake_generate)
    answer["next"] = None
    assert row.scope == "location" and row.place == "bengaluru", (row.scope, row.place)
    assert row.id in {r.id for r in mc_lines.candidates(M(5001, None, place=("Bengaluru", "bengaluru"), level="max"))}
    assert row.id not in {r.id for r in mc_lines.candidates(M(5002, None, place=("Mumbai", "mumbai"), level="max"))}
    assert row.id not in {r.id for r in mc_lines.candidates(M(5003, None, level="max"))}
    assert not mc_lines.candidates(M(5004, None, voice="gemini:Puck:other"), limit=500)
    print("4. 'What's up Bengaluru' is kept for Bengaluru parties only; another voice reuses nothing")

    # 5. the checks
    blr = M(1, None, place=("Bengaluru", "bengaluru"), titles=frozenset({"kesariya"}))
    chk = lambda line, **kw: mc_lines.check({"line": line, "uses_place": False, "names": [], **kw}, blr)
    assert chk("Hands up everybody, let's go") == ("Hands up everybody, let's go", "generic")
    assert chk("Hello Bengaluru, hands up")[1] == "location"
    assert chk("Hello Mumbai, hands up") is None, "another place"
    assert chk("Priya, this one is for you", names=["Priya"]) is None, "a name in a generic line"
    assert chk("Kesariya vibes, hands up") is None, "a song title"
    assert chk("Get drunk and dance all night") is None, "rude"
    assert chk("one two three four five six seven eight nine ten eleven") is None, "too long"
    ded = M(1, None, dedication={"to": "Ishan", "by": "Priya"})
    assert mc_lines.check({"line": "This one is for Ishan, from Priya", "names": ["Ishan", "Priya"]}, ded)[1] == "personal"
    assert mc_lines.check({"line": "This one is for you", "names": []}, ded) is None, "a dedication says the name"
    print("5. checks: other places, names, song titles, rude words, long lines refused; a dedication must say the name")

    # 6. Gemini busy or slow: a stored line at once
    answer["next"] = genai.RateLimited("quota")
    t0 = time.time()
    got = mc_lines.pick(M(6000, None), random.Random(3), party.speak, generate=fake_generate)
    assert got and time.time() - t0 < 2, "busy: a stored line"
    mc_lines.DEADLINE_S, real_deadline = 0.3, mc_lines.DEADLINE_S
    answer["next"] = lambda parts: time.sleep(2) or {"line": "too late", "uses_place": False, "names": []}
    t0 = time.time()
    got = mc_lines.pick(M(6001, None), random.Random(3), party.speak, generate=fake_generate)
    took = time.time() - t0
    mc_lines.DEADLINE_S, answer["next"] = real_deadline, None
    assert got and took < 1.5, took
    print(f"6. Gemini busy or slow: a stored line instead ({took:.1f} s)")

    # 7. a dedication through Talk to the DJ, said over the requested song's join; the host's MC location
    def fake_post(model, parts, schema, timeout):
        props = schema.get("properties", {})
        if "dedicate_to" in props:  # talk: request song i, dedicated to Ishan
            return {"reply": "Sure, for Ishan!", "level": "none", "mood": "none", "songs": [want_i], "command": "none",
                    "dedicate_to": "Ishan"}
        if "uses_place" in props:
            text = parts[0]["text"]
            if "dedicates the next song to Ishan" in text:
                return {"line": "This one is for Ishan, Bengaluru, from Priya", "uses_place": True, "names": ["Ishan", "Priya"]}
            return {"line": "Bengaluru, make some noise", "uses_place": True, "names": []}
        return {k: ["Hands up everybody"] * 3 for k in mc.KEYS}
    genai._post = fake_post
    real_room, real_place = live.mc_room, mc.placement
    live.mc_room = lambda overlap, nxt, nxt_skip, voc, info, sr=44100: (np.zeros((44100 * 30, 2)), [], 30.0)
    mc.placement = lambda music, segs, voice, sr, start_s=0.0, end_s=30.0: (2.0, 1.0)
    try:
        code, key = party.create("medium", a_id, places.parse("Bangalore, India"))
        v = party.view(code, host=True)
        assert v["location"] == {"text": "Bengaluru, India", "say": "Bengaluru", "source": "ip"}, v["location"]
        party.set_location(code, "Bombay")
        assert party.view(code, host=True)["location"]["say"] == "Mumbai" and "location" not in party.view(code)
        party.set_location(code, "Bengaluru, India")
        party.start(code)
        st = party.read(code)[1]
        played = {e["entry"]["song"] for e in st["timeline"]} | {e["song"] for e in st["queue"][:1]}
        want_i = next(i for i, s in enumerate(lib.songs) if s["file"] not in played and not s["file"].startswith("_"))
        want = lib.songs[want_i]["file"]
        said = party.talk(code, "Play this one and dedicate it to Ishan", guest=True, name="Priya")
        assert any(d.startswith("dedication to Ishan") for d in said["done"]), said
        st = party.read(code)[1]
        assert st["mc"]["dedications"][0]["file"] == want and st["mc"]["dedications"][0]["by"] == "Priya"
        for _ in range(6):
            tl = party.read(code)[1]["timeline"]
            party.sync(code, tl[-1]["start_s"] + 1, "playing")
            assert party.advance(code)["status"] == "ready"
            st = party.read(code)[1]
            if not st["mc"]["dedications"]:
                break
        last = st["timeline"][-1]
        mc_play = [p for p in last["item"]["join"]["plays"] if p.get("role") == "mc"]
        assert last["entry"]["song"] == want and mc_play, (last["entry"]["song"], want)
        assert any("Ishan" in str(x) for x in st["log"]), st["log"][-4:]
        with repository.session() as s:
            row = s.scalar(select(db.MCLine).where(db.MCLine.scope == "personal"))
        assert row and "Ishan" in row.text and row.party_id == party.read(code)[0], row
        assert row.id not in {r.id for r in mc_lines.candidates(M(9999, None, place=("Bengaluru", "bengaluru")), limit=500)}
        print(f"7. dedication: '{row.text}' said over the join into the requested song; kept for that party only;"
              " the host's MC location set, shown to the host only")
    finally:
        live.mc_room, mc.placement = real_room, real_place
    print("MC LINES PASS")


if __name__ == "__main__":
    main(*sys.argv[1:])
