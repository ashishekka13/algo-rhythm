"""Live updates over WebSockets: every screen of a party gets the party's view the moment it changes, and once a
second besides (countdowns, progress bars).

    hub.publish(code)        something changed (any thread): push the new view to that party's screens now
    await hub.serve(ws, code, host)   one screen's connection, until it closes

ponytail: one process. publish() reaches the sockets of this instance only, so the service runs with at most one
instance (and session affinity) for now. To run several, make publish() also send a PostgreSQL NOTIFY on channel
"party" with the code, and have each instance LISTEN and call _wake(code): nothing else changes.
"""
import asyncio
import json
import logging
import threading

from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketDisconnect

from algo_rhythm import party

log = logging.getLogger("algo_rhythm.hub")
TICK_S = 1.0       # a view at least this often, so clocks on the screens keep moving
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
    """Push the party's view to its screens now. Safe from any thread (request handlers run in a thread pool)."""
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
            views = {}
            for host in {h for _, h in screens}:
                views[host] = json.dumps({"type": "state", "state": await run_in_threadpool(party.view, code, host)},
                                         ensure_ascii=False)
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
