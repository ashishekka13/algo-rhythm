"""Live updates over WebSockets: every screen of a party gets the party's view the moment it changes, and once a
second besides (countdowns, progress bars).

    hub.publish(code)        something changed (any thread): push the new view to that party's screens now
    await hub.serve(ws, code, host)   one screen's connection, until it closes

Several instances: a party's screens may be on different instances. publish() also sends a PostgreSQL NOTIFY on
channel "party" ("CODE|instance"), and every instance LISTENs (listen(), started with the server) and wakes its own
screens of that party. Without PostgreSQL (SQLite: tests, a laptop) it is one process and the NOTIFY is skipped.
"""
import asyncio
import concurrent.futures
import json
import logging
import os
import threading
import uuid

from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketDisconnect

from algo_rhythm import party

log = logging.getLogger("algo_rhythm.hub")
TICK_S = float(os.environ.get("HUB_TICK_S", "1.0"))  # a view at least this often, so clocks on the screens keep moving
CHANNEL = "party"
INSTANCE = uuid.uuid4().hex[:12]  # this process: its own NOTIFYs are not acted on twice
_notify_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")
MAX_SCREENS = 500  # per party: a guard, not a plan


class _Party:
    def __init__(self, loop):
        self.screens = set()           # (websocket, host)
        self.wake = asyncio.Event()
        self.task = None
        self.loop = loop


_parties = {}
_loop = {"loop": None}
_lock = threading.Lock()


def _wake(code):
    p = _parties.get(code)
    if p:
        p.wake.set()


def publish(code):
    """Push the party's view to its screens now, here and on every other instance. Safe from any thread."""
    if _pg_url():
        _notify_pool.submit(_notify, code)
    _wake_here(code)


def _wake_here(code):
    loop = _loop["loop"]
    if loop is None:
        return
    try:
        if threading.current_thread() is threading.main_thread() and loop.is_running():
            loop.call_soon(_wake, code)
        else:
            loop.call_soon_threadsafe(_wake, code)
    except RuntimeError:  # the loop is closing
        pass


async def _pump(code, p):
    """Send views to the party's screens: on every publish, and every TICK_S. Ends when the last screen leaves."""
    while p.screens:
        try:
            await asyncio.wait_for(p.wake.wait(), TICK_S)
        except asyncio.TimeoutError:
            pass
        p.wake.clear()
        screens = list(p.screens)
        if not screens:
            break
        try:
            built = await run_in_threadpool(party.views, code, tuple({h for _, h in screens}))
            views = {h: json.dumps({"type": "state", "state": v}, ensure_ascii=False) for h, v in built.items()}
        except party.NotFound:
            for ws, _ in screens:
                await ws.close(code=4404)
            break
        except Exception:
            log.exception("could not build the view of party %s", code)
            continue
        for ws, host in screens:
            try:
                await ws.send_text(views[host])
            except Exception:
                p.screens.discard((ws, host))
    if _parties.get(code) is p:
        _parties.pop(code, None)


async def serve(ws: WebSocket, code, host):
    """One screen. The host's player also reports its playhead here: {"type": "sync", "playhead": s, "status": ...}."""
    loop = _loop["loop"] = asyncio.get_running_loop()
    await ws.accept()
    p = _parties.get(code)
    if p is None or p.loop is not loop:  # first screen of this party (or a new event loop: tests)
        p = _parties[code] = _Party(loop)
    if len(p.screens) >= MAX_SCREENS:
        await ws.close(code=4429)
        return
    me = (ws, host)
    p.screens.add(me)
    if p.task is None or p.task.done():
        p.task = asyncio.create_task(_pump(code, p))
    p.wake.set()
    try:
        while True:
            msg = await ws.receive_text()
            if len(msg) > 2048:
                continue
            try:
                m = json.loads(msg)
            except ValueError:
                continue
            if m.get("type") == "sync" and host:
                try:
                    await run_in_threadpool(party.sync, code, m.get("playhead"), m.get("status"))
                except (ValueError, party.NotFound, party.Conflict) as e:
                    await ws.send_text(json.dumps({"type": "error", "error": str(e)}))
            elif m.get("type") == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        p.screens.discard(me)


# ---------------------------------------------------------------- across instances: PostgreSQL NOTIFY / LISTEN

def _pg_url():
    """The libpq URL of the database when it is PostgreSQL, else None."""
    from algo_rhythm import config
    url = config.settings().database_url or ""
    if not url.startswith("postgresql"):
        return None
    return "postgresql://" + url.split("://", 1)[1]


def _notify(code):
    from sqlalchemy import text
    from algo_rhythm import repository
    try:
        with repository.session() as s:
            s.execute(text("SELECT pg_notify(:ch, :msg)"), {"ch": CHANNEL, "msg": f"{code}|{INSTANCE}"})
            s.commit()
    except Exception:
        log.exception("could not tell the other instances about party %s", code)


async def listen():
    """Wake this instance's screens when another instance changed one of their parties. Runs for the server's life;
    reconnects after a database restart (screens still get a view every TICK_S meanwhile)."""
    url = _pg_url()
    if not url:
        return
    import psycopg
    _loop["loop"] = asyncio.get_running_loop()
    delay = 1
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
                await conn.execute(f"LISTEN {CHANNEL}")
                log.info("listening for other instances' party updates (instance %s)", INSTANCE)
                delay = 1
                async for n in conn.notifies():
                    code, _, sender = n.payload.partition("|")
                    if sender != INSTANCE:
                        _wake(code)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("party updates from other instances paused (%s); retrying in %d s", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
