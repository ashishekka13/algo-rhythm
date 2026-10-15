"""Runnable check that a party works across two server instances (PostgreSQL only: LISTEN/NOTIFY):

    TEST_DATABASE_URL=postgresql+psycopg://... python -m tests.test_instances [LIBRARY]

Two server processes on one database, the once-a-second refresh slowed to 30 s (HUB_TICK_S) so that only the
NOTIFY can carry a change quickly. A guest's screen is on instance B; the host changes the vibe through instance A;
B's screen shows it within a second. Also: one read builds the host's and the guests' views (party.views), and the
local cache of a server running from a bucket stays within LOCAL_CACHE_MB, files the server made deleted once
uploaded.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check_cache(library):
    """storage.trim and storage.forget with a file:// "bucket"."""
    tmp = Path(tempfile.mkdtemp())
    bucket, cache = tmp / "bucket", tmp / "cache"
    (bucket / "songs").mkdir(parents=True)
    for k in range(6):
        (bucket / "songs" / f"s{k}.mp3").write_bytes(os.urandom(400_000))
    saved = os.environ.get("LIBRARY_DIR")
    os.environ.update(STORAGE_URL=f"file://{bucket}", LOCAL_CACHE_MB="1", LIBRARY_DIR=str(cache))
    from algo_rhythm import config, storage
    config.settings.cache_clear() if hasattr(config.settings, "cache_clear") else None
    try:
        for k in range(6):
            p = storage.local_path(f"songs/s{k}.mp3", cache)
            assert p.exists(), p
            time.sleep(0.01)
        kept = sorted(p.name for p in (cache / "songs").glob("*.mp3"))
        size = sum(p.stat().st_size for p in (cache / "songs").glob("*.mp3"))
        assert size <= 1_000_000 and "s5.mp3" in kept and "s0.mp3" not in kept, (kept, size)
        again = storage.local_path("songs/s0.mp3", cache)  # trimmed away: fetched again when asked for
        assert again.exists()
        made = cache / "assets" / "x.mp3"
        made.parent.mkdir(parents=True)
        made.write_bytes(b"x")
        storage.upload(made, "assets/x.mp3")
        storage.forget(made)
        assert not made.exists() and (bucket / "assets" / "x.mp3").exists()
    finally:
        os.environ.pop("STORAGE_URL", None)
        os.environ.pop("LOCAL_CACHE_MB", None)
        if saved is None:
            os.environ.pop("LIBRARY_DIR", None)
        else:
            os.environ["LIBRARY_DIR"] = saved
        config.settings.cache_clear() if hasattr(config.settings, "cache_clear") else None
    print(f"1. local cache kept {len(kept)} of 6 songs within 1 MB (oldest out, fetched again when asked); "
          "a file the server made is gone once uploaded")


def main(library="test_library"):
    library = Path(library).resolve()
    check_cache(library)
    url = os.environ.get("TEST_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        print("INSTANCES SKIPPED (needs TEST_DATABASE_URL: PostgreSQL)")
        return
    os.environ["DATABASE_URL"], os.environ["LIBRARY_DIR"], os.environ["MC_TTS"] = url, str(library), "none"
    from algo_rhythm import accounts, importer, library as lib_mod, party
    importer.main([str(library), "--database-url", url])
    lib_mod.reset()
    accounts.create("host", "host@example.com", "host-password", active=True)

    code0, _ = party.create("medium")
    both = party.views(code0)
    assert "engine" in both[True] and "engine" not in both[False] and both[False]["code"] == code0
    print("2. one read gives the host's view (with its player's part) and the guests' (without)")

    env = {**os.environ, "HUB_TICK_S": "30", "STORAGE_URL": ""}
    ports, procs = [free_port(), free_port()], []
    for p in ports:
        procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "algo_rhythm.server:asgi",
                                       "--port", str(p), "--log-level", "warning"], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
    A, B = (f"http://127.0.0.1:{p}" for p in ports)
    try:
        for base in (A, B):
            for _ in range(120):
                try:
                    if requests.get(base + "/health", timeout=2).ok:
                        break
                except requests.RequestException:
                    time.sleep(0.5)
            else:
                raise SystemExit(f"{base} did not start")
        s = requests.Session()
        s.post(A + "/api/auth/login", json={"username": "host", "password": "host-password"}).raise_for_status()
        r = s.post(A + "/api/parties", json={"level": "medium"}).json()
        code, H = r["code"], {"X-Host-Key": r["host_key"]}
        time.sleep(1.5)  # both instances are listening

        async def watch():
            import websockets
            async with websockets.connect(f"ws://127.0.0.1:{ports[1]}/ws/p/{code}") as ws:
                first = json.loads(await asyncio.wait_for(ws.recv(), 10))["state"]
                level0 = first["level"]
                t0 = time.time()
                await asyncio.to_thread(lambda: s.post(f"{A}/api/p/{code}/vibe", json={"vibe": "peak"}, headers=H).raise_for_status())
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if m.get("type") == "state" and m["state"]["level"] != level0:
                        return level0, m["state"]["level"], time.time() - t0
        before, after, took = asyncio.run(watch())
        assert after == "max" and took < 2.0, (before, after, took)
        print(f"3. a change on instance A ({before} -> {after}) reached a screen on instance B in {took:.2f} s "
              "(its own refresh every 30 s: only the NOTIFY could)")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
    print("INSTANCES PASS")


if __name__ == "__main__":
    main(*sys.argv[1:])
