"""Runnable check for search by meaning (algo_rhythm.search, genai.embed, the talk shortlist).
Fake embedder for the logic; then ONE real check with the library's stored vectors (embedding model only, no
flash-lite call).

    python -m tests.test_search [LIBRARY]       (default: ../song-matcher/library_100, read only)
"""
import os
import sys
from pathlib import Path

import numpy as np

from algo_rhythm import genai, search, talk

LIB = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / "song-matcher/library_100")
WORDS = ["rain", "party", "sad", "animal", "wedding", "love"]


def fake_embed(texts):
    """One axis per known word: a text's vector counts the words it contains."""
    return [[1.0 + t.lower().count(w) * 5 for w in WORDS[:1]] + [t.lower().count(w) for w in WORDS[1:]] for t in texts]


def fake_songs():
    names = ["Barsaat - Rain Film", "Party Song", "Sad Song", "Animal Song - Animal", "Rain Dance Party"]
    songs = [{"file": f"{i}.mp3", "name": n, "energy_pct": p, "mood": "m", "llm": None}
             for i, (n, p) in enumerate(zip(names, [0.1, 0.9, 0.05, 0.6, 0.8]))]
    for s, v in zip(songs, fake_embed([s["name"] for s in songs])):
        s["vec"] = np.asarray(v, float) / np.linalg.norm(v)
    return songs


def check_nearest():
    songs = fake_songs()
    assert search.nearest("rain", songs, k=2, embed=fake_embed)[0] in (0, 4)
    assert search.nearest("party", songs, k=2, embed=fake_embed) == [1, 4]
    assert search.nearest("rain", songs, k=5, level="low", embed=fake_embed) == [0, 2], "level filter: low only"
    assert search.nearest("rain", [{**s, "vec": None} for s in songs], embed=fake_embed) == [], "no vectors"
    # find: names containing the text first, then by meaning; an embedding failure leaves the name matches
    assert search.find("song", songs, k=3, embed=fake_embed)[:3] == [1, 2, 3]

    def broken(texts):
        raise genai.QuotaError("429")
    assert search.find("party", songs, embed=broken) == [1, 4]
    assert search.find("pa", songs, embed=broken) == [1, 4], "short text: names only, no embedding"
    # typed words match the start of name/film/singer words, with small typos; a song name is listed once
    named = [{"file": "a", "name": "Gallan Goodiyaan - Dil Dhadakne Do", "llm": {"singers": ["Shankar Mahadevan"]}},
             {"file": "b", "name": "Aaj Ki Raat - Stree 2", "llm": None}, {"file": "c", "name": "Phone Maar Di", "llm": None},
             {"file": "d", "name": "Aaj Ki Raat - Stree 2", "llm": None}]
    assert search.name_hits("galan goodiyan", named) == [0] and search.name_hits("shankar", named) == [0]
    assert search.name_hits("aaj ki rat", named) == [1], "typo found, duplicate name listed once"
    assert search.name_hits("malhar", named) == [] and search.name_hits("chiki", named) == []


class FakeResp:
    """An embedding answer in the shape of the backend the request was for."""
    def __init__(self, body):
        self.status_code, self.body = 200, body

    def json(self):
        vecs = [[float(ord(t[0]))] * genai.EMBED_DIM for t in texts(self.body)]
        if "instances" in self.body:
            return {"predictions": [{"embeddings": {"values": v}} for v in vecs]}
        return {"embeddings": [{"values": v} for v in vecs]}


def texts(body):
    if "instances" in body:
        return [r["content"] for r in body["instances"]]
    return [r["content"]["parts"][0]["text"] for r in body["requests"]]


def check_embed_cache(backend):
    key, os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY"), "test-key"
    old_backend, os.environ["GEMINI_BACKEND"] = os.environ.get("GEMINI_BACKEND"), backend
    sent, urls, real = [], [], genai.requests.post
    genai.requests.post = lambda url, headers, json, timeout: (sent.append(json), urls.append(url), FakeResp(json))[2]
    try:
        cache = genai.MemoryCache()
        a = genai.embed(["x", "y", "x"], task_type="RETRIEVAL_DOCUMENT", cache=cache)
        assert len(sent) == 1 and texts(sent[0]) == ["x", "y"], "repeated text sent once"
        assert urls[0].endswith("gemini-embedding-001:" + ("predict" if backend == "vertex" else "batchEmbedContents"))
        assert a[0] == a[2] and a[0] != a[1] and len(a[0]) == genai.EMBED_DIM
        b = genai.embed(["y", "z"], task_type="RETRIEVAL_DOCUMENT", cache=cache)
        assert len(sent) == 2 and texts(sent[1]) == ["z"], "cached"
        assert b[0] == a[1]
        genai.embed(["y"], task_type="RETRIEVAL_QUERY", cache=cache)
        assert len(sent) == 3, "another task type is another cache key"
    finally:
        genai.requests.post = real
        if old_backend is None:
            os.environ.pop("GEMINI_BACKEND", None)
        else:
            os.environ["GEMINI_BACKEND"] = old_backend
        if key is None:
            del os.environ["GEMINI_API_KEY"]
        else:
            os.environ["GEMINI_API_KEY"] = key


def check_talk_shortlist():
    songs = fake_songs() + [{"file": f"x{i}.mp3", "name": f"Filler {i}", "energy_pct": 0.5, "llm": None, "vec": None}
                            for i in range(30)]
    seen = {}

    def spy(task, pv, parts, schema, cache=None):
        seen["text"] = parts[0]["text"]
        return {"reply": "ok", "level": "none", "songs": [7, 0, 4], "command": "none"}  # 7 was not sent
    r = talk.interpret("Animal Song bajao, rain wala", {"level": "low"}, songs, generate=spy, embed=fake_embed)
    lib = seen["text"].split("Library, closest to the listener's words first (number|song|energy level|style|moment):\n")[1].split("\nListener:")[0]
    nums = [int(line.split("|")[0]) for line in lib.splitlines()]
    assert nums[0] == 3 and set(nums) == {0, 1, 2, 3, 4}, nums  # the named song first, then by meaning
    assert "3|Animal Song - Animal|" in lib
    assert [a["file"] for a in r["actions"]] == ["0.mp3", "4.mp3"], r
    # no vectors -> the whole library goes out, as before
    talk.interpret("hi", {}, [{**s, "vec": None} for s in songs], generate=spy, embed=fake_embed)
    assert seen["text"].count("\n") > 30


def check_real():
    from algo_rhythm import dj
    songs, _ = dj.load_library(LIB)
    n = search.attach(LIB, songs)
    assert n == len(songs), f"only {n}/{len(songs)} songs have a vector: run python -m algo_rhythm.search LIBRARY"
    hits = search.nearest("Animal ka Bobby Deol wala gaana", songs)
    names = [songs[i]["name"] for i in hits]
    print("Bobby Deol:", names[:8])
    assert any("Jamal Kudu" in x for x in names), names
    rain = [songs[i]["name"] for i in search.nearest("rain song", songs, k=8)]
    print("rain song:", rain)


def main():
    check_nearest()
    for backend in ("vertex", "studio"):
        check_embed_cache(backend)
    check_talk_shortlist()
    print("ok: fake embedder checks passed")
    check_real()
    print("ok: search checks passed")


if __name__ == "__main__":
    main()
