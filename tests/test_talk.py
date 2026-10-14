"""Runnable check for algo_rhythm.genai and algo_rhythm.talk: fake Gemini answers, the rules fallback, apply(), and the gateway's
cache, daily cap, schema check and 429 handling with a fake HTTP layer. No real Gemini calls.

    python -m tests.test_talk [LIBRARY]       (default: ../song-matcher/library_100, read only)
"""
import os
import sys
from pathlib import Path

from algo_rhythm import dj, genai, talk

LIB = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / "song-matcher/library_100")
STATE = {"level": "medium", "now": "Kesariya - Brahmastra", "next": ["Makhna - Drive"], "status": "playing"}


def idx(songs, needle):
    return next(i for i, s in enumerate(songs) if needle in s["name"])


def fake(answer):
    return lambda *a, **k: answer


def raiser(exc):
    def f(*a, **k):
        raise exc
    return f


def check_interpret(songs):
    kc = idx(songs, "Kala Chashma")
    r = talk.interpret("peak pe le chalo, Kala Chashma bajao", STATE, songs,
                       generate=fake({"reply": "Peak time!", "level": "max", "songs": [kc], "command": "none"}))
    assert r == {"reply": "Peak time!", "source": "gemini",
                 "actions": [{"type": "set_level", "level": "max"}, {"type": "request", "file": songs[kc]["file"]}]}, r
    r = talk.interpret("kuch romantic", STATE, songs, generate=fake(
        {"reply": "Romance it is", "level": "none", "mood": "romantic", "songs": [], "command": "none"}))
    assert r["actions"] == [{"type": "set_mood", "mood": "romantic"}], r
    # unknown and repeated indices are dropped; at most 3 requests
    r = talk.interpret("x", STATE, songs, generate=fake(
        {"reply": "ok", "level": "none", "songs": [9999, -1, 0, 0, 1, 2, 3], "command": "skip"}))
    assert [a.get("file") for a in r["actions"]] == [songs[0]["file"], songs[1]["file"], songs[2]["file"], None], r
    assert r["actions"][-1] == {"type": "skip"}
    # schema violation from the real validator (through generate) and quota both fall back to the rules
    for gen in (raiser(genai.GenAIError("bad")), raiser(genai.QuotaError("429"))):
        r = talk.interpret("party time", STATE, songs, generate=gen)
        assert r["source"] == "rules" and r["actions"] == [{"type": "set_level", "level": "high"}, {"type": "set_mood", "mood": "party"}], r
        assert "simple rules" in r["reply"]
    # the prompt carries the numbered library and the state
    seen = {}

    def spy(task, pv, parts, schema, cache=None):
        seen.update(task=task, pv=pv, text=parts[0]["text"])
        return {"reply": "ok", "level": "none", "songs": [], "command": "none"}
    talk.interpret("hi", STATE, songs, generate=spy)
    assert seen["task"] == "talk" and seen["pv"] == "talk-v3"
    assert f"{kc}|{songs[kc]['name']}|" in seen["text"] and "playing Kesariya" in seen["text"]


def check_rules(songs):
    kc, jk = idx(songs, "Kala Chashma"), idx(songs, "Jamal Kudu")
    paused = {**STATE, "status": "paused"}
    cases = [  # (text, state, expected actions)
        ("chill vibes please", STATE, [{"type": "set_level", "level": "low"}, {"type": "set_mood", "mood": "chill"}]),
        ("party time!", {**STATE, "mood": "party"}, [{"type": "set_level", "level": "high"}]),  # mood already held
        ("let's groove", {**STATE, "level": "max"}, [{"type": "set_level", "level": "medium"}]),
        ("party time!", STATE, [{"type": "set_level", "level": "high"}, {"type": "set_mood", "mood": "party"}]),
        ("ab dhamaka karo", STATE, [{"type": "set_level", "level": "max"}, {"type": "set_mood", "mood": "party"}]),
        ("thoda slow karo", STATE, [{"type": "set_level", "level": "low"}]),
        ("slow down", {**STATE, "level": "low"}, []),  # already lowest
        ("aur tez", STATE, [{"type": "set_level", "level": "high"}]),
        ("speed up", {**STATE, "level": "max"}, []),
        ("next please", STATE, [{"type": "skip"}]),
        ("agla gaana", STATE, [{"type": "skip"}]),
        ("pause", STATE, [{"type": "pause"}]),
        ("ruko yaar", STATE, [{"type": "pause"}]),
        ("resume", paused, [{"type": "play"}]),
        ("play kala chashma", STATE, [{"type": "request", "file": songs[kc]["file"]}]),
        ("Jamal Kudu bajao", STATE, [{"type": "request", "file": songs[jk]["file"]}]),
        ("kuch romantic bajao", paused, [{"type": "set_mood", "mood": "romantic"},  # already medium
                                         {"type": "play"}]),
        ("what is the weather", STATE, []),
    ]
    for text, state, want in cases:
        r = talk.rules(text, state, songs)
        assert r["source"] == "rules" and r["actions"] == want, (text, r)


def check_apply():
    calls = []
    res = {"actions": [{"type": "set_level", "level": "high"}, {"type": "request", "file": "a.mp3"},
                       {"type": "request", "file": "bad.mp3"}, {"type": "skip"}, {"type": "pause"}, {"type": "play"}]}

    def request(f):
        if f == "bad.mp3":
            raise ValueError("unknown song")
        calls.append(("request", f))
    done = talk.apply(res, set_level=lambda lv: calls.append(("level", lv)), request=request,
                      skip=lambda: calls.append(("skip",)), play=lambda: calls.append(("play",)),
                      pause=lambda: calls.append(("pause",)))
    assert calls == [("level", "high"), ("request", "a.mp3"), ("skip",), ("pause",), ("play",)], calls
    assert done == ["level high", "requested a.mp3", "request failed: unknown song", "skip", "pause", "play"], done


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def answer(obj):
    import json
    return FakeResp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]})


