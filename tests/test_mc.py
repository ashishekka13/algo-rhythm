"""Runnable check for the AI MC (no speakers, no real Gemini or Cloud calls): python -m tests.test_mc"""
import base64
import io
import sys
import types

import numpy as np
import soundfile as sf

from algo_rhythm import dj, genai, live, mc, transition

SR = dj.OUT_SR


def tone(s, hz=220, amp=0.3):
    t = np.arange(int(s * SR)) / SR
    return np.repeat((amp * np.sin(2 * np.pi * hz * t))[:, None], 2, axis=1)


def fake_voice(text):
    return tone(2.5, 300, 0.2).astype(np.float32)


def check_schedule():
    m = mc.MC(speak_fn=fake_voice)
    assert not m.due(0, "medium") and not m.due(119, "medium"), "never in the first 2 minutes"
    assert not m.due(600, "medium"), "no level change: wait 20 minutes"
    assert m.due(mc.EVERY_S, "medium"), "20 minutes without a line: one is due"
    m = mc.MC(speak_fn=fake_voice)
    m.due(30, "medium")
    assert not m.due(100, "max"), "a level change in the first 2 minutes still waits"
    assert m.due(130, "max") and m.key("max") == "build", "level change after 2 minutes: due, as a build-up line"
    m.spoke("x", 130)
    assert not m.due(200, "low") and not m.due(729, "low"), "never two lines within 10 minutes, even on a level change"
    assert m.due(731, "low") and m.key("low") == "low", "the change is kept until 10 minutes have passed"
    m.spoke("y", 731)
    assert not m.due(731 + 900, "low") and m.due(731 + mc.EVERY_S, "low"), "then 20 minutes again"
    m.enabled = False
    assert not m.due(10 ** 6, "max") and m.take(10 ** 6, "max") is None, "off means off"
    print("schedule ok")


def check_pool():
    calls = []

    def gen(task, version, parts, schema, **k):
        calls.append(task)
        return {"low": ["Easy now", "Feel it", "Slow and sweet", "one two three four five six seven eight nine"],
                "medium": ["Groove karo", "Move it", "Chalo chalo"], "high": ["Party!", "Jump", "Nacho"],
                "max": ["Hands up", "Peak time", "Dhamaka"], "build": ["Higher"]}
    m = mc.MC(generate=gen, speak_fn=fake_voice)
    p = m.lines()
    m.lines()
    assert calls == ["mc"], "one Gemini call per party"
    assert p["low"] == ["Easy now", "Feel it", "Slow and sweet"], "lines over 8 words are dropped"
    assert p["build"] == mc.TEMPLATES["build"], "too few lines for a key: built-in lines"

    def down(*a, **k):
        raise genai.QuotaError("cap")
    assert mc.make_pool(down) == mc.TEMPLATES, "Gemini unavailable: built-in lines"
    m = mc.MC(generate=down, speak_fn=fake_voice)
    line, audio = m.take(mc.EVERY_S, "high", wait=True)
    assert line in mc.TEMPLATES["high"] and audio.shape[1] == 2, "a ready line for the level"
    print("pool ok")


