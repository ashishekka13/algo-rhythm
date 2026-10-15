"""algo-rhythm web server: stateless. The host's browser plays and mixes the music; this serves the pages, the
library's audio, the plan (which song next, and the recipe for each join) and a party's shared state, which lives in
the database (DATABASE_URL: Cloud SQL in production, SQLite on a laptop).

    python -m algo_rhythm.server [LIBRARY]       http://127.0.0.1:8765 (PORT, HOST); Cloud Run runs the same

Pages: /  (start a party or join one), /p/CODE/host (the host: plays the music), /p/CODE (guests: requests, votes,
reactions), /p/CODE/screen (the big screen). API under /api (see /docs); live updates on the WebSocket /ws/p/CODE.
The host's actions carry its key (header X-Host-Key; ?key= on the WebSocket), returned once when the party is made.
"""
import argparse
import asyncio
import hmac
import json
import logging
import os
import re
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from algo_rhythm import accounts, config, hub, library, live, live_plan, party, places, repository, storage

log = logging.getLogger("algo_rhythm.server")
MAX_BODY = 4096
STATIC = Path(__file__).parent / "static"
CODE = re.compile(r"[A-Z2-9]{4,12}")


class JsonLogs(logging.Formatter):
    """One JSON object per line, so a log collector (Cloud Logging, Loki) can index the fields."""

    def format(self, r):
        out = {"t": self.formatTime(r, "%Y-%m-%dT%H:%M:%S"), "severity": r.levelname, "logger": r.name, "message": r.getMessage()}
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out)


def setup_logging(level="INFO"):
    h = logging.StreamHandler()
    h.setFormatter(JsonLogs())
    logging.basicConfig(level=level, handlers=[h], force=True)


def signed_url(rel):
    """A short-lived URL for a file in the bucket, so the browser downloads audio straight from Cloud Storage. None
    when these credentials cannot sign (a user login on a laptop): the server then sends the file itself."""
    try:
        import datetime
        import google.auth
        import google.auth.transport.requests
        from google.oauth2 import service_account
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not creds.valid:
            creds.refresh(google.auth.transport.requests.Request())
        # a key file signs by itself; Cloud Run's account signs through IAM (roles/iam.serviceAccountTokenCreator on
        # itself); a user login cannot sign at all (AttributeError: no service account email)
        kw = {} if isinstance(creds, service_account.Credentials) else {
            "service_account_email": creds.service_account_email, "access_token": creds.token}
        return storage._blob(rel).generate_signed_url(version="v4", expiration=datetime.timedelta(hours=2), method="GET", **kw)
    except Exception as e:
        log.warning("cannot sign a URL (%s): the server sends the file itself", type(e).__name__)
        return None


def from_edge(request):
    """True when the request came through the Cloudflare Worker in front of the domain (deploy/cloudflare/worker.js):
    it carries EDGE_SECRET, which the Worker and the service share. Only then are its forwarded headers believed."""
    secret = os.environ.get("EDGE_SECRET", "")
    return bool(secret) and hmac.compare_digest(request.headers.get("x-edge-secret", ""), secret)


def public_host(request):
    """The address the visitor typed (algo-rhythm.in through the Worker, else the run.app one)."""
    host = request.headers.get("x-forwarded-host") if from_edge(request) else None
    return host or request.headers.get("host", request.url.netloc)