def check_genai(backend):
    os.environ["GEMINI_BACKEND"] = backend
    os.environ["GEMINI_API_KEY"] = "test-key"
    os.environ["GEMINI_DAILY_CAP"] = "5"
    genai.BACKOFF_S = 0
    sent, replies, log = [], [], []
    genai.requests.post = lambda url, headers, json, timeout: (sent.append((url, headers)), replies.pop(0))[1]
    on_call = lambda *a: log.append(a)
    reset = lambda: genai._used.update(day=None, n=0)
    schema = talk.SCHEMA
    good = {"reply": "ok", "level": "high", "songs": [1], "command": "none"}
    parts = [{"text": "hello"}]

    # good answer, then the same request comes from the cache with no HTTP call
    reset()
    cache = genai.MemoryCache()
    replies[:] = [answer(good)]
    assert genai.generate("t", "v1", parts, schema, cache=cache, on_call=on_call) == good
    assert genai.generate("t", "v1", parts, schema, cache=cache, on_call=on_call) == good
    assert len(sent) == 1 and sent[0][1] == {"x-goog-api-key": "test-key"} and "gemini-3.5-flash-lite" in sent[0][0]
    host = {"vertex": "https://aiplatform.googleapis.com/v1/publishers/google/models/",
            "studio": "https://generativelanguage.googleapis.com/v1beta/models/"}[backend]
    assert sent[0][0] == host + "gemini-3.5-flash-lite:generateContent", sent[0][0]
    assert [(c[3], c[4]) for c in log] == [(False, True), (True, True)], log
    # a different prompt_version is a different key
    assert genai.cache_key("m", "t", "v1", parts, schema) != genai.cache_key("m", "t", "v2", parts, schema)

    # schema violations: bad enum, missing field, wrong type -> GenAIError, nothing cached
    for bad in ({**good, "level": "loud"}, {"reply": "ok"}, {**good, "songs": ["1"]}, [good]):
        replies[:] = [answer(bad)]
        try:
            genai.generate("t", "v1", [{"text": str(bad)}], schema, cache=cache, on_call=on_call)
            raise AssertionError(f"accepted {bad}")
        except genai.GenAIError as e:
            assert not isinstance(e, genai.QuotaError)
    assert log[-1][4] is False and log[-1][6] == "GenAIError"

    # 429 -> QuotaError at once, no retry
    reset()
    sent.clear()
    replies[:] = [FakeResp(429, {"error": {"message": "Quota exceeded. details"}})]
    try:
        genai.generate("t", "v1", [{"text": "q"}], schema, cache=cache)
        raise AssertionError("no QuotaError")
    except genai.QuotaError:
        assert len(sent) == 1

    # transient 503s are retried (3 times at most), then succeed or give up
    reset()
    sent.clear()
    replies[:] = [FakeResp(503, {}), FakeResp(503, {}), answer(good)]
    assert genai.generate("t", "v1", [{"text": "r"}], schema, cache=cache) == good and len(sent) == 3
    reset()
    sent.clear()
    replies[:] = [FakeResp(503, {})] * 4
    try:
        genai.generate("t", "v1", [{"text": "r2"}], schema, cache=cache)
        raise AssertionError("no GenAIError")
    except genai.GenAIError:
        assert len(sent) == 4

    # daily cap: 5 requests allowed, the 6th raises QuotaError before any HTTP call
    reset()
    sent.clear()
    replies[:] = [answer(good) for _ in range(5)]
    for n in range(5):
        genai.generate("t", "v1", [{"text": f"cap{n}"}], schema, cache=cache)
    try:
        genai.generate("t", "v1", [{"text": "cap5"}], schema, cache=cache)
        raise AssertionError("cap not enforced")
    except genai.QuotaError:
        assert len(sent) == 5
    # a cached answer still works after the cap
    assert genai.generate("t", "v1", [{"text": "cap0"}], schema, cache=cache) == good

    # talk through the real gateway: a schema violation becomes the rules fallback
    reset()
    replies[:] = [answer({**good, "level": "loud"})]
    songs = [{"file": "a.mp3", "name": "Party Song", "energy_pct": 0.9, "llm": None}]
    r = talk.interpret("party now", STATE, songs, cache=genai.MemoryCache())
    assert r["source"] == "rules" and r["actions"] == [{"type": "set_level", "level": "high"}, {"type": "set_mood", "mood": "party"}], r


def main():
    songs, _ = dj.load_library(LIB)
    assert len(songs) > 100, len(songs)
    check_interpret(songs)
    check_rules(songs)
    check_apply()
    for backend in ("vertex", "studio"):
        check_genai(backend)
    os.environ.update(VERTEX_LOCATION="europe-west4", GOOGLE_CLOUD_PROJECT="p1", GEMINI_BACKEND="vertex")
    assert genai.model_url("m", "predict") == ("https://europe-west4-aiplatform.googleapis.com/v1/projects/p1/locations/"
                                               "europe-west4/publishers/google/models/m:predict")
    print(f"ok: talk + genai checks passed ({len(songs)} songs)")


if __name__ == "__main__":
    main()
