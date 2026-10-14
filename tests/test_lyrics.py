"""Lyric word links on tiny fake lyrics files. Run: python -m tests.test_lyrics"""
import json
import tempfile
from pathlib import Path

from algo_rhythm import lyrics


def main():
    assert lyrics.norm("दिल") == lyrics.norm("Dil") == "dil"
    assert lyrics.norm("प्यार") == lyrics.norm("pyaar") == lyrics.norm("Pyar")
    assert lyrics.norm("मिर्ची") == lyrics.norm("Mirchi")
    assert lyrics.keys_of("मेरा दिल है तू झाल") == ["dil"]  # stop words and the fade-out hallucination gone
    assert lyrics.song_name("Chal_Ghar_Chalen_From_Malang_-_X_feat._Arijit [mupNjLln3nc]").split() == ["Chal", "Ghar", "Chalen"]
    assert lyrics.song_name("Kesariya - Brahmastra").strip() == "Kesariya"
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "lyrics"
        d.mkdir()
        files = {
            "a": {"title": "Song A", "words": [[10.0, 10.4, "मेरा"], [10.5, 11.0, "दिल"], [100.0, 100.5, "दिल"],
                                               [101.0, 101.5, "तेरा"], [102.0, 102.6, "मिर्ची"]]},
            "b": {"title": "Dil Diyan Gallan", "words": [[5.0, 5.4, "dil"], [6.0, 6.5, "gallan"]]},
            "c": {"title": "Mirchi", "words": [[3.0, 3.5, "jhoom"]]},
            "d": {"title": "Nothing", "words": [[1.0, 1.5, "raat"]]},
        }
        for k, v in files.items():
            (d / f"{k}.json").write_text(json.dumps({"lines": [], **v}, ensure_ascii=False))
        lib = lyrics.load_all(tmp)
        df = lyrics.doc_freq(lib)
        a, b, c, e = lib["a"], lib["b"], lib["c"], lib["d"]
        score, word, ta, tb = lyrics.word_link(a, b, df=df, n=len(lib))
        assert (word, ta, tb) == ("dil", 100.0, 5.0) and score == 0.8, (score, word, ta, tb)  # dil is B's title
        score, word, ta, tb = lyrics.word_link(a, c)
        assert (score, word, ta, tb) == (0.8, "mirchi", 102.0, 3.0)  # Mirchi lyric into a song called Mirchi
        assert lyrics.word_link(a, e)[1] is None
        assert lyrics.word_link(a, b, a_exit=50.0)[2] is None  # no "dil" within 20 s before an exit at 50 s
    print("PASS")


if __name__ == "__main__":
    main()