def create_app():
    app = FastAPI(title="algo-rhythm", docs_url="/docs", redoc_url=None)

    @app.on_event("startup")
    async def warm():
        await run_in_threadpool(library.get)  # the first request should not wait for the library
        app.state.listener = asyncio.create_task(hub.listen())  # other instances' party updates (PostgreSQL only)

    page_html = (STATIC / "live.html").read_bytes()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method == "POST":
            # Non-simple content type: a page on another site cannot send this without a preflight we refuse.
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"error": "send application/json"}, 415)
            if not 0 < int(request.headers.get("content-length") or 0) <= MAX_BODY:
                return JSONResponse({"error": "bad body size"}, 400)
        return await call_next(request)

    async def body(request):
        try:
            data = json.loads(await request.body())
        except ValueError:
            raise ValueError("body is not JSON")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def check(code):
        if not CODE.fullmatch(code):
            raise party.NotFound("no such party")

    async def host_of(request, code):
        check(code)
        if not await run_in_threadpool(party.is_host, code, request.headers.get("x-host-key")):
            raise PermissionError("only the host can do that")

    async def answer(code, fn, host=False):
        """Run fn, tell every screen, and return the view (the host's, with its player's part, to the host). Both off
        the event loop: they wait on the database, and a wait there must never stall the other screens' sockets."""
        await run_in_threadpool(fn)
        hub.publish(code)
        return JSONResponse(await run_in_threadpool(party.view, code, host), headers={"Cache-Control": "no-store"})

    async def is_host(request, code):
        return await run_in_threadpool(party.is_host, code, request.headers.get("x-host-key"))

    @app.exception_handler(ValueError)
    async def bad_input(request, e):
        return JSONResponse({"error": str(e)}, 400)

    @app.exception_handler(KeyError)
    async def missing(request, e):
        return JSONResponse({"error": f"missing {e}"}, 400)

    @app.exception_handler(PermissionError)
    async def forbidden(request, e):
        return JSONResponse({"error": str(e)}, 403)

    @app.exception_handler(party.NotFound)
    async def not_found(request, e):
        return JSONResponse({"error": str(e)}, 404)

    @app.exception_handler(party.RateLimited)
    async def limited(request, e):
        return JSONResponse({"error": str(e)}, 429)

    @app.exception_handler(party.Conflict)
    async def conflict(request, e):
        return JSONResponse({"error": str(e)}, 409)

    # ---- pages
    def page():
        return HTMLResponse(page_html, headers={"Cache-Control": "no-store"})

    @app.get("/")
    def landing():
        return page()

    @app.get("/login")
    def login_page():  # the start page, opened at its sign-in form
        return page()

    @app.get("/p/{code}")
    def guest(code: str):
        return page()

    @app.get("/p/{code}/host")
    def host_page(code: str):
        return page()

    @app.get("/p/{code}/screen")
    def screen(code: str):
        return page()

    @app.get("/qr/{code}.svg")
    def qr(code: str, request: Request):
        """A QR code of the party's guest page, for the big screen and the host to show: scan it to join."""
        if not re.fullmatch(r"[A-Za-z0-9]{1,12}", code):
            return JSONResponse({"error": "not found"}, 404)
        import io
        import segno
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme  # Cloud Run ends TLS in front
        buf = io.BytesIO()
        segno.make(f"{scheme}://{public_host(request)}/p/{code.upper()}", error="m").save(
            buf, kind="svg", scale=8, border=2, dark="#09090B", light="#FFFFFF", xmldecl=False, svgns=True)
        return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})

    BRAND = {"icon.svg": "image/svg+xml", "favicon-32.png": "image/png", "apple-touch-icon.png": "image/png",
             "icon-192.png": "image/png", "icon-512.png": "image/png", "icon-maskable-512.png": "image/png",
             "brand.woff2": "font/woff2", "manifest.webmanifest": "application/manifest+json"}

    @app.get("/static/{name}")
    def static(name: str):
        if name in ("visuals.js", "player.js"):
            return Response((STATIC / name).read_bytes(), media_type="text/javascript", headers={"Cache-Control": "no-store"})
        if name in BRAND:  # the logo's files: they change only with a release
            return Response((STATIC / name).read_bytes(), media_type=BRAND[name], headers={"Cache-Control": "public, max-age=86400"})
        return JSONResponse({"error": "not found"}, 404)

    @app.get("/favicon.ico")
    def favicon():
        return Response((STATIC / "favicon-32.png").read_bytes(), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/health")
    @app.get("/healthz")  # Cloud Run's front end answers /healthz itself: use /health there
    def healthz():
        lib = library.get()
        return {"ok": True, "songs": len(lib.songs)}

    # ---- the library
    @app.get("/api/songs")
    def songs():
        return [{"file": s["file"], "name": s["name"], "mood": s["mood"]} for s in library.get().songs]

    @app.get("/api/search")
    def search_songs(q: str = ""):
        from algo_rhythm import search
        songs = library.get().songs
        named = set(search.name_hits(q, songs))  # the rest are only similar in meaning: the page says so
        return [{"file": songs[i]["file"], "name": songs[i]["name"], "mood": songs[i]["mood"], "named": i in named}
                for i in search.find(q, songs)]

    @app.get("/media/{rel:path}")
    def media_file(rel: str):
        """A song, a vocal stem, an asset or a song's art: from the bucket (a signed URL) or from the library folder."""
        if not library.allowed_media(rel) and not re.fullmatch(r"art/[0-9a-f]{16}\.jpg", rel):
            return JSONResponse({"error": "no such file"}, 404)
        cache = {"Cache-Control": "public, max-age=86400"}
        url = config.settings().storage_url
        if url and url.startswith("gs://"):
            signed = signed_url(rel)
            if signed:
                return RedirectResponse(signed, 302, headers={"Cache-Control": "private, max-age=3000"})
        path = storage.local_path(rel, library.get().root)
        if not Path(path).exists():
            return JSONResponse({"error": "no such file"}, 404)
        kind = "image/jpeg" if rel.endswith(".jpg") else "audio/mpeg"
        return FileResponse(path, media_type=kind, headers=cache)

    @app.get("/art/{name}")
    def art_image(name: str):
        if not re.fullmatch(r"[0-9a-f]{16}\.jpg", name) or name not in library.get().art.values():
            return JSONResponse({"error": "no such image"}, 404)
        return media_file(f"art/{name}")

    # ---- host accounts: hosting needs one, joining does not
    def client_ip(request):
        # Through the domain the caller is Cloudflare's Worker: the visitor's address is the one it passes on
        if from_edge(request) and request.headers.get("x-client-ip"):
            return request.headers["x-client-ip"][:64]
        # Cloud Run appends the caller's address to X-Forwarded-For: the last entry is the one a client cannot forge
        fwd = request.headers.get("x-forwarded-for", "")
        return (fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "?"))[:64]

    async def signed_in(request):
        return await run_in_threadpool(accounts.user_for, request.cookies.get(accounts.COOKIE))

    @app.get("/api/auth/me")
    async def me(request: Request):
        return JSONResponse({"user": await signed_in(request)}, headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/register")
    async def register(request: Request):
        data = await body(request)
        try:
            name = await run_in_threadpool(accounts.register, data.get("username"), data.get("email"),
                                           data.get("password"), client_ip(request))
        except accounts.AuthError as e:
            return JSONResponse({"error": str(e)}, 400)
        except accounts.Limited as e:
            return JSONResponse({"error": str(e)}, 429)
        log.info("account %s signed up (waiting for approval)", name)
        return JSONResponse({"ok": True, "username": name, "active": False}, 201)

    @app.post("/api/auth/login")
    async def login(request: Request):
        data = await body(request)
        try:
            token, name = await run_in_threadpool(accounts.login, data.get("username"), data.get("password"),
                                                  client_ip(request))
        except accounts.AuthError as e:
            return JSONResponse({"error": str(e)}, 401)
        except accounts.Limited as e:
            return JSONResponse({"error": str(e)}, 429)
        resp = JSONResponse({"user": {"username": name}})
        https = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
        resp.set_cookie(accounts.COOKIE, token, max_age=accounts.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=https, path="/")
        return resp

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        await run_in_threadpool(accounts.logout, request.cookies.get(accounts.COOKIE))
        resp = JSONResponse({"user": None})
        resp.delete_cookie(accounts.COOKIE, path="/")
        return resp

    # ---- parties
    @app.post("/api/parties")
    async def create_party(request: Request):
        data = await body(request)
        user = await signed_in(request)
        if not user:
            return JSONResponse({"error": "Sign in to host a party.", "login": True}, 401)
        where = await run_in_threadpool(places.lookup, client_ip(request))  # city and country only; the IP is not kept
        code, key = await run_in_threadpool(party.create, data.get("level", "medium"), user["id"], where)
        log.info("party %s created by %s", code, user["username"])
        return {"code": code, "host_key": key, "host": f"/p/{code}/host", "guest": f"/p/{code}", "screen": f"/p/{code}/screen"}

    @app.get("/api/p/{code}/state")
    def state(code: str, request: Request):
        check(code)
        host = party.is_host(code, request.headers.get("x-host-key"))
        return JSONResponse(party.view(code, host=host), headers={"Cache-Control": "no-store"})

    @app.post("/api/p/{code}/sync")
    async def sync(code: str, request: Request):
        """The host's player says where the music is (the WebSocket does the same without a request)."""
        await host_of(request, code)
        data = await body(request)
        await run_in_threadpool(party.sync, code, data.get("playhead"), data.get("status"))
        return JSONResponse(await run_in_threadpool(party.view, code, True), headers={"Cache-Control": "no-store"})

    @app.post("/api/p/{code}/advance")
    async def advance(code: str, request: Request):
        """Make the next join ready (seconds of work). swap: true = put a fitting request in place of the next song."""
        await host_of(request, code)
        data = await body(request)
        done = await run_in_threadpool(party.advance, code, data.get("swap") is True)
        hub.publish(code)
        view = await run_in_threadpool(party.view, code, True)
        return JSONResponse({**view, "advance": done}, headers={"Cache-Control": "no-store"})

    @app.post("/api/p/{code}/transport")
    async def transport(code: str, request: Request):
        await host_of(request, code)
        data = await body(request)
        await run_in_threadpool(party.transport, code, data["action"])
        hub.publish(code)
        return JSONResponse(await run_in_threadpool(party.view, code, True), headers={"Cache-Control": "no-store"})

    @app.post("/api/p/{code}/vibe")
    async def vibe(code: str, request: Request):
        await host_of(request, code)
        data = await body(request)
        if "vibe" in data:
            return await answer(code, lambda: party.act(code, lambda s, st: s.set_vibe(data["vibe"])), host=True)
        return await answer(code, lambda: party.act(code, lambda s, st: s.set_level(data["level"])), host=True)

    @app.post("/api/p/{code}/mood")
    async def mood(code: str, request: Request):
        await host_of(request, code)
        data = await body(request)
        return await answer(code, lambda: party.act(code, lambda s, st: s.set_mood(data["mood"])), host=True)

    @app.post("/api/p/{code}/visuals")
    async def visuals(code: str, request: Request):
        await host_of(request, code)
        data = await body(request)
        return await answer(code, lambda: party.set_visuals(code, data.get("world"), data.get("kaleido")), host=True)

    @app.post("/api/p/{code}/location")
    async def location(code: str, request: Request):
        await host_of(request, code)
        data = await body(request)
        return await answer(code, lambda: party.set_location(code, data.get("text", "")), host=True)

    @app.post("/api/p/{code}/trivia")
    async def trivia_switch(code: str, request: Request):
        await host_of(request, code)
        data = await body(request)
        return await answer(code, lambda: party.set_trivia(code, data.get("on")), host=True)

    @app.post("/api/p/{code}/answer")
    async def trivia_answer(code: str, request: Request):
        check(code)
        data = await body(request)
        # no push to every screen per answer (a burst of them redrew every phone mid-tap): "N answered" comes with the
        # screens' regular refresh, once a second
        return await run_in_threadpool(party.trivia_answer, code, data.get("guest"), data.get("name"), data.get("round"),
                                       data.get("choice"))

    @app.get("/api/p/{code}/coins")
    async def trivia_coins(code: str, guest: str = ""):
        check(code)
        return JSONResponse(await run_in_threadpool(party.trivia_me, code, guest), headers={"Cache-Control": "no-store"})

    @app.post("/api/p/{code}/screen")
    async def screen_options(code: str, request: Request):
        await host_of(request, code)
        data = await body(request)
        return await answer(code, lambda: party.set_screen(code, data.get("qr"), data.get("board")), host=True)

    @app.post("/api/p/{code}/request")
    async def request_song(code: str, request: Request):
        check(code)
        data = await body(request)
        host = await is_host(request, code)
        return await answer(code, lambda: party.request(code, data["file"], data.get("name")), host=host)

    @app.post("/api/p/{code}/feedback")
    async def feedback(code: str, request: Request):
        check(code)
        data = await body(request)
        host = await is_host(request, code)
        return await answer(code, lambda: party.vote(code, data.get("guest"), data.get("target"), data.get("value"), host), host=host)

    @app.post("/api/p/{code}/react")
    async def react(code: str, request: Request):
        check(code)
        data = await body(request)
        return await answer(code, lambda: party.react(code, data.get("guest"), data.get("emoji")))

    @app.post("/api/p/{code}/talk")
    async def talk(code: str, request: Request):
        check(code)
        data = await body(request)
        host = await is_host(request, code)
        said = await run_in_threadpool(party.talk, code, data.get("text"), not host, data.get("name"))
        hub.publish(code)
        return {**await run_in_threadpool(party.view, code, host), "talk": said}

    @app.websocket("/ws/p/{code}")
    async def ws(websocket: WebSocket, code: str):
        if not CODE.fullmatch(code):
            await websocket.close(code=4404)
            return
        key = websocket.query_params.get("key")
        host = bool(key) and await run_in_threadpool(party.is_host, code, key)
        guest = websocket.query_params.get("g") if websocket.query_params.get("role") == "guest" else None
        await hub.serve(websocket, code, host, guest if guest and party.live_guest_id(guest) else None)

    return app


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cfg = config.settings()
    ap.add_argument("library", type=Path, nargs="?", default=cfg.library_dir,
                    help="local library folder (default LIBRARY_DIR); imported into the database if that is empty")
    ap.add_argument("--host", default=cfg.host)
    ap.add_argument("--port", type=int, default=cfg.port)
    ap.add_argument("--log-level", default=cfg.log_level)
    args = ap.parse_args(argv)
    setup_logging(args.log_level)
    if not cfg.database_url:
        sys.exit("set DATABASE_URL (Cloud SQL, or sqlite:///path.db on a laptop)")
    if args.library and not cfg.storage_url:
        _import_if_empty(Path(args.library).resolve())
    library.get()  # load the library before the first request
    log.info("ready on http://%s:%s", args.host, args.port)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_config=None, ws_ping_interval=20, ws_ping_timeout=20)
    return 0


def _import_if_empty(lib_dir):
    """A laptop with a tagged library folder and an empty database: import it once (algo_rhythm.importer)."""
    from sqlalchemy import func, select
    from algo_rhythm import db, importer
    with repository.session() as s:
        n = s.scalar(select(func.count()).select_from(db.Song))
    if not n and (lib_dir / "tags").is_dir():
        log.info("empty database: importing %s", lib_dir)
        importer.main([str(lib_dir)])


def asgi():
    """For `uvicorn --factory algo_rhythm.server:asgi`."""
    setup_logging(config.settings().log_level)
    return create_app()


if __name__ == "__main__":
    sys.exit(main())
