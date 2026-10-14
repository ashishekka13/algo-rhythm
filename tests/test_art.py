"""Runnable check for song art (no AI calls): python -m tests.test_art"""
import base64
import io
import json
import tempfile
from pathlib import Path

from PIL import Image

from algo_rhythm import art


def main():
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
    print("ART PASS")


if __name__ == "__main__":
    main()