def check_cloud():
    buf = io.BytesIO()
    sf.write(buf, 0.3 * np.sin(np.arange(12000) / 24000 * 2 * np.pi * 200), 24000, format="WAV", subtype="PCM_16")
    sent = {}

    def post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, body=json)
        return types.SimpleNamespace(status_code=200, json=lambda: {"audioContent": base64.b64encode(buf.getvalue()).decode()})
    a = mc.cloud_tts("Hands up", {"x-goog-api-key": "k"}, post=post)
    assert sent["url"] == mc.TTS_URL and sent["body"]["voice"]["languageCode"] == "en-IN" and sent["headers"]["x-goog-api-key"] == "k"
    assert a.dtype == np.float32 and a.shape[1] == 2 and abs(len(a) / SR - 0.5) < 0.02, (a.shape, len(a) / SR)
    bad = lambda *a, **k: types.SimpleNamespace(status_code=403, json=lambda: {})
    try:
        mc.cloud_tts("x", {}, post=bad)
        raise AssertionError("HTTP 403 must raise")
    except RuntimeError:
        pass
    # speak(): a Cloud or Gemini failure falls back to the local voice, and that engine rests for VOICE_RETRY_S, then is
    # tried again (it used to stay off until the server restarted: one failed request silenced the MC for good)
    real = mc.cloud_headers, mc.cloud_tts, mc.say_tts
    used = []
    mc.cloud_headers, mc.say_tts = (lambda: {"x": "y"}), (lambda t: used.append(t) or fake_voice(t))
    mc.cloud_tts = lambda t, h: (_ for _ in ()).throw(RuntimeError("HTTP 403"))
    import os
    real_gem = mc.gemini_tts
    try:
        for engine in ("cloud", "gemini"):  # either engine failing falls back to the local voice, and rests
            os.environ["MC_TTS"], used[:] = engine, []
            mc.gemini_tts = lambda t: (_ for _ in ()).throw(RuntimeError("HTTP 403"))
            mc.speak.cache_clear()
            mc._cloud["off_until"] = 0.0
            assert mc.speak("fallback line") is not None and used == ["fallback line"] and mc._voice_off(), engine
            assert mc.speak("fallback line") is not None and used == ["fallback line"], "rendered audio is cached per line"
        os.environ["MC_TTS"] = "gemini"  # resting: the local voice, without trying Gemini again
        tried = []
        mc.gemini_tts, used[:] = (lambda t: tried.append(t) or fake_voice(t)), []
        assert mc.speak("while resting") is not None and used == ["while resting"] and not tried
        mc._cloud["off_until"] = 0.0  # the rest is over: Gemini is tried again and used
        assert mc.speak("gemini line") is not None and tried == ["gemini line"] and used == ["while resting"]
        mc.gemini_tts = lambda t: None  # nothing came back: not cached, so a later call tries again
        mc.speak.cache_clear()
        mc.say_tts = lambda t: None
        assert mc.speak("silent once") is None and "silent once" not in mc._voices
    finally:
        mc.cloud_headers, mc.cloud_tts, mc.say_tts, mc.gemini_tts = *real, real_gem
        os.environ.pop("MC_TTS", None)
        mc._cloud["off_until"] = 0.0
        mc.speak.cache_clear()
    import os
    os.environ["GOOGLE_TTS_API_KEY"] = "test-key"
    try:
        assert mc.cloud_headers() == {"x-goog-api-key": "test-key"}
    finally:
        del os.environ["GOOGLE_TTS_API_KEY"]
    print("cloud TTS (fake HTTP) ok")


def check_say():
    if sys.platform != "darwin":
        print("say: skipped (not macOS)")
        return
    a = mc.say_tts("Haath upar, energy upar")
    assert a is not None and a.dtype == np.float32 and a.shape[1] == 2 and 0.5 < len(a) / SR < mc.MAX_LINE_S, None if a is None else a.shape
    print(f"say ok: voice {mc.say_voice() or 'default'}, {len(a) / SR:.2f} s")


def duck_db(mixed, music, at_s):
    """Measure the music's gain under the voice: least squares of mixed = g * music + c * voice, over the middle of
    the line (clear of the ramps)."""
    i, off = int(at_s * SR), SR // 4
    n = len(fake_voice("")) - 2 * off
    m, x = music[i + off:i + off + n].mean(axis=1), mixed[i + off:i + off + n].mean(axis=1)
    v = fake_voice("")[off:off + n].mean(axis=1)
    g, _ = np.linalg.lstsq(np.stack([m, v], axis=1), x, rcond=None)[0]
    return 20 * np.log10(g)


def stem(lines, total_s):
    """A vocal stem singing at the given (start, end) seconds."""
    v = np.zeros((int(total_s * SR), 2))
    for a, b in lines:
        v[int(a * SR):int(b * SR)] = tone(b - a, 500, 0.2)[:int(b * SR) - int(a * SR)]
    return v


