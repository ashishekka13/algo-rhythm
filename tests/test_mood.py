"""Crowd mood: a dance song is followed by dance, a sad one by sad/romantic. Run: python -m tests.test_mood"""
from algo_rhythm import dj, live_plan


def song(style, moment, lyric):
    return {"llm": {"style": style, "moment": moment, "lyric_mood": lyric}}


def main():
    dance = song("dance_pop", "groove", "peppy")
    party = song("punjabi", "peak", "swag")
    banger = song("folk", "peak", "devotional")  # Jai Jai Shivshankar: devotional lyrics, peak-hour song
    sad = song("ballad", "slow_dance", "heartbroken")
    romantic = song("ballad", "warmup", "romantic")
    assert dj.mood_family(banger["llm"]) == "party"
    assert dj.mood_family(song("ballad", "warmup", "devotion")["llm"]) == "romantic"  # devoted in love
    assert dj.mood_family(song("sufi", "warmup", "devotional")["llm"]) == "devotional"
    assert dj.mood_family(song("ballad", "warmup", "devoted")["llm"]) == "romantic"
    assert dj.mood_family(song("indie_acoustic", "warmup", "xyz")["llm"]) == "chill"  # unknown lyric -> style
    assert dj.mood_fit(dance, party) >= 0.9
    assert dj.mood_fit(dance, sad) < dj.MIN_MOOD and dj.mood_fit(sad, dance) < dj.MIN_MOOD
    assert dj.mood_fit(sad, romantic) >= dj.MIN_MOOD
    assert dj.mood_fit({"llm": None}, dance) == 0.6  # no Gemini answer: neutral, never gated out
    assert all(t[3] == dj.MIN_MOOD for t in live_plan.TIERS[:-1]) and live_plan.TIERS[-1][3] == 0.0
    assert "mood" in dj.PAIR_KEYS
    print("PASS")


if __name__ == "__main__":
    main()
