"""The browser player, in a real Chromium (Playwright), against a real server:

    .venv/bin/python -m tests.ui.player_browser [LIBRARY]     (default test_library; pip install playwright,
                                                                python -m playwright install chromium)

1. Offline: Chromium renders a join from the server's recipe with static/player.js (OfflineAudioContext), and it is
   compared with recipe.render, the same arithmetic in numpy: this checks the JavaScript (playback rates, gains,
   filters, offsets) and that Chromium decodes the MP3s with the server's timing.
2. Live: start a party on the landing page, press Play, let it ask for joins, skip to a transition and play through it;
   a guest page follows over the WebSocket. Fails on any page error.
"""
import base64
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

SCRATCH = Path(os.environ.get("SCRATCH", tempfile.gettempdir()))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port):
    import uvicorn
    from algo_rhythm import server
    cfg = uvicorn.Config(server.create_app(), host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            return srv
        time.sleep(0.1)
    raise RuntimeError("server did not start")


OFFLINE_JS = """
async ({items, media, t0, t1}) => {
  const SR = 44100, c = new OfflineAudioContext(2, Math.round((t1 - t0) * SR), SR);
  const P = new Player({onError: m => { throw new Error(m); }});
  P.attach(c); P.media = media; P.status = "playing";
  for (const it of items) P.items.set(it.index, it);
  P.anchor = {ctx: 0, music: t0};
  const plays = items.flatMap(it => P.plays(it).map(p => [it, p]));
  await Promise.all(plays.map(([it, p]) => P.load(P.playUrl(p)).p));
  for (const [it, p] of plays) {
    const end = p.end === null || p.end === undefined ? Infinity : p.end;
    if (end <= t0 || p.at >= t1) continue;
    P.make(it, p, P.buffers.get(P.playUrl(p)).buf, Math.max(p.at, t0));
  }
  for (const it of items) if (it.join && it.join.duck && it.join.duck.length) P.duck(it.join.duck, t0);
  const out = await c.startRendering();
  const a = out.getChannelData(0), b = out.getChannelData(1), x = new Float32Array(a.length * 2);
  for (let i = 0; i < a.length; i++) { x[2 * i] = a[i]; x[2 * i + 1] = b[i]; }
  let s = ""; const u = new Uint8Array(x.buffer);
  for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
  return btoa(s);
}
"""


def offline_check(page, base, library, state):
    from algo_rhythm import dj, recipe
    from tests.test_recipe import Audio, db, env
    eng = state["engine"]
    items = eng["items"]
    join = items[-1]
    J, L = join["start_s"], join["overlap_s"]
    t0, t1 = J - 3, J + L + 3
    page.goto(base + "/")
    got = np.frombuffer(base64.b64decode(page.evaluate(OFFLINE_JS, {"items": items, "media": eng["media"], "t0": t0, "t1": t1})),
                        dtype=np.float32).reshape(-1, 2).astype(float)
    # the same plays the page made, in numpy (body ends as the player sets them)
    plays = []
    by_index = {it["index"]: it for it in items}
    for it in items:
        nxt = by_index.get(it["index"] + 1)
        plays += (it["join"]["plays"] if it["join"] else []) + [{**it["body"], "end": nxt["start_s"] if nxt else it["body"]["end"]}]
    duck = [q for it in items if it["join"] for q in it["join"]["duck"]]
    want = recipe.render(plays, t0, t1, Audio(library), duck)
    want = 0.97 * dj.soft_limit(want.copy())
    n = min(len(got), len(want))
    got, want = got[:n], want[:n]
    lag = int(np.argmax(np.correlate(got[:20000, 0], want[:20000, 0], "full"))) - 19999
    err = np.abs(got - want).max()
    e_got, e_want = env(got), env(want)
    loud = e_want > e_want.max() - 30
    d = np.abs(e_got - e_want)[loud]
    print(f"offline render of a {join['join']['type']} ({L:.1f} s overlap): lag {lag} samples, max sample error {err:.3f}, "
          f"level diff median {np.median(d):.2f} dB, 95th pct {np.percentile(d, 95):.2f} dB, overall {db(got):.2f} vs {db(want):.2f} dBFS")
    problems = []
    if abs(lag) > 2:
        problems.append(f"Chromium's audio is {lag} samples off the server's timing")
    if np.median(d) > 0.5 or np.percentile(d, 95) > 1.5:
        problems.append(f"the browser's join differs in level (median {np.median(d):.2f} dB)")
    return problems


def live_check(browser, base):
    problems, errors = [], []
    host = browser.new_context()
    page = host.new_page()
    page.on("pageerror", lambda e: errors.append(f"host: {e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"host console: {m.text}"))
    page.goto(base + "/")
    # a signup gets the "our team will approve it soon" dialog and cannot sign in yet; the approved host signs in
    page.click("#toregister")
    page.fill("#rg-user", "fresh")
    page.fill("#rg-email", "fresh@example.com")
    page.fill("#rg-pass", "longenough1")
    page.click("#registerform button[type=submit]")
    page.wait_for_selector("dialog#created[open]")
    if "approve it soon" not in page.inner_text("dialog#created"):
        problems.append("the signup dialog does not say the team will approve it soon")
    page.click("dialog#created button")
    page.fill("#li-pass", "longenough1")  # the username is filled in already
    page.click("#loginform button[type=submit]")
    page.wait_for_function("document.getElementById('authmsg').textContent.includes('approval')")
    page.fill("#li-user", "host")
    page.fill("#li-pass", "host-password")
    page.click("#loginform button[type=submit]")
    page.wait_for_selector("#signedin:not([hidden])")
    errors[:] = [e for e in errors if "status of 401" not in e]  # the refused sign-in above, on purpose
    page.click("#startparty")
    page.wait_for_url("**/p/*/host")
    code = page.url.rstrip("/").split("/")[-2]
    page.wait_for_selector("#transport")
    page.click("#transport")
    page.wait_for_function("P && P.status === 'playing'", timeout=30000)
    page.wait_for_function("P.items.size >= 2", timeout=60000)  # the page asked for the next join and got it
    t_a = page.evaluate("P.now()")
    time.sleep(2)
    t_b = page.evaluate("P.now()")
    if not 1.5 < t_b - t_a < 2.6:
        problems.append(f"the music clock moved {t_b - t_a:.2f} s in 2 s")
    guest = browser.new_context().new_page()
    guest.on("pageerror", lambda e: errors.append(f"guest: {e}"))
    guest.goto(f"{base}/p/{code}")
    guest.wait_for_function("document.getElementById('now') && !/Loading|Warming/.test(document.getElementById('now').textContent)", timeout=15000)
    playing_name = guest.inner_text("#now")
    # skip to 10 s before the next join and play through it
    join_at = page.evaluate("Math.max(...[...P.items.values()].map(i => i.start_s))")
    page.click("#skip")
    time.sleep(1)
    t_skip = page.evaluate("P.now()")
    if abs(t_skip - (join_at - 10)) > 2:
        problems.append(f"skip landed at {t_skip:.1f} s, not 10 s before the join at {join_at:.1f} s")
    time.sleep(14)
    t_after = page.evaluate("P.now()")
    nodes = page.evaluate("[...P.nodes.values()].reduce((n, l) => n + l.length, 0)")
    if t_after < join_at + 2:
        problems.append(f"the music did not play through the join ({t_after:.1f} s, join at {join_at:.1f} s)")
    page.wait_for_function("P.items.size >= 3", timeout=60000)  # on air: the next join was asked for
    server_ph = page.evaluate(f"fetch('/api/p/{code}/state').then(r => r.json()).then(s => s.playhead)")
    if abs(server_ph - page.evaluate("P.now()")) > 3:
        problems.append(f"the server's playhead {server_ph:.1f} s is not the player's")
    page.screenshot(path=str(SCRATCH / "host.png"))
    guest.screenshot(path=str(SCRATCH / "guest.png"))
    page.click("#transport")  # pause
    page.wait_for_function("P.status === 'paused'")
    paused = page.evaluate("P.now()")
    time.sleep(1.5)
    if abs(page.evaluate("P.now()") - paused) > 0.05:
        problems.append("the clock ran on while paused")
    print(f"live: party {code}, guest sees '{playing_name}', skipped to {t_skip:.1f} s, played through the join at "
          f"{join_at:.1f} s to {t_after:.1f} s with {nodes} nodes; server playhead {server_ph:.1f} s")
    return problems + errors


def main(library="test_library"):
    library = Path(library).resolve()
    tmp = Path(tempfile.mkdtemp())
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp / 'algo_rhythm.db'}"
    os.environ["LIBRARY_DIR"] = str(library)
    os.environ.pop("STORAGE_URL", None)
    os.environ["MC_TTS"] = "none"
    from playwright.sync_api import sync_playwright
    from algo_rhythm import importer, library as lib_mod, party
    importer.main([str(library), "--database-url", os.environ["DATABASE_URL"]])
    lib_mod.reset()
    port = free_port()
    serve(port)
    base = f"http://127.0.0.1:{port}"
    from algo_rhythm import accounts
    accounts.create("host", "host@example.com", "host-password", active=True)
    code, _ = party.create("medium")
    party.start(code)
    party.advance(code)
    state = party.view(code, host=True)
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        problems += offline_check(browser.new_page(), base, library, state)
        problems += live_check(browser, base)
        browser.close()
    if problems:
        print("\nBROWSER FAIL\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("\nBROWSER PASS")


def db(x):
    return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)


if __name__ == "__main__":
    main(*sys.argv[1:])
