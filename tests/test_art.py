"""Runnable check for song art (no AI calls): python -m tests.test_art"""
import base64
import io
import json
import os
import tempfile
from pathlib import Path

from PIL import Image

from algo_rhythm import art, genai


def main():
    os.environ.setdefault("GEMINI_API_KEY", "test-key")  # an URL without credentials; nothing is sent
    png = io.BytesIO()
    Image.new("RGB", (1536, 1536), (200, 40, 90)).save(png, "PNG")
    song = {"sha256": "ab" * 32, "name": "Kala Chashma - Baar Baar Dekho", "mood": "bright/driving",
            "llm": {"lyric_mood": "playful", "style": "dance_pop", "occasion": ["wedding"], "year": 2016, "energy": 4}}
    p = art.prompt(song)
    assert "No people's faces" in p and "Kala Chashma" not in p, "the prompt describes the feel, never the title or stars"

    sent = {}

    def fake_send(url, body, timeout):
        sent.update(url=url, body=body)
        return {"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(png.getvalue()).decode()}}]}}]}

    jpg = art.generate(song, fake_send)
    im = Image.open(io.BytesIO(jpg))
    assert im.format == "JPEG" and max(im.size) == art.MAX_SIDE, (im.format, im.size)
    assert sent["url"].endswith(f"{art.MODEL}:generateContent") and sent["body"]["generationConfig"]["responseModalities"] == ["IMAGE"]

    lib = Path(tempfile.mkdtemp())
    made, failed = art.run(lib, [song], jobs=1, send=fake_send)
    index = json.loads((lib / art.INDEX).read_text())
    assert (made, failed) == (1, 0) and index == {"abababababababab": "abababababababab.jpg"} == art.load_index(lib)
    assert art.load_index(Path(tempfile.mkdtemp())) == {}, "no art yet = empty index"
    bad = lambda url, body, timeout: {"candidates": [{"content": {"parts": [{"text": "sorry"}]}}]}
    assert art.run(lib, [song], jobs=1, send=bad) == (0, 1), "no image in the answer is a failure, not a crash"

    # a 429 waits and tries again; one that outlasts every wait stops the run
    calls, waited, art.sleep = [], [], lambda s: waited.append(s)
    def busy_once(url, body, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise genai.RateLimited("429")
        return fake_send(url, body, timeout)
    assert art.run(Path(tempfile.mkdtemp()), [song], jobs=1, send=busy_once) == (1, 0) and waited == [art.WAITS_S[0]]
    def busy(url, body, timeout):
        raise genai.RateLimited("429")
    waited.clear()
    assert art.run(Path(tempfile.mkdtemp()), [song], jobs=1, send=busy) == (0, 1) and waited == list(art.WAITS_S)
    # the daily cap stops the run and cancels the songs not yet started: nothing is paid for and dropped
    sent_n = []
    def capped(url, body, timeout):
        sent_n.append(1)
        raise genai.QuotaError("daily Gemini cap reached")
    three = [dict(song, sha256=c * 64) for c in "cde"]
    assert art.run(Path(tempfile.mkdtemp()), three, jobs=1, send=capped) == (0, 1) and len(sent_n) == 1, sent_n
    print("ART PASS")


if __name__ == "__main__":
    main()