def run_join(info, overlap, nxt, nxt_skip, nxt_voc):
    """Live.Engine._mc on a fake engine with an MC whose line is due and ready."""
    m = mc.MC(generate=lambda *a, **k: {}, speak_fn=fake_voice)
    said = []
    sess = types.SimpleNamespace(mc=m, level="max", _say=said.append)
    eng = types.SimpleNamespace(sess=sess, sink=types.SimpleNamespace(written=1300 * SR, realtime=False))
    out_ov, out_nxt = live.Engine._mc(eng, overlap, nxt, nxt_skip, nxt_voc, info)
    at = None if m.last_at is None else m.last_at - 1300
    return out_ov, out_nxt, at, said


def check_placement():
    rng = np.random.default_rng(1)
    noise = lambda s: 0.2 * rng.standard_normal((int(s * SR), 2))
    # echo-out: overlap = incoming song + music-only echo; the incoming singer comes in at 1-3 s, 7-12 s, 16 s on
    nxt = noise(70)
    nxt_voc = stem([(1.0, 3.0), (7.0, 12.0), (16.0, 70.0)], 70)
    nxt = nxt + nxt_voc
    n_ov = 4 * SR
    overlap = nxt[:n_ov] + 0.1 * noise(4)
    ov, nx, at, said = run_join({"type": "echo-out"}, overlap, nxt, n_ov, nxt_voc)
    segs = transition.sung_segments(nxt_voc[:30 * SR], SR)
    assert at is not None and said == [f"MC: {said[0][4:]}"], (at, said)
    assert not any(a < at + 2.5 + mc.PAD_OUT_S and at - mc.PAD_IN_S < b for a, b in segs), ("over a sung line", at, segs)
    assert 3.0 < at < 7.0, at
    stream_in = np.concatenate([overlap, nxt[n_ov:30 * SR]])
    stream_out = np.concatenate([ov, nx[n_ov:30 * SR]])
    d = duck_db(stream_out, stream_in, at)
    assert abs(d + mc.DUCK_DB) < 0.5, d
    assert np.array_equal(stream_out[:int((at - 0.2) * SR)], stream_in[:int((at - 0.2) * SR)]), "untouched before the duck"
    print(f"echo-out join: line at {at:.2f} s (gap 3.0-7.0 s), music ducked {d:.1f} dB")
    # blend with a vocal swap: outgoing voice cut at 3 s, incoming in at 9 s; 16 s overlap; incoming sings on after it
    nxt = noise(80)
    nxt_voc = stem([(0.0, 80.0)], 80)
    ov16 = noise(16)
    ov, nx, at, said = run_join({"type": "blend", "vocal_swap": True, "vocal_out_at_s": 3.0, "vocal_in_at_s": 9.0},
                                ov16, nxt, 16 * SR, nxt_voc)
    assert at is not None and 3.0 + mc.PAD_IN_S <= at and at + 2.5 + mc.PAD_OUT_S <= 9.0, at
    d = duck_db(ov, ov16, at)
    assert abs(d + mc.DUCK_DB) < 0.5, d
    print(f"blend join: line at {at:.2f} s (instrumental 3.0-9.0 s), music ducked {d:.1f} dB")
    # no instrumental stretch long enough: skip, nothing changes, the line waits for the next join
    nxt_voc = stem([(0.0, 80.0)], 80)
    ov, nx, at, said = run_join({"type": "echo-out"}, nxt[:4 * SR], nxt, 4 * SR, nxt_voc)
    assert at is None and not said and nx is nxt, "sung throughout: skip"
    # a blend without a vocal swap cannot be checked: skip
    ov, nx, at, said = run_join({"type": "blend"}, ov16, nxt, 16 * SR, nxt_voc)
    assert at is None and not said
    print("placement ok")


if __name__ == "__main__":
    check_schedule()
    check_pool()
    check_cloud()
    check_say()
    check_placement()
    print("MC PASS")
