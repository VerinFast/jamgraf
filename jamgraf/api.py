"""FastAPI app: HTML wiki views + JSON REST API + image serving + AI SSE stream
+ real-time collaborative editing (CRDT) at /collab."""
from __future__ import annotations

import anyio
import asyncio
import difflib
import functools
import json
import os
import signal
import sys
import tempfile
from urllib.parse import quote, urlencode
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (FastAPI, File, Form, HTTPException, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import (accesslog, ai, appconfig, auth, authoring, backups, bonjour,
               bugreports, calendarfeed, capabilities, chat, collab, doorman,
               config, db, debuglog, deeplink, edits, elements, embeddings,
               help_content, imagegen, kahala, kahalaauth, pdfgen, rag, render,
               secretstore, store, structure, tunnel, updater, wikis)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # ensures the wiki registry + default wiki schema
    accesslog.setup()  # capture ERROR-level logs into the access log
    wikis.ensure_help_wiki()  # so the Help wiki shows in the switcher immediately
    # Advertise on the LAN via Bonjour, but only while sharing is actually on —
    # an unreachable wiki isn't worth announcing.
    if auth.share_lan_enabled():
        bonjour.start(config.PORT)

    async def _seed_help():
        # Populate the Help wiki (prose + editable AI system prompts) off-thread;
        # idempotent, so it just no-ops on subsequent starts.
        import anyio
        try:
            await anyio.to_thread.run_sync(help_content.seed)
        except Exception as exc:
            print(f"[jamgraf] help seed skipped: {exc}")

    async def _migrate_html_default():
        # One-time: raw HTML is now on by default (all content is local/trusted).
        # Flip existing wikis that still have the old off value, and re-render.
        import anyio
        if appconfig.get("html_default_on_v1"):
            return

        def do():
            for w in wikis.list_wikis():
                db.current_wiki.set(w["slug"])
                try:
                    if store.get_setting("allow_html", "1") != "1":
                        store.set_setting("allow_html", "1")
                        store.rerender_all()
                except Exception as exc:
                    print(f"[jamgraf] html-default migration for {w['slug']}: {exc}")
            appconfig.set("html_default_on_v1", True)

        try:
            await anyio.to_thread.run_sync(do)
        except Exception as exc:
            print(f"[jamgraf] html-default migration skipped: {exc}")

    # Pre-load the embedding model once (under the default wiki context), off the
    # event loop, so the first search and a concurrent reindex don't race to load.
    async def _warm():
        try:
            import anyio
            db.current_wiki.set(wikis.default_slug())
            await anyio.to_thread.run_sync(lambda: embeddings.get_embedder().embed(["warmup"]))
        except Exception as exc:
            print(f"[jamgraf] embedder warmup skipped: {exc}")

    async def _retention():
        # Enforce the trash + access-log retention policies, hourly.
        import anyio
        first_pass = True
        while True:
            for w in wikis.list_wikis():
                db.current_wiki.set(w["slug"])
                try:
                    await anyio.to_thread.run_sync(store.sweep_trash)
                except Exception as exc:
                    print(f"[jamgraf] trash sweep failed for {w['slug']}: {exc}")
                try:
                    await anyio.to_thread.run_sync(store.sweep_activity)
                except Exception as exc:
                    print(f"[jamgraf] activity sweep failed for {w['slug']}: {exc}")
            try:
                await anyio.to_thread.run_sync(accesslog.sweep)
            except Exception as exc:
                print(f"[jamgraf] access-log sweep failed: {exc}")
            try:
                res = await anyio.to_thread.run_sync(backups.maybe_run)
                if res and res.get("ok"):
                    skipped = res.get("skipped") or []
                    accesslog.record(
                        "BACKUP ok", status=200,
                        detail=f"{res['name']} ({len(res['wikis'])} wikis"
                               + (f", skipped unreadable: {', '.join(skipped)}"
                                  if skipped else "") + ")")
            except Exception as exc:
                print(f"[jamgraf] scheduled backup failed: {exc}")
            try:
                # Check only — never install unattended. Swapping the bundle
                # restarts the app, and doing that under someone mid-edit is
                # not a decision to make on their behalf.
                #
                # Skipped on the first pass: this is a network call, and firing
                # it in the first seconds of every launch buys nothing (the
                # interval is measured in hours, and Settings has "Check now").
                # It also kept the loop reaching out during process startup,
                # including under the test suite.
                res = None if first_pass else await anyio.to_thread.run_sync(
                    updater.maybe_check)
                if res and res.get("available"):
                    # Label says UPGRADE because the chokepoint guard greps this
                    # module for SQL verbs — a log string shouldn't blunt it.
                    accesslog.record("UPGRADE available", status=200,
                                     detail=f"{res.get('latest')} (running {res['current']})")
            except Exception as exc:
                print(f"[jamgraf] update check failed: {exc}")
            first_pass = False
            await asyncio.sleep(3600)

    async with collab.server:                       # start the CRDT websocket server
        warm_task = asyncio.create_task(_warm())
        seed_task = asyncio.create_task(_seed_help())
        html_task = asyncio.create_task(_migrate_html_default())
        flush_task = asyncio.create_task(collab.flusher())
        retention_task = asyncio.create_task(_retention())
        try:
            yield
        finally:
            bonjour.stop()
            tunnel.stop()
            # Persist any edit the debounce has not written yet, BEFORE the
            # flusher is cancelled — cancelling only stops its loop, it does not
            # flush, so the tail of whatever someone was editing was being
            # dropped on quit. Matters most on the updater's path, which quits
            # the app on purpose. See issue #19.
            try:
                await collab.flush_all()
            except Exception as exc:
                print(f"[jamgraf] final collab flush failed: {exc}", file=sys.stderr)
            warm_task.cancel()
            seed_task.cancel()
            html_task.cancel()
            flush_task.cancel()
            retention_task.cancel()


app = FastAPI(title="Jamgraf", version=config.VERSION, lifespan=lifespan)


def _resolve_wiki(scope) -> str:
    """Pick the active wiki for a request: /collab/{wiki}/... path segment, then
    the X-Jamgraf-Wiki header (MCP), then the jamgraf_wiki cookie (browser)."""
    path = scope.get("path", "") or ""
    if path.startswith("/collab/"):
        parts = path.split("/")
        if len(parts) >= 3 and parts[2] and wikis.exists(parts[2]):
            return parts[2]
    from urllib.parse import parse_qs
    q = parse_qs(scope.get("query_string", b"").decode())
    if q.get("wiki") and wikis.exists(q["wiki"][0]):
        return q["wiki"][0]
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    # Both spellings of the header. An MCP server started before the rename keeps
    # sending X-Waikiki-Wiki until it restarts, and ignoring it would send that
    # agent's edits to whatever wiki the cookie or the default happens to name.
    cand = headers.get("x-jamgraf-wiki") or headers.get("x-waikiki-wiki")
    if cand and wikis.exists(cand):
        return cand
    cand = config.cookie(_parse_cookies(headers.get("cookie", "")), "wiki")
    if cand and wikis.exists(cand):
        return cand
    return wikis.default_slug() or "main"


def _parse_cookies(header: str) -> dict[str, str]:
    """A Cookie header as a dict (first occurrence of a name wins, as browsers send)."""
    out: dict[str, str] = {}
    for part in header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out.setdefault(k, v)
    return out


def _explicit_query_wiki(scope) -> str | None:
    """The wiki named by an explicit ?wiki= on this request, if valid."""
    from urllib.parse import parse_qs
    q = parse_qs(scope.get("query_string", b"").decode())
    cand = (q.get("wiki") or [None])[0]
    return cand if cand and wikis.exists(cand) else None


class WikiContextMiddleware:
    """Pure-ASGI middleware: bind db.current_wiki for the whole request in one
    task, so the contextvar reaches sync endpoints (a BaseHTTPMiddleware would
    run downstream in a separate task and lose it).

    It also keeps the browser's cookie in step with what it is actually being
    shown. Pages are resolved by ?wiki= first but cookie second, while the forms
    on those pages POST to bare paths — so viewing a page with ?wiki=X while the
    cookie said Y meant every write on it silently landed in Y. Rather than
    thread the wiki through ~20 form actions (and every future one), a GET that
    names a wiki explicitly re-points the cookie at it, so the implicit state can
    never disagree with the visible page."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        resolved = _resolve_wiki(scope)
        send_wrapped = send
        if (scope["type"] == "http" and scope.get("method") == "GET"
                and _explicit_query_wiki(scope) == resolved):
            async def send_wrapped(message):        # noqa: F811 - deliberate rebind
                if message["type"] == "http.response.start":
                    cookie = (f"jamgraf_wiki={resolved}; Path=/; Max-Age=31536000; "
                              f"SameSite=Lax")
                    message = dict(message)
                    message["headers"] = list(message.get("headers", [])) + [
                        (b"set-cookie", cookie.encode())]
                await send(message)

        token = db.current_wiki.set(resolved)
        try:
            await self.app(scope, receive, send_wrapped)
        finally:
            db.current_wiki.reset(token)


class DoormanFrameMiddleware:
    """Notice when Doorman is the one displaying this page.

    Doorman embeds Jamgraf in a plain iframe, and nothing in that request says
    so — which is why Settings could offer "use Doorman when it is running" as a
    checkbox while running *inside* Doorman. The browser does say it though:
    `Sec-Fetch-Dest: iframe` on the document navigation. The decision has to be
    server-side (Settings renders server-side, and `doorman.enabled()` gates the
    integration), so a client-side `window.self !== window.top` check can't be
    the authority.

    Only the framed *document* request carries that header — the page's own
    fetch() calls do not — so the judgement is recorded in `doorman` rather than
    re-derived per request. The probe it needs is a blocking HTTP call, so it
    runs off the event loop, and only when there is framing evidence at all."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("path", "").startswith("/static"):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            query = scope.get("query_string", b"").decode()
            if doorman.looks_framed(headers, query):
                import anyio
                await anyio.to_thread.run_sync(doorman.note_request, headers, query)
        return await self.app(scope, receive, send)


class AccessLogMiddleware:
    """Pure-ASGI: log every HTTP request (method, path, status, size, duration)
    and any unhandled error to the access log. Registered last so it's the
    outermost user middleware and sees the final status."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        import time as _t
        if scope["type"] != "http" or scope.get("path", "").startswith("/static"):
            return await self.app(scope, receive, send)
        start = _t.monotonic()
        info = {"status": 0, "size": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                info["status"] = message["status"]
            elif message["type"] == "http.response.body":
                info["size"] += len(message.get("body", b"") or b"")
            await send(message)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        client = (scope.get("client") or ["-"])[0]
        method = scope.get("method", "-")
        path = scope.get("path", "-")
        if scope.get("query_string"):
            path += "?" + scope["query_string"].decode()
        proto = "HTTP/" + scope.get("http_version", "1.1")
        ua, ref = headers.get("user-agent", "-"), headers.get("referer", "-")
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            dur = _t.monotonic() - start
            accesslog.error(f"{method} {path}", exc, dur)
            accesslog.http(client, method, path, proto, 500, info["size"], dur, ua, ref)
            raise
        dur = _t.monotonic() - start
        accesslog.http(client, method, path, proto, info["status"], info["size"],
                       dur, ua, ref)


class ShareAuthMiddleware:
    """Password-gate non-loopback requests when LAN sharing is on.

    Loopback callers are the owner (full access). Network callers must present a
    valid session cookie and are restricted to page reading/editing — see
    auth.guest_may(), which blocks anything that runs a local command (chat,
    image generation) or reconfigures the host (settings, wikis, logs)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "") or "/"
        if path.startswith("/static") or path in ("/login", "/logout"):
            return await self.app(scope, receive, send)

        client = (scope.get("client") or ("", 0))[0]
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

        # Cross-site POSTs to the Kahala routes are refused even for the owner.
        # Everywhere else the app has no CSRF defence and the worst a forged
        # POST does is local damage; here a forged link+push would send the whole
        # wiki to a host the attacker names. Checked in the middleware rather
        # than per-route so a route added later cannot forget it.
        if (path.startswith("/kahala") and scope.get("method") == "POST"
                and not auth.same_origin(headers)):
            return await self._deny(
                scope, receive, send, 403,
                "That request didn't come from Jamgraf's own pages, and the "
                "Kahala controls can send a wiki off this machine, so it was "
                "refused.")

        # Headers matter here: a tunnel reaches us over loopback, so address alone
        # would hand internet traffic full owner rights.
        if auth.is_local(client, headers):
            scope["jamgraf_role"] = "owner"
            return await self.app(scope, receive, send)

        # Remote caller from here on.
        if not auth.enabled():
            return await self._deny(scope, receive, send, 403,
                                    "Sharing is off on this Jamgraf.")
        cookies = _parse_cookies(headers.get("cookie", ""))
        token = cookies.get(auth.COOKIE) or cookies.get(auth.LEGACY_COOKIE) or ""
        if not auth.check_token(token):
            if scope["type"] == "websocket":
                return await self._close_ws(send)
            nxt = quote(path, safe="/")
            return await self._redirect(scope, receive, send, f"/login?next={nxt}")
        if not auth.guest_may(path):
            if scope["type"] == "websocket":
                return await self._close_ws(send)
            return await self._deny(
                scope, receive, send, 403,
                "That area is limited to the owner of this Jamgraf (it can run "
                "commands on their computer).")
        scope["jamgraf_role"] = "guest"
        return await self.app(scope, receive, send)

    async def _deny(self, scope, receive, send, code, msg):
        await Response(msg, status_code=code, media_type="text/plain")(
            scope, receive, send)

    async def _redirect(self, scope, receive, send, url):
        await RedirectResponse(url, status_code=303)(scope, receive, send)

    async def _close_ws(self, send):
        await send({"type": "websocket.close", "code": 4403})


app.add_middleware(WikiContextMiddleware)
app.add_middleware(DoormanFrameMiddleware)
app.add_middleware(ShareAuthMiddleware)
app.add_middleware(AccessLogMiddleware)  # added last → outermost

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Timestamps are rendered relative ("3 minutes ago"), because that is what makes
# the history affordance on an article read as news rather than as a field.
templates.env.filters["ago"] = render.time_ago


@app.exception_handler(db.WikiUnreadable)
def wiki_unreadable(request: Request, exc: db.WikiUnreadable):
    """Say which wiki can't be opened, and how to get its content back.

    A damaged wiki file used to surface as a bare "Internal Server Error", which
    tells someone whose family wiki won't open precisely nothing. The app is
    packaged and launched from the Finder, so the traceback on stderr is
    invisible: whatever we know has to be said in the browser (issue #71).

    Anything else — a `KeyError` in our own code — never reaches here; `db`
    raises this type only for SQLite refusing a file.
    """
    slug = exc.wiki or db.active_wiki()
    path = exc.path or str(wikis.db_path(slug))
    if request.url.path.startswith("/api/") or request.url.path.startswith("/mcp"):
        return JSONResponse(status_code=503, content={
            "error": "wiki_unreadable", "wiki": slug, "reason": exc.reason,
            "path": path, "detail": str(exc)})
    try:
        others = [dict(w, **{"ok": wikis.health(w["slug"])["ok"]})
                  for w in wikis.list_wikis() if w["slug"] != slug]
        return templates.TemplateResponse(request, "unreadable.html", {
            "wiki": slug,
            "wiki_name": wikis.name_of(slug),
            "reason": exc.reason,
            "path": path,
            "others": others,
            "backups": backups.restore_hint(),
            "detail": str(exc),
        }, status_code=503)
    except Exception as inner:  # pragma: no cover - the page of last resort
        return HTMLResponse(status_code=503, content=(
            f"<h1>{slug} can’t be read</h1><p>{exc.reason}</p>"
            f"<p><code>{path}</code></p>"
            f"<p><a href=\"/wikis\">Manage wikis</a> to switch wiki or open a "
            f"backup. The file has been left untouched.</p>"
            f"<!-- {inner} -->"))


class _StarletteChannel:
    """Adapts a Starlette WebSocket to the pycrdt-websocket Channel protocol.

    The room name is `path` — we pass the bare slug so it matches the room that
    ensure_room()/append/replace operate on (a plain Mount would rewrite the
    path to '/<slug>' and desync into a different, empty room)."""

    def __init__(self, websocket: WebSocket, path: str):
        self._ws = websocket
        self.path = path

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._ws.receive_bytes()
        except WebSocketDisconnect:
            raise StopAsyncIteration

    async def send(self, message: bytes) -> None:
        await self._ws.send_bytes(message)

    async def recv(self) -> bytes:
        return await self._ws.receive_bytes()


@app.websocket("/collab/{wiki}/{slug}")
async def collab_ws(websocket: WebSocket, wiki: str, slug: str):
    if not wikis.exists(wiki):
        await websocket.close(code=4004)
        return
    await websocket.accept()
    db.current_wiki.set(wiki)
    try:
        await collab.ensure_room(wiki, slug)  # seed from the wiki's DB first
    except db.WikiUnreadable:
        # Nothing to co-edit if the file can't be opened. Closing is the honest
        # answer; the HTML view of the same page explains why (see above).
        await websocket.close(code=4004)
        return
    channel = _StarletteChannel(websocket, collab.room_key(wiki, slug))
    try:
        await collab.server.serve(channel)
    except WebSocketDisconnect:
        pass


def _nav_tree(pages: list[dict]) -> list[dict]:
    """Nest a flat `list_pages(include_children=True)` result under a
    `children` key, keyed by `parent_slug`.

    Order within each sibling group follows `pages`' own order, so whatever
    the SQL-level sort (recent/title/custom) produced is preserved per level
    rather than re-sorted here. A page whose parent isn't in `pages` — cut by
    the [:500] cap below, say — is promoted to top-level so it's shown rather
    than silently dropped."""
    by_slug = {p["slug"]: p for p in pages}
    roots: list[dict] = []
    for p in pages:
        p["children"] = []
    for p in pages:
        parent = by_slug.get(p.get("parent_slug"))
        (parent["children"] if parent else roots).append(p)
    return roots


def _ctx(request: Request, **extra) -> dict:
    """Common template context: active wiki, theme, nav pages, pygments styles.

    The active wiki's own reads are allowed to fail here: if its file is
    unreadable, the chrome renders without a page list and carries a banner
    saying so, rather than taking down pages that don't need that wiki's
    contents — *Manage wikis* above all, which is where recovery starts."""
    wiki = db.active_wiki()
    nav_filter = config.cookie(request.cookies, "nav_filter", "all")
    nav_sort = config.cookie(request.cookies, "nav_sort", "updated")
    unreadable = wikis.health(wiki)
    if unreadable["ok"]:
        theme = store.get_setting("theme", "default")
        starred_only = nav_filter == "starred"
        # Starred stays flat: nesting a starred child would need its unstarred
        # ancestors pulled in too just to place it in the tree, which isn't
        # worth the complexity for a filtered view.
        flat_pages = store.list_pages(
            sort=nav_sort, starred_only=starred_only,
            include_children=not starred_only)[:500]
        nav_pages = flat_pages if starred_only else _nav_tree(flat_pages)
    else:
        theme, nav_pages = "default", []
    base = {
        "request": request,
        "theme": theme,
        "nav_pages": nav_pages,
        # None when the wiki is fine; a plain reason when it can't be opened.
        "wiki_unreadable": None if unreadable["ok"] else unreadable["reason"],
        "wiki_unreadable_path": unreadable.get("path"),
        "nav_filter": nav_filter,
        "nav_sort": nav_sort,
        "current_path": request.url.path,
        "pygments_css": render.pygments_css(),
        "vec_available": db.VEC_AVAILABLE,
        # Only ever non-zero when an agent filed something; the nav entry is
        # hidden otherwise, so a queue nobody is using costs no attention.
        "pending_reports": bugreports.count(),
        "current_wiki": wiki,
        "current_wiki_name": wikis.name_of(wiki),
        "wikis": wikis.list_wikis(),
        "version": config.VERSION,
        "is_desktop": config.is_desktop(),
        "native_dictation": config.native_dictation(),
        # "owner" (loopback) or "guest" (LAN, password) — templates hide the
        # owner-only controls from guests so the UI matches what they can do.
        "role": request.scope.get("jamgraf_role", "owner"),
        # {capability id: ok|degraded|unavailable}. Every template that offers an
        # AI affordance gates it on this, so a feature that cannot work renders
        # disabled and points at Settings → Capabilities instead of failing on
        # click. Probed below the routes (jamgraf/capabilities.py) and cached.
        "cap_state": capabilities.states(),
    }
    base.update(extra)
    # A jamgraf:// link for whichever page this view is showing, so "Copy link"
    # hands out something that survives the app picking a different port.
    page = base.get("page") or {}
    if page.get("slug"):
        base.setdefault("page_link", deeplink.for_page(wiki, page["slug"]))
        # Candidate parents for the Page options menu. Top-level pages only,
        # matching what the old Page settings block offered, and never the page
        # itself.
        base.setdefault("parent_options",
                        [p for p in store.list_pages() if p["slug"] != page["slug"]])
    return base


# =============================================================================
# HTML views (human-facing)
# =============================================================================

@app.get("/login", response_class=HTMLResponse)
def login_view(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "next": next, "error": error,
        "theme": store.get_setting("theme", "default"), "version": config.VERSION})


@app.post("/login")
def login_submit(request: Request, password: str = Form(""), next: str = Form("/")):
    client = (request.client.host if request.client else "")
    hdrs = {k.lower(): v for k, v in request.headers.items()}
    if not auth.is_local(client, hdrs) and not auth.enabled():
        raise HTTPException(403, "Sharing is off")
    if not auth.verify_password(password):
        accesslog.record("LOGIN failed", status=401, host=client or "-")
        return RedirectResponse(f"/login?next={quote(next, safe='/')}&error=1",
                                status_code=303)
    accesslog.record("LOGIN ok", status=200, host=client or "-")
    resp = RedirectResponse(next or "/", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_token(), httponly=True, samesite="lax",
                    max_age=auth.SESSION_DAYS * 86400)
    return resp


@app.post("/logout")
def logout_view():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    resp.delete_cookie(auth.LEGACY_COOKIE)   # or a pre-rename cookie keeps them in
    return resp


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request,
        "index.html", _ctx(request, pages=store.list_pages())
    )


def _claude_config(request: Request) -> dict:
    """The MCP config for THIS install, with real paths filled in — so the
    Connect page is copy-paste ready. Shared with chat, which hands the same
    definition to the CLI so the agent can read the wiki (see config)."""
    return config.mcp_server_config(f"{request.url.scheme}://{request.url.netloc}")


def _help_cookie(resp):
    resp.set_cookie("jamgraf_wiki", config.HELP_WIKI, max_age=60 * 60 * 24 * 365,
                    samesite="lax")
    return resp


@app.get("/help")
def help_home():
    """Open the Help wiki (switches the browser's active wiki to Help)."""
    return _help_cookie(RedirectResponse("/wiki/getting-started", status_code=303))


@app.get("/help/{slug}")
def help_slug(slug: str):
    return _help_cookie(RedirectResponse(f"/wiki/{slug}", status_code=303))


@app.get("/debug", response_class=HTMLResponse)
def debug_view(request: Request):
    """Show recent local-CLI invocations (claude/agy/gemini) with full output."""
    return templates.TemplateResponse(request, "debug.html",
        _ctx(request, entries=debuglog.tail(100)))


@app.post("/debug/clear")
def debug_clear():
    debuglog.clear()
    return RedirectResponse("/debug", status_code=303)


@app.get("/logs", response_class=HTMLResponse)
def logs_view(request: Request, q: str = "", kind: str = "", limit: int = 500):
    """Human-readable access log: requests received + calls made + errors."""
    entries = accesslog.read(limit=min(max(limit, 1), 2000), query=q, kind=kind)
    return templates.TemplateResponse(request, "logs.html", _ctx(
        request, entries=entries, q=q, kind=kind,
        retention_hours=accesslog.retention_hours()))


@app.post("/logs/clear")
def logs_clear():
    accesslog.clear()
    return RedirectResponse("/logs", status_code=303)


@app.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request):
    return templates.TemplateResponse(request, "help.html", _ctx(
        request,
        claude_config=json.dumps(_claude_config(request), indent=2),
        config_path=str(config.claude_desktop_config_path()),
        web_url=f"{request.url.scheme}://{request.url.netloc}",
    ))


@app.get("/wikis", response_class=HTMLResponse)
def wikis_manage(request: Request, error: str = "", ok: str = ""):
    # stats() reports a wiki it cannot open rather than raising — this page is
    # where someone comes to escape a broken wiki or open a backup, so it has to
    # render even when one of the files is damaged (issue #71).
    stats = {w["slug"]: wikis.stats(w["slug"]) for w in wikis.list_wikis()}
    return templates.TemplateResponse(request, "wikis.html", _ctx(
        request, error=error, ok=ok, stats=stats,
        help_wiki=config.HELP_WIKI, restore=backups.restore_hint(),
        # Only what needs a person: a clean move says nothing.
        migration=(config.DATA_MIGRATION if config.DATA_MIGRATION and
                   config.DATA_MIGRATION["outcome"] != "moved" else None)))


@app.post("/switch-wiki")
def switch_wiki(wiki: str = Form(...)):
    resp = RedirectResponse("/", status_code=303)
    if wikis.exists(wiki):
        resp.set_cookie("jamgraf_wiki", wiki, max_age=60 * 60 * 24 * 365,
                        samesite="lax")
    return resp


@app.post("/wikis/create")
def wikis_create(name: str = Form(...)):
    if not name.strip():
        return RedirectResponse("/wikis?error=Name+is+required", status_code=303)
    slug = wikis.create_wiki(name)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("jamgraf_wiki", slug, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.post("/wikis/{slug}/rename")
def wikis_rename(slug: str, name: str = Form(...)):
    """Change a wiki's display name. Its address is untouched."""
    try:
        wikis.rename(slug, name)
    except ValueError as exc:
        return RedirectResponse(f"/wikis?error={exc}", status_code=303)
    return RedirectResponse("/wikis", status_code=303)


@app.post("/wikis/{slug}/address")
async def wikis_change_slug(request: Request, slug: str,
                            new_slug: str = Form(...)):
    """Change a wiki's slug — its URLs and the name of its database file.

    Async because the CRDT rooms have to be released before the file moves:
    a room outlives the request and persists under the wiki its key names, so
    one left pointing at an address that no longer exists would write its page
    into the default wiki (`collab.release_wiki`).

    Every refusal is collected *first* (`wikis.plan_slug_change`). Releasing the
    rooms closes whatever anyone has open in that wiki, so doing it before
    finding out the address was a duplicate — or the Help wiki's — would throw
    people out of their editors to accomplish nothing at all.
    """
    try:
        await anyio.to_thread.run_sync(wikis.plan_slug_change, slug, new_slug)
        await collab.release_wiki(slug)
        target = await anyio.to_thread.run_sync(wikis.change_slug, slug, new_slug)
    except ValueError as exc:
        return RedirectResponse(f"/wikis?error={exc}", status_code=303)
    resp = RedirectResponse("/wikis?ok=" + quote(
        f"Address changed to /{target}. Reload any tab that had this wiki open."),
        status_code=303)
    # The cookie is the browser's pointer at the active wiki, and it still names
    # the old address — which no longer resolves, so the next bare POST would
    # land in the default wiki rather than this one.
    if config.cookie(request.cookies, "wiki") == slug:
        resp.set_cookie("jamgraf_wiki", target, max_age=60 * 60 * 24 * 365,
                        samesite="lax")
    return resp


@app.post("/wikis/{slug}/delete")
def wikis_delete(slug: str):
    """Forget a wiki and remove its files. A refusal comes back as a message.

    Removing the files means closing every handle on them, which is refused
    outright while a save is in flight — so this has a refusal to report now,
    the same way the address route does.
    """
    try:
        wikis.delete_wiki(slug)
    except ValueError as exc:
        return RedirectResponse(f"/wikis?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/wikis", status_code=303)


@app.get("/wikis/{slug}/export")
def wikis_export(slug: str):
    """Save a wiki to a downloadable .wiki file (browser fallback for Save)."""
    if not wikis.exists(slug):
        raise HTTPException(404, "No such wiki")
    dest = Path(tempfile.mkdtemp()) / f"{slug}.wiki"
    wikis.export_to(slug, str(dest))
    return FileResponse(str(dest), filename=f"{wikis.name_of(slug)}.wiki",
                        media_type="application/octet-stream")


@app.post("/wikis/import")
async def wikis_import(file: UploadFile):
    """Open a wiki from an uploaded file (browser fallback for Open)."""
    import anyio

    tmp = Path(tempfile.mkdtemp()) / (file.filename or "import.wiki")
    tmp.write_bytes(await file.read())
    name = Path(file.filename or "Imported").stem
    try:
        # Off the event loop: importing now rebuilds the retrieval index when the
        # file arrives without one, which re-embeds every page. The sync
        # re-embed routes (`/settings/models/*`) are plain `def` and get the
        # threadpool for free; this one is `async` for the upload read, so it has
        # to ask. Like those routes, the request simply waits — there is no job
        # runner here to report progress through.
        slug = await anyio.to_thread.run_sync(
            lambda: wikis.import_from(str(tmp), name=name))
    except ValueError as exc:
        return RedirectResponse(f"/wikis?error={exc}", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("jamgraf_wiki", slug, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


# --- Kahala (issue #58) ------------------------------------------------------
#
# Routes only, per rule 1: every one of these validates its form and calls
# `kahala`, which owns the link record, the transfer and the wording of every
# refusal. Nothing here decides anything.
#
# All of it is owner-only -- `auth._GUEST_DENY_PREFIX` carries "/kahala" -- so a
# LAN guest can neither sign this app in nor push its wikis anywhere.


def _kahala_back(wiki: str, ok: str = "", error: str = "") -> RedirectResponse:
    query = urlencode({k: v for k, v in
                       (("wiki", wiki), ("ok", ok), ("error", error)) if v})
    return RedirectResponse(f"/kahala?{query}", status_code=303)


@app.get("/kahala", response_class=HTMLResponse)
def kahala_pane(request: Request, wiki: str = "", ok: str = "", error: str = ""):
    slug = wiki if wikis.exists(wiki) else db.active_wiki()
    return templates.TemplateResponse(request, "kahala.html", _ctx(
        request, ok=ok, error=error, subject=slug,
        status=kahala.status(slug), suggest=kahala.suggested_link(slug),
        links={w["slug"]: wikis.get_link(w["slug"]) for w in wikis.list_wikis()}))


@app.get("/kahala/signin")
def kahala_signin(wiki: str = "", shell: str = ""):
    """Start a sign-in. Returns the authorize URL as JSON, or redirects to it.

    The desktop shell asks for `shell=desktop` and opens the URL in the SYSTEM
    browser rather than letting this window navigate. That is not a nicety:
    RFC 8252 §8.12 tells native apps not to run OAuth in an embedded
    user-agent, and WKWebView is one. Practically it also means the person gets
    their real browser's Keycloak session, their password manager, and whatever
    MFA their org uses — none of which exist inside our own window.

    Either way the code comes back to /kahala/callback on this same loopback
    port, which this process serves, so it does not matter which browser made
    the request.
    """
    desktop = shell == "desktop"
    try:
        url, _state = kahalaauth.begin(
            next_url=f"/kahala?wiki={quote(wiki)}", external=desktop)
    except ValueError as exc:
        if desktop:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return _kahala_back(wiki, error=str(exc))
    if desktop:
        return JSONResponse({"ok": True, "url": url})
    return RedirectResponse(url, status_code=303)


@app.get("/kahala/state")
def kahala_state(wiki: str = ""):
    """Whether we are signed in — polled by the pane while a sign-in is open.

    The desktop sign-in finishes in another application, so this window has no
    event to react to. Rather than leave a stale "you aren't signed in" on
    screen until someone reloads, the pane asks."""
    slug = wiki if wikis.exists(wiki) else db.active_wiki()
    return JSONResponse({"signed_in": kahala.status(slug)["signed_in"]})


@app.get("/kahala/callback", response_class=HTMLResponse)
def kahala_callback(request: Request, code: str = "", state: str = "",
                    error: str = "", error_description: str = ""):
    """Where Keycloak sends the code back — in whichever browser did the sign-in.

    When the flow began in the desktop shell, that browser is not the app's
    window, so redirecting into the app UI here would leave the person looking
    at Jamgraf in a stray browser tab. They get a short page saying it worked
    instead, and the app's own window notices by polling /kahala/state.
    """
    external = kahalaauth.is_external(state)
    if error:
        why = f"Kahala's sign-in returned: {error_description or error}"
        return (_kahala_done_page(request, False, why) if external
                else _kahala_back("", error=why))

    done = kahalaauth.complete(code, state)
    if external:
        return _kahala_done_page(request, done["ok"], done["error"])
    if not done["ok"]:
        return _kahala_back("", error=done["error"])
    return RedirectResponse(done["next"], status_code=303)


def _kahala_done_page(request: Request, ok: bool, why: str):
    # Through _ctx like every other page: this renders in a browser that is not
    # the app's window, and a bare context would drop the chrome base.html needs.
    return templates.TemplateResponse(
        request, "kahala_done.html",
        _ctx(request, signed_in=ok, why=why,
             store_name=secretstore.store_name()))


@app.post("/kahala/signout")
def kahala_signout(wiki: str = Form("")):
    kahalaauth.sign_out()
    return _kahala_back(wiki, ok="Signed out. Your wikis are untouched.")


@app.post("/kahala/link")
def kahala_link(wiki: str = Form(...), base_url: str = Form(...),
                remote: str = Form(...)):
    done = kahala.link(wiki, base_url, remote)
    return _kahala_back(wiki, ok="Linked." if done["ok"] else "",
                        error=done.get("error", ""))


@app.post("/kahala/unlink")
def kahala_unlink(wiki: str = Form(...)):
    kahala.unlink(wiki)
    return _kahala_back(wiki, ok="Link forgotten. Nothing was deleted.")


@app.post("/kahala/push")
def kahala_push(wiki: str = Form(...), full: str = Form("")):
    done = kahala.push(wiki, full=bool(full))
    return _kahala_back(wiki, error=done.get("error", ""),
                        ok=_kahala_done(done, "Pushed"))


@app.post("/kahala/pull")
def kahala_pull(wiki: str = Form(...), full: str = Form("")):
    done = kahala.pull(wiki, full=bool(full))
    return _kahala_back(wiki, error=done.get("error", ""),
                        ok=_kahala_done(done, "Pulled"))


@app.post("/kahala/clone")
def kahala_clone(base_url: str = Form(...), remote: str = Form(...),
                 name: str = Form("")):
    done = kahala.clone(base_url, remote, name)
    if not done["ok"]:
        return _kahala_back("", error=done["error"])
    resp = _kahala_back(done["wiki"], ok=_kahala_done(done, "Cloned"))
    resp.set_cookie("jamgraf_wiki", done["wiki"], max_age=60 * 60 * 24 * 365,
                    samesite="lax")
    return resp


def _kahala_done(done: dict, verb: str) -> str:
    """What happened, including WHICH transfer ran.

    The mode is part of the message rather than a detail: "only the changes" and
    "the whole wiki" behave differently when something goes wrong, and a fallback
    the user cannot see is a fallback they cannot question."""
    if not done.get("ok"):
        return ""
    detail = done.get("detail") or "nothing to move"
    mode = {"incremental": "changes only", "full": "whole wiki"}.get(done.get("mode"), "")
    head = f"{verb}{f' ({mode})' if mode else ''}"
    return f"{head}: {detail}. {done.get('note', '')}".strip()
# --- Agent bug reports (issue #89) -------------------------------------------
#
# Nothing here publishes. The queue is filled by the MCP `report_bug` tool and
# emptied by a person: "Open on GitHub" is an ordinary link to GitHub's own new
# issue form, pre-filled, so what gets published is submitted by the reader in
# GitHub's editor. See `bugreports` for why that indirection is the point.


@app.get("/reports", response_class=HTMLResponse)
def bug_reports(request: Request):
    return templates.TemplateResponse(request, "reports.html", _ctx(
        request, reports=bugreports.listing(), repo=bugreports.REPO))


@app.post("/reports/{report_id}/discard")
def bug_report_discard(report_id: str):
    bugreports.discard(report_id)
    return RedirectResponse("/reports", status_code=303)


@app.get("/new", response_class=HTMLResponse)
def new_page(request: Request, template: str = ""):
    markdown = ""
    if template:
        tpl = (store.template_get(int(template)) if template.isdigit()
               else store.template_by_name(template))
        if tpl:
            # Same materialisation the MCP `create_from_template` path uses, so a
            # page a human starts from a template is bound to it identically.
            markdown = store.apply_template(tpl)
    return templates.TemplateResponse(request,
        "edit.html",
        _ctx(request, page={"slug": "", "title": "", "markdown": markdown}, is_new=True),
    )


@app.get("/templates", response_class=HTMLResponse)
def templates_manage(request: Request):
    return templates.TemplateResponse(request,
        "templates.html", _ctx(request, templates_list=store.templates_list()))


@app.get("/templates/new", response_class=HTMLResponse)
def template_new_view(request: Request):
    return templates.TemplateResponse(request, "template_edit.html", _ctx(
        request, tpl={"id": "", "name": "", "markdown": "", "meta_schema": ""},
        is_new=True))


@app.get("/templates/{tid}/edit", response_class=HTMLResponse)
def template_edit_view(request: Request, tid: int):
    tpl = store.template_get(tid)
    if not tpl:
        raise HTTPException(404, "Template not found")
    return templates.TemplateResponse(request, "template_edit.html",
        _ctx(request, tpl=tpl, is_new=False))


@app.post("/templates/save")
def templates_save(name: str = Form(...), markdown: str = Form(""),
                   tid: str = Form(""), meta_schema: str = Form("")):
    """Save a template, including the metadata schema it declares (may be empty:
    the form always submits the field, so clearing the box clears the schema)."""
    if name.strip():
        store.template_save(name.strip(), markdown, int(tid) if tid else None,
                            meta_schema=meta_schema)
    return RedirectResponse("/templates", status_code=303)


@app.post("/templates/{tid}/delete")
def templates_delete(tid: int):
    store.template_delete(tid)
    return RedirectResponse("/templates", status_code=303)


# --- Custom elements ---------------------------------------------------------

class DraftIn(BaseModel):
    kind: str = "element"
    description: str = ""
    provider: str | None = None


@app.get("/api/voice/status")
def voice_status():
    """Whether nicer speech is available right now.

    The page asks once and falls back to the browser's own voice otherwise, so a
    machine without Doorman behaves exactly as it did before.
    """
    return doorman.status()


class SpeakIn(BaseModel):
    text: str = ""
    voice: str = ""


@app.post("/api/voice/speak")
async def voice_speak(body: SpeakIn):
    """Synthesise through Doorman when it is running.

    404 means "not available" rather than an error: the caller is expected to
    use speechSynthesis instead, and a reader should never see a failure for
    what is only a better-sounding option.
    """
    import anyio
    wav = await anyio.to_thread.run_sync(lambda: doorman.speak(body.text, body.voice))
    if not wav:
        raise HTTPException(404, "no external voice available")
    return Response(content=wav, media_type="audio/wav")


@app.post("/api/draft")
async def api_draft(body: DraftIn):
    """Draft a template or element from a description.

    Returns the parts for the editor to fill — it never saves. Elements ship
    arbitrary HTML and JS into every page that uses them, so a human reads the
    draft in the form and saves it deliberately (see jamgraf/authoring.py).
    """
    import anyio
    provider = body.provider or store.get_setting("chat_provider", "claude")
    model = store.get_setting("chat_model", "")
    return await anyio.to_thread.run_sync(
        lambda: authoring.draft(body.kind, body.description, provider, model))


@app.get("/elements", response_class=HTMLResponse)
def elements_manage(request: Request):
    return templates.TemplateResponse(request, "elements.html",
        _ctx(request, elements_list=elements.list_elements()))


@app.get("/elements/new", response_class=HTMLResponse)
def element_new_view(request: Request):
    blank = {"slug": "", "name": "", "fields": "[]", "html": "", "css": "", "js": ""}
    return templates.TemplateResponse(request, "element_edit.html",
        _ctx(request, el=blank, fields_text="", is_new=True))


@app.get("/elements/{slug}/edit", response_class=HTMLResponse)
def element_edit_view(request: Request, slug: str):
    el = elements.get_element(slug)
    if not el:
        raise HTTPException(404, "Element not found")
    return templates.TemplateResponse(request, "element_edit.html", _ctx(
        request, el=el, fields_text=elements.fields_to_text(el["fields"]), is_new=False))


@app.post("/elements/save")
def elements_save(request: Request, slug: str = Form(""), name: str = Form(...),
                  fields: str = Form(""), html: str = Form(""), css: str = Form(""),
                  js: str = Form("")):
    if not name.strip():
        return RedirectResponse("/elements", status_code=303)
    saved = elements.save_element(slug, name.strip(), fields, html, css, js)
    store.rerender_all()   # element changed → refresh pages that use it
    return RedirectResponse(f"/elements/{saved}/edit", status_code=303)


@app.post("/elements/{slug}/delete")
def elements_delete(slug: str):
    elements.delete_element(slug)
    store.rerender_all()
    return RedirectResponse("/elements", status_code=303)


class TableCell(BaseModel):
    table: int
    row: int
    col: int
    value: str


@app.get("/wiki/{slug}/section", response_class=HTMLResponse)
def section_edit_view(request: Request, slug: str, anchor: str = ""):
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    span = render.section_span_for_slug(page["markdown"], anchor)
    section_md = page["markdown"][span[0]:span[1]] if span else ""
    return templates.TemplateResponse(request, "section.html",
        _ctx(request, page=page, anchor=anchor, section_md=section_md,
             found=span is not None))


@app.post("/wiki/{slug}/section")
def section_save(slug: str, anchor: str = Form(...), markdown: str = Form(...)):
    page = store.get_page(slug)
    if page:
        span = render.section_span_for_slug(page["markdown"], anchor)
        if span:
            md = page["markdown"]
            tail = md[span[1]:]
            body = markdown.rstrip("\n") + "\n" + ("\n" if tail.strip() else "")
            store.update_page(slug, page["title"], md[:span[0]] + body + tail,
                              author="human")
    return RedirectResponse(f"/wiki/{slug}#{anchor}", status_code=303)


@app.post("/wiki/{slug}/table-cell")
def table_cell_edit(slug: str, body: TableCell):
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    try:
        new_md = edits.set_table_cell(page["markdown"], body.table, body.row,
                                      body.col, body.value)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    store.update_page(slug, page["title"], new_md, author="human")
    return {"ok": True}


class ChatIn(BaseModel):
    question: str
    provider: str | None = None
    model: str | None = None
    history: list[dict] = []


@app.post("/chat")
async def chat_wiki_endpoint(body: ChatIn):
    """Chat with no page in context — reachable from Settings, the Index, search.

    Deliberately does NOT pre-retrieve excerpts to compensate. The agent should
    look things up through Jamgraf's own MCP tools; until it can (issue #30) an
    answer here is only as good as the model's general knowledge, which is the
    honest state of it rather than a retrieval step pretending otherwise.
    """
    import anyio
    provider = body.provider or store.get_setting("chat_provider", "claude")
    model = body.model if body.model is not None else store.get_setting("chat_model", "")
    return await anyio.to_thread.run_sync(
        lambda: chat.answer(None, body.question, provider, model, body.history or []))


@app.post("/wiki/{slug}/chat")
async def chat_endpoint(slug: str, body: ChatIn):
    """Answer a question about a page via the configured CLI (claude/gemini),
    grounded in the page + wiki-search excerpts. Runs the subprocess off-thread."""
    import anyio
    provider = body.provider or store.get_setting("chat_provider", "claude")
    model = body.model if body.model is not None else store.get_setting("chat_model", "")
    return await anyio.to_thread.run_sync(
        lambda: chat.answer(slug, body.question, provider, model, body.history or []))


class ImageGenIn(BaseModel):
    description: str
    model: str | None = None


@app.post("/wiki/{slug}/generate-image")
async def generate_image_endpoint(slug: str, body: ImageGenIn):
    """Generate an image for a page: Claude writes the prompt, the image CLI
    (agy/gemini) renders it into the article's folder. Runs off-thread."""
    import anyio
    cli = store.get_setting("image_cli", "agy")
    model = body.model if body.model is not None else store.get_setting("image_model", "")
    return await anyio.to_thread.run_sync(
        lambda: imagegen.generate(slug, body.description, cli, model))


@app.get("/wiki/{slug}/pdf")
def page_pdf_view(slug: str):
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    pdf = pdfgen.page_pdf(page["title"], page["html"])
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{slug}.pdf"'})


@app.get("/wiki/{slug}", response_class=HTMLResponse)
def view_page(request: Request, slug: str):
    page = store.get_page(slug)
    if not page:
        # Offer to create it (wiki-style red link).
        return templates.TemplateResponse(request,
            "edit.html",
            _ctx(request, page={"slug": "", "title": slug.replace("-", " ").title(),
                                "markdown": ""}, is_new=True, missing=slug),
            status_code=404,
        )
    store.log_activity("human", "read")
    parent = store.parent_of(page)
    # The article view is intentionally bare — just the content. Page settings,
    # backlinks, history, suggestions, comments, and chat live on the Talk page.
    #
    # The one exception is *when this page last changed*, and how many earlier
    # versions are still on disk. Version history is the app's undo, and until
    # issue #73 nothing on an article said it existed: you had to open a tab
    # called "Details" on a hunch. The count comes from the repository like any
    # other read (rule 2), capped at `retention_versions` per page, and the
    # newest row is the page as it stands now — so the number of texts you can
    # go *back* to is one fewer.
    versions = store.page_versions(slug)
    # A proposal that nobody can see may as well not exist. Chat can now offer an
    # edit (it may not make one), and the offer used to land only on the Details
    # tab — so asking chat to change the page looked like it did nothing at all.
    # The article says a change is waiting; Details is still where it is reviewed.
    pending = store.suggestions_list(slug, status="pending")
    return templates.TemplateResponse(request,
        "page.html", _ctx(request, page=page, pending_count=len(pending),
                          trashed=bool(page.get("deleted_at")),
                          toc=render.extract_toc(page["markdown"]),
                          children=store.children(slug), parent=parent,
                          crumbs=store.ancestors(page),
                          tags=store.tags_of(slug),
                          version_count=len(versions),
                          last_changed=(versions[0]["created_at"] if versions
                                        else page.get("updated_at")),
                          comment_count=len(store.comments_list(slug)))
    )


@app.get("/wiki/{slug}/metadata", response_class=HTMLResponse)
def metadata_view(request: Request, slug: str, msg: str = "", error: str = ""):
    """The page's frontmatter properties and tags, as a peer of Article.

    This is where the automatic key/value table used to be prepended to the
    article body. Custom elements the author placed in the body stay there.
    """
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    meta, _tags, _body = structure.parse_frontmatter(page["markdown"])
    return templates.TemplateResponse(request,
        "metadata.html", _ctx(request, page=page,
                          trashed=bool(page.get("deleted_at")),
                          properties=meta, schema=store.check_metadata(meta),
                          tags=store.tags_of(slug),
                          all_tags=[t["tag"] for t in store.all_tags()],
                          parent=store.parent_of(page),
                          crumbs=store.ancestors(page),
                          comment_count=len(store.comments_list(slug)),
                          msg=msg, error=error))


@app.post("/wiki/{slug}/metadata")
async def metadata_save(request: Request, slug: str):
    """Replace the page's properties with exactly what the form submitted.

    Keys and values arrive as parallel lists so a rename is just an edited key,
    and order is whatever the form shows. `tags` is refused: tags live in the
    same block but are their own concept, and accepting one here would create a
    second, competing source for them.
    """
    form = await request.form()
    keys = [str(k).strip() for k in form.getlist("key")]
    values = [str(v) for v in form.getlist("value")]
    props: dict[str, str] = {}
    rejected = []
    for key, value in zip(keys, values):
        if not key:
            continue                       # a blank key is a deleted row
        if key.replace(" ", "").lower() == "tags":
            rejected.append(key)
            continue
        props[key] = value.strip()
    if store.replace_properties(slug, props, author="human") is None:
        raise HTTPException(404, "Page not found")
    if rejected:
        return RedirectResponse(
            f"/wiki/{quote(slug)}/metadata?error="
            + quote("'tags' is managed separately — use the tag controls"),
            status_code=303)
    return RedirectResponse(f"/wiki/{quote(slug)}/metadata?msg=Saved",
                            status_code=303)


@app.post("/wiki/{slug}/tags")
async def tags_save(request: Request, slug: str):
    """Replace a page's tags from the Metadata tab.

    Goes through the repository so the frontmatter is rewritten too — updating
    only the index would leave the page's own text disagreeing with its tags,
    and the next editor save would quietly revert it.
    """
    form = await request.form()
    tags = [str(t) for t in form.getlist("tag")]
    if store.set_tags(slug, tags, author="human") is None:
        raise HTTPException(404, "Page not found")
    return RedirectResponse(f"/wiki/{quote(slug)}/metadata?msg=Tags+saved",
                            status_code=303)


@app.get("/wiki/{slug}/details", response_class=HTMLResponse)
def details_view(request: Request, slug: str, show: str = ""):
    """The article's discussion/metadata page — settings, links, history,
    proposed edits, comments, and chat. Kept out of the article view.

    `show=history` opens the History block, which is otherwise collapsed. The
    article's "Edited …" link carries it, because a fragment never reaches the
    server: landing on a closed `<details>` after clicking something that
    promised earlier versions is a dead end for exactly the person who needs it.
    """
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    suggestions = []
    for s in store.suggestions_list(slug):
        full = store.suggestion_get(s["id"])
        s["diff"] = "\n".join(difflib.unified_diff(
            page["markdown"].splitlines(), full["markdown"].splitlines(),
            fromfile="current", tofile="proposed", lineterm=""))
        suggestions.append(s)
    return templates.TemplateResponse(request,
        "details.html", _ctx(request, page=page,
                          trashed=bool(page.get("deleted_at")),
                          versions=store.page_versions(slug),
                          backlinks=store.backlinks(slug), parent=store.parent_of(page),
                          crumbs=store.ancestors(page),
                          tags=store.tags_of(slug),
                          comments=store.comments_list(slug),
                          open_history=(show == "history"),
                          suggestions=suggestions)
    )


@app.post("/wiki/{slug}/comment")
def add_comment_view(slug: str, body: str = Form(...)):
    store.comment_add(slug, body, author="human")
    return RedirectResponse(f"/wiki/{slug}/details#comments", status_code=303)


@app.post("/wiki/{slug}/comment/{cid}/resolve")
def resolve_comment_view(slug: str, cid: int):
    store.comment_resolve(cid)
    return RedirectResponse(f"/wiki/{slug}/details#comments", status_code=303)


@app.post("/wiki/{slug}/suggestion/{sid}/apply")
def apply_suggestion_view(slug: str, sid: int):
    store.suggestion_apply(sid)
    return RedirectResponse(f"/wiki/{slug}/details", status_code=303)


@app.post("/wiki/{slug}/suggestion/{sid}/reject")
def reject_suggestion_view(slug: str, sid: int):
    store.suggestion_reject(sid)
    return RedirectResponse(f"/wiki/{slug}/details", status_code=303)


@app.get("/wikis/{slug}/export-md")
def wikis_export_markdown(slug: str):
    if not wikis.exists(slug):
        raise HTTPException(404, "No such wiki")
    data = wikis.markdown_zip(slug)
    return Response(content=data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{slug}-markdown.zip"'})


@app.get("/tags", response_class=HTMLResponse)
def tags_view(request: Request):
    return templates.TemplateResponse(request,
        "tags.html", _ctx(request, tags=store.all_tags()))


def _activity_svg(days: list) -> str:
    """Two stacked bar charts (Reads, Writes) over the last 7 days, each split
    Human (accent) / AI (danger). Inline SVG uses the theme's CSS variables."""
    import html as _h

    def panel(ox, title, kh, ka):
        vals = [(d[kh], d[ka]) for d in days]
        mx = max((h + a for h, a in vals), default=0) or 1
        cw, ch, base = 290.0, 150.0, 180.0
        step = cw / len(days)
        bw = step * 0.55
        parts = [f'<text x="{ox}" y="14" class="ag-title">{title}</text>',
                 f'<text x="{ox}" y="30" class="ag-ax">peak {mx}/day</text>']
        for i, (h, a) in enumerate(vals):
            cx = ox + i * step + (step - bw) / 2
            hh, ah = h / mx * ch, a / mx * ch
            yb = base - hh
            tip = _h.escape(days[i]["date"])
            parts.append(f'<rect x="{cx:.1f}" y="{yb:.1f}" width="{bw:.1f}" '
                         f'height="{hh:.1f}" fill="var(--accent)"><title>{tip} '
                         f'human {title.lower()}: {h}</title></rect>')
            parts.append(f'<rect x="{cx:.1f}" y="{yb - ah:.1f}" width="{bw:.1f}" '
                         f'height="{ah:.1f}" fill="var(--danger)"><title>{tip} '
                         f'AI {title.lower()}: {a}</title></rect>')
            parts.append(f'<text x="{cx + bw / 2:.1f}" y="{base + 13:.0f}" '
                         f'class="ag-ax" text-anchor="middle">{days[i]["date"][5:]}</text>')
        return "".join(parts)

    return (
        '<svg viewBox="0 0 640 214" class="activity-graph" '
        'xmlns="http://www.w3.org/2000/svg">'
        + panel(10, "Reads", "human_read", "ai_read")
        + panel(340, "Writes", "human_write", "ai_write")
        + '<rect x="10" y="202" width="10" height="10" fill="var(--accent)"/>'
          '<text x="25" y="211" class="ag-ax">Human</text>'
          '<rect x="80" y="202" width="10" height="10" fill="var(--danger)"/>'
          '<text x="95" y="211" class="ag-ax">AI</text></svg>'
    )


@app.get("/info", response_class=HTMLResponse)
def wiki_info_view(request: Request):
    """Stats about the active wiki: counts, links, size, and a 7-day activity graph."""
    wiki = db.active_wiki()
    days = store.activity_last_7_days()
    return templates.TemplateResponse(request, "info.html", _ctx(
        request, stats=wikis.stats(wiki), wiki_label=wikis.name_of(wiki),
        graph_svg=_activity_svg(days)))


@app.get("/index", response_class=HTMLResponse)
def index_view(request: Request):
    """A book-style index: every tag with the pages under it, plus an
    all-articles A–Z list (sub-pages included)."""
    tag_groups = [{"tag": t["tag"], "count": t["count"],
                   "pages": store.pages_with_tag(t["tag"])}
                  for t in store.all_tags()]
    all_pages = sorted(store.list_pages(include_children=True),
                       key=lambda p: (p["title"] or "").lower())
    return templates.TemplateResponse(request, "tagindex.html",
        _ctx(request, tag_groups=tag_groups, all_pages=all_pages))


@app.get("/tag/{tag}", response_class=HTMLResponse)
def tag_index_view(request: Request, tag: str):
    return templates.TemplateResponse(request,
        "tag.html", _ctx(request, tag=tag, pages=store.pages_with_tag(tag)))


@app.get("/changes", response_class=HTMLResponse)
def changes_view(request: Request):
    return templates.TemplateResponse(request,
        "changes.html", _ctx(request, changes=store.recent_changes(limit=100)))


@app.get("/broken-links", response_class=HTMLResponse)
def broken_links_view(request: Request):
    return templates.TemplateResponse(request,
        "broken.html", _ctx(request, broken=store.broken_links()))


@app.get("/wiki/{slug}/edit", response_class=HTMLResponse)
async def edit_page(request: Request, slug: str):
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    # Seed the CRDT room from the DB before the browser's websocket connects,
    # so the live document already has the page content.
    await collab.ensure_room(db.active_wiki(), slug)
    return templates.TemplateResponse(request,
        "edit.html", _ctx(request, page=page, is_new=False, collab=True,
                          # The parent selector lives on this page now, and it
                          # must show where the page ACTUALLY sits: without this
                          # it always reads "none", so pressing Move without
                          # touching it would quietly promote the page to
                          # top-level and orphan it from its parent.
                          parent=store.parent_of(page),
                          # Shown in the image panel: it silently shapes every
                          # generated image and is otherwise invisible here.
                          image_style_prompt=store.get_setting("image_style_prompt", ""))
    )


class AutosaveIn(BaseModel):
    slug: str = ""
    title: str
    markdown: str = ""


@app.post("/api/autosave")
def api_autosave(body: AutosaveIn):
    """Persist an in-progress edit without leaving the editor.

    New pages have no CRDT room (one only exists once a page exists), so until
    the first save their text lives solely in the browser — closing the tab lost
    it. The editor calls this on a debounce: the first call creates the page, and
    later calls update it. Existing pages are already persisted by the collab
    flusher and don't use this path."""
    title = (body.title or "").strip()
    if not title:
        return {"ok": False, "error": "needs a title"}
    if body.slug:
        page = store.update_page(body.slug, title, body.markdown, author="human")
        if not page:
            return {"ok": False, "error": "page not found"}
    else:
        page = store.create_page(title, body.markdown, author="human")
    return {"ok": True, "slug": page["slug"], "title": page["title"],
            "version": store.content_version(body.markdown)}


@app.post("/wiki/save", response_class=HTMLResponse)
def save_page(slug: str = Form(""), title: str = Form(...), markdown: str = Form("")):
    if slug:
        page = store.update_page(slug, title, markdown, author="human")
    else:
        page = store.create_page(title, markdown, author="human")
    return RedirectResponse(f"/wiki/{page['slug']}", status_code=303)


@app.post("/wiki/{slug}/delete")
def delete_page_view(slug: str):
    store.soft_delete(slug)   # to the trash (restorable)
    return RedirectResponse("/", status_code=303)


@app.post("/wiki/{slug}/star")
def star_page_view(slug: str, next: str = Form("/")):
    store.toggle_star(slug)
    return RedirectResponse(next or "/", status_code=303)


class ReorderIn(BaseModel):
    slugs: list[str]


@app.post("/reorder")
def reorder_pages(body: ReorderIn):
    """Persist the Custom sidebar order (drag-and-drop)."""
    store.set_page_order(body.slugs)
    return {"ok": True}


@app.post("/wiki/{slug}/clone")
def clone_page_view(slug: str):
    page = store.clone_page(slug)
    return RedirectResponse(f"/wiki/{page['slug']}" if page else "/", status_code=303)


@app.post("/wiki/{slug}/parent")
def set_parent_view(slug: str, parent: str = Form("")):
    store.set_parent(slug, parent or None)
    return RedirectResponse(f"/wiki/{slug}/details", status_code=303)


@app.post("/wiki/{slug}/restore")
def restore_page_view(slug: str):
    store.restore(slug)
    return RedirectResponse(f"/wiki/{slug}", status_code=303)


@app.post("/wiki/{slug}/purge")
def purge_page_view(slug: str):
    store.hard_delete(slug)   # permanent
    return RedirectResponse("/trash", status_code=303)


@app.get("/trash", response_class=HTMLResponse)
def trash_view(request: Request):
    store.sweep_trash()  # opportunistically enforce retention
    days = store.get_setting("retention_trash_days", "30")
    return templates.TemplateResponse(request,
        "trash.html", _ctx(request, trash=store.list_trash(), retention_days=days))


@app.get("/wiki/{slug}/history/{version_id}", response_class=HTMLResponse)
def history_view(request: Request, slug: str, version_id: int):
    page = store.get_page(slug)
    version = store.get_version(version_id)
    if not page or not version or version["page_id"] != page["id"]:
        raise HTTPException(404, "Version not found")
    diff = "\n".join(difflib.unified_diff(
        version["markdown"].splitlines(), page["markdown"].splitlines(),
        fromfile=f"version @ {version['created_at']}", tofile="current", lineterm=""))
    return templates.TemplateResponse(request, "history.html", _ctx(
        request, page=page, version=version,
        version_html=render.render_markdown(version["markdown"]), diff=diff))


@app.post("/wiki/{slug}/history/{version_id}/restore")
def history_restore(slug: str, version_id: int):
    store.restore_version(slug, version_id)
    return RedirectResponse(f"/wiki/{slug}", status_code=303)


@app.get("/search", response_class=HTMLResponse)
def search_view(request: Request, q: str = ""):
    # Hybrid (BM25 + embeddings) over ALL pages, sub-pages included. The advanced
    # page picks the partition/mode and can exclude sub-pages.
    results = rag.search(q, index="all", mode="hybrid") if q else []
    return templates.TemplateResponse(request,
        "search.html", _ctx(request, q=q, results=results)
    )


@app.get("/search/advanced", response_class=HTMLResponse)
def advanced_search_view(request: Request, q: str = "", index: str = "all",
                         mode: str = "hybrid", exclude_children: str = ""):
    excl = bool(exclude_children)
    results = rag.search(q, index=index, mode=mode, exclude_children=excl) if q else []
    return templates.TemplateResponse(request, "advanced.html", _ctx(
        request, q=q, index=index, mode=mode, exclude_children=excl,
        results=results, indices=rag.list_indices(), modes=rag.MODES,
        vec_off=not db.VEC_AVAILABLE))


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, msg: str = "", error: str = "",
                  detail: str = "", confirm: str = ""):
    themes = sorted(p.stem for p in (_STATIC / "themes").glob("*.css"))
    provider, model = embeddings.active()
    wiki = db.active_wiki()
    # `confirm` names a remedy the user has clicked but not yet agreed to. It is
    # matched against the registry rather than trusted: an unknown id renders no
    # panel at all.
    pending = capabilities.describe(confirm) if confirm else None
    return templates.TemplateResponse(request,
        "settings.html",
        _ctx(request, settings=store.all_settings(), themes=themes,
             capabilities=capabilities.report(), pending=pending,
             library=embeddings.get_library(), active_provider=provider,
             active_model=model, catalog=embeddings.fastembed_catalog(),
             style_refs=imagegen.list_style_refs(wiki),
             style_refs_dir=str(imagegen.style_reference_dir(wiki)),
             log_retention_hours=accesslog.retention_hours(),
             share_enabled=bool(appconfig.get("share_enabled")),
             share_has_password=auth.has_password(),
             share_lan_urls=auth.lan_urls(config.PORT),
             bonjour_on=bonjour.is_running(),
             bonjour_available=bonjour.available(),
             tunnel_url=tunnel.url(),
             tunnel_available=tunnel.available(),
             backup_enabled=backups.enabled(), backup_keep=backups.keep(),
             backup_interval=backups.interval_hours(),
             backup_list=backups.list_backups()[:10],
             backup_dir=str(config.DATA_DIR / "backups"),
             update=updater.status(),
             doorman=doorman.status(),
             msg=msg, error=error, detail=detail),
    )


@app.post("/settings/capabilities/{remedy_id}/fix")
async def capability_fix(remedy_id: str, confirm: str = Form("")):
    """Carry out one capability remedy (owner-only — guests can't reach /settings/*).

    Two steps, on purpose. The first POST only *offers*: it redirects back to a
    confirmation that names what is about to happen, including the host a script
    would be fetched from. Installing software changes the user's machine, so
    the consent is enforced here rather than in a JavaScript ``confirm()`` a
    stale form or a hand-rolled POST could skip.

    ``remedy_id`` is looked up in the registry and never used to build a
    command, so the path segment can't become an argument. The work is blocking
    (a package manager, over the network), so it runs off the event loop the way
    backups and updates already do.
    """
    plan = capabilities.describe(remedy_id)
    if not plan or plan.get("kind") == "manual":
        return RedirectResponse(
            "/settings?error=" + quote("That isn't a fix Jamgraf can carry out.")
            + "#capabilities", status_code=303)
    if not confirm:
        return RedirectResponse(f"/settings?confirm={quote(remedy_id)}#capabilities",
                                status_code=303)

    import anyio
    res = await anyio.to_thread.run_sync(capabilities.apply, remedy_id)
    if not res.get("ok"):
        return RedirectResponse(
            "/settings?error=" + quote(res.get("error", "The fix failed."))
            + "&detail=" + quote(res.get("detail", "")) + "#capabilities",
            status_code=303)
    return RedirectResponse(
        "/settings?msg=" + quote(res.get("message", "Done.")) + "#capabilities",
        status_code=303)


@app.post("/settings/sharing")
def settings_sharing(request: Request, share_enabled: str = Form(""),
                     password: str = Form("")):
    """Owner-only (the middleware blocks guests from /settings/*)."""
    if password.strip():
        auth.set_password(password.strip())
    want_on = bool(share_enabled)
    if want_on and not auth.has_password():
        return RedirectResponse(
            "/settings?error=Set+a+password+before+enabling+sharing", status_code=303)
    appconfig.set("share_enabled", want_on)
    msg = ("Sharing+on+—+restart+Jamgraf+to+bind+to+the+network"
           if want_on else "Sharing+off+—+restart+to+stop+listening")
    return RedirectResponse(f"/settings?msg={msg}", status_code=303)


@app.get("/style-ref/{filename}")
def serve_style_ref(filename: str):
    import os
    path = imagegen.style_reference_dir(db.active_wiki()) / os.path.basename(filename)
    if not path.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(str(path))


@app.post("/settings/backups/run")
async def backups_run_now(request: Request):
    """Take a snapshot now (owner-only — guests can't reach /settings/*)."""
    import anyio
    res = await anyio.to_thread.run_sync(backups.run_backup)
    if not res.get("ok"):
        return RedirectResponse(f"/settings?error={quote(res.get('error', 'failed'))}",
                                status_code=303)
    msg = f"Backed up {len(res['wikis'])} wiki(s)"
    if res.get("skipped"):
        # Say what was left out, and why it was left out — a backup that quietly
        # covers less than you think is worse than one that failed loudly.
        msg += (f" — skipped {', '.join(res['skipped'])}: "
                f"the file could not be read")
    return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)


@app.post("/settings/backups")
def backups_save(enabled: str = Form(""), keep: str = Form("7"),
                 interval_hours: str = Form("24")):
    appconfig.set("backup_enabled", bool(enabled))
    try:
        appconfig.set("backup_keep", max(1, int(keep)))
        appconfig.set("backup_interval_hours", max(1, int(interval_hours)))
    except ValueError:
        pass
    return RedirectResponse("/settings?msg=Backup+settings+saved", status_code=303)


@app.post("/settings/doorman")
def doorman_save(enabled: str = Form("")):
    """Turn the Doorman integration on or off.

    Declinable even when Doorman is running: it is the user's other app, and
    wanting it running without wanting Jamgraf to reach into it is reasonable.

    Not declinable while Doorman is the window Jamgraf is displayed in — the
    control is rendered locked there, and this refuses the same way so a stale
    form or a hand-rolled POST can't unlock it either.
    """
    if doorman.embedded():
        return RedirectResponse(
            "/settings?msg=Jamgraf+is+running+inside+Doorman,+so+the+integration"
            "+stays+on", status_code=303)
    store.set_setting("doorman_enabled", "1" if enabled else "0")
    doorman.refresh()                 # reflect the change immediately
    capabilities.refresh()            # ...including in the capabilities view
    return RedirectResponse("/settings?msg=Saved", status_code=303)


@app.post("/settings/updates")
def updates_save(auto_check: str = Form(""), interval_hours: str = Form("24")):
    appconfig.set("update_auto_check", bool(auto_check))
    try:
        appconfig.set("update_interval_hours", max(1, int(interval_hours)))
    except ValueError:
        pass
    return RedirectResponse("/settings?msg=Update+settings+saved", status_code=303)


@app.post("/settings/updates/check")
async def updates_check_now(request: Request):
    """Ask GitHub for the latest release (owner-only — guests can't reach /settings/*)."""
    import anyio
    res = await anyio.to_thread.run_sync(updater.check)
    if not res.get("ok"):
        return RedirectResponse(f"/settings?error={quote(res.get('error', 'check failed'))}",
                                status_code=303)
    if res.get("error"):            # newer release exists but isn't installable
        return RedirectResponse(f"/settings?error={quote(res['error'])}", status_code=303)
    if not res.get("available"):
        return RedirectResponse(
            f"/settings?msg=Up+to+date+({quote(res['current'])})", status_code=303)
    return RedirectResponse(
        f"/settings?msg={quote(res['latest'])}+is+available", status_code=303)


@app.post("/settings/updates/install")
async def updates_install(request: Request):
    """Download, verify, and stage an update, then quit so the helper can swap.

    The swap itself happens in a detached helper that waits for this process to
    exit — see jamgraf/updater.py. We only get to schedule it and shut down.
    """
    import anyio
    res = await anyio.to_thread.run_sync(updater.download_and_apply)
    if not res.get("ok"):
        return RedirectResponse(f"/settings?error={quote(res.get('error', 'update failed'))}",
                                status_code=303)

    async def _quit_soon():
        # Give the redirect time to reach the window, then exit so the waiting
        # helper can swap the bundle and relaunch. The lifespan shutdown runs on
        # the way out, which is what flushes collab.py's pending snapshots.
        await asyncio.sleep(1.5)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_quit_soon())
    return RedirectResponse("/settings?msg=Updating+—+Jamgraf+will+restart",
                            status_code=303)


@app.post("/settings/tunnel/start")
async def tunnel_start(request: Request):
    """Open a temporary public URL (owner-only — guests can't reach /settings/*)."""
    import anyio
    result = await anyio.to_thread.run_sync(lambda: tunnel.start(config.PORT))
    if not result.get("ok"):
        return RedirectResponse(f"/settings?error={quote(result.get('error', 'failed'))}",
                                status_code=303)
    accesslog.record("TUNNEL open", status=200, detail=result["url"])
    return RedirectResponse("/settings?msg=Public+link+is+live", status_code=303)


@app.post("/settings/tunnel/stop")
def tunnel_stop():
    tunnel.stop()
    accesslog.record("TUNNEL closed", status=200)
    return RedirectResponse("/settings?msg=Public+link+closed", status_code=303)


@app.post("/settings/style-refs")
async def upload_style_refs(files: list[UploadFile] = File(...)):
    wiki = db.active_wiki()
    for f in files:
        data = await f.read()
        if data:
            imagegen.save_style_ref(wiki, f.filename or "image", data)
    return RedirectResponse("/settings?msg=Reference+images+updated", status_code=303)


@app.post("/settings/style-refs/{filename}/delete")
def delete_style_ref_view(filename: str):
    imagegen.delete_style_ref(db.active_wiki(), filename)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings")
def settings_save(theme: str = Form(...),
                  retention_versions: str = Form("50"),
                  retention_trash_days: str = Form("30"),
                  allow_html: str = Form(""),
                  gen_provider: str = Form("anthropic"),
                  gen_model: str = Form(""),
                  gen_model_local: str = Form("phi3"),
                  ollama_url: str = Form("http://localhost:11434"),
                  chat_provider: str = Form("claude"),
                  chat_model: str = Form(""),
                  image_cli: str = Form("agy"),
                  image_model: str = Form(""),
                  image_style_prompt: str = Form(""),
                  log_retention_hours: str = Form("24")):
    store.set_setting("theme", theme)
    try:
        appconfig.set("log_retention_hours", max(1, int(log_retention_hours)))
    except ValueError:
        pass
    store.set_setting("retention_versions", str(max(0, int(retention_versions or 0))))
    store.set_setting("retention_trash_days", str(max(0, int(retention_trash_days or 0))))
    store.set_setting("gen_provider",
                      gen_provider if gen_provider in ("anthropic", "ollama") else "anthropic")
    store.set_setting("gen_model", gen_model.strip() or config.ANTHROPIC_MODEL)
    store.set_setting("gen_model_local", gen_model_local.strip() or "phi3")
    store.set_setting("ollama_url", ollama_url.strip() or "http://localhost:11434")
    store.set_setting("chat_provider",
                      chat_provider if chat_provider in ("claude", "gemini") else "claude")
    store.set_setting("chat_model", chat_model.strip())
    store.set_setting("image_cli", image_cli.strip() or "agy")
    store.set_setting("image_model", image_model.strip())
    store.set_setting("image_style_prompt", image_style_prompt.strip())
    new_html = "1" if allow_html else "0"
    if new_html != store.get_setting("allow_html", "0"):
        store.set_setting("allow_html", new_html)
        store.rerender_all()  # re-render every page under the new HTML policy
    # Which CLI, provider and URL answer are exactly what the capabilities view
    # reports on, so it has to re-probe rather than show the previous machine.
    capabilities.refresh()
    return RedirectResponse("/settings?msg=Saved", status_code=303)


@app.post("/settings/models/add")
def settings_add_model(provider: str = Form(...), slug: str = Form(...)):
    """Add an embedding model by slug, make it active, and re-embed all pages."""
    result = embeddings.add_model(provider, slug)
    if not result["ok"]:
        return RedirectResponse(f"/settings?error={result['error']}", status_code=303)
    rag.reindex_all()
    return RedirectResponse(
        f"/settings?msg=Added+and+activated+{slug}+(dim+{result['dim']})",
        status_code=303,
    )


@app.post("/settings/models/activate")
def settings_activate_model(provider: str = Form(...), model: str = Form(...)):
    embeddings.set_active(provider, model)
    rag.reindex_all()
    return RedirectResponse(f"/settings?msg=Activated+{model}", status_code=303)


@app.get("/image/{image_id}")
@app.get("/image/{image_id}/{filename}")
def serve_image(image_id: int, filename: str = ""):
    # `filename` is only for the URL to carry an extension (media detection); the
    # blob is looked up by id.
    img = store.get_image(image_id)
    if not img:
        raise HTTPException(404, "Image not found")
    return Response(content=img["data"], media_type=img["mimetype"])


# =============================================================================
# JSON REST API (LLM / programmatic)
# =============================================================================

class PageIn(BaseModel):
    title: str
    markdown: str = ""


@app.get("/api/pages")
def api_list_pages(children: bool = False):
    """Pages in the active wiki. `children=1` includes child pages, which the
    sidebar hides but the jump-to-article palette needs — you should be able to
    jump to any article, not just top-level ones."""
    return store.list_pages(include_children=children)


@app.post("/api/pages")
def api_create_page(body: PageIn):
    return store.create_page(body.title, body.markdown, author="api")


@app.get("/api/pages/{slug}")
def api_get_page(slug: str):
    page = store.get_page(slug)
    if not page:
        raise HTTPException(404, "Page not found")
    return page


@app.put("/api/pages/{slug}")
def api_update_page(slug: str, body: PageIn):
    page = store.update_page(slug, body.title, body.markdown, author="api")
    if not page:
        raise HTTPException(404, "Page not found")
    return page


@app.delete("/api/pages/{slug}")
def api_delete_page(slug: str):
    if not store.soft_delete(slug):
        raise HTTPException(404, "Page not found or already trashed")
    return {"trashed": slug}


@app.get("/api/search")
def api_search(q: str, k: int = config.RAG_TOP_K):
    """Hybrid BM25 + vector retrieval over the wiki (the RAG endpoint)."""
    return {"query": q, "results": rag.search_chunks(q, k)}


@app.get("/api/calendar-feed")
async def api_calendar_feed(url: str, tz: str = ""):
    """Events from a subscribed calendar, for the ``calendar`` element to render.

    The element cannot fetch the calendar host itself — none of them send CORS
    headers — so this same-origin route does it server-side and hands back JSON.

    The URL arrives from the page, so **the allow-list is the security boundary
    here, not the caller.** ``auth.py`` grants loopback callers owner rights and
    any web page can fire requests at our loopback port, so without
    ``calendarfeed.validate_url`` this would be an open proxy into the user's own
    network — a website could aim it at ``192.168.1.1`` or a metadata endpoint
    and use us to reach what it cannot. Validation refuses everything that isn't
    https at a known calendar provider, which is what keeps that shut. See
    ``docs/calendar-feeds.md``.
    """
    tzname = (tz or "").strip() or calendarfeed.local_tzname()
    try:
        # Blocking network + expansion; off the event loop so one slow calendar
        # host can't stall every other request.
        events = await anyio.to_thread.run_sync(
            functools.partial(calendarfeed.events_for_url, url, tzname=tzname))
    except calendarfeed.FeedError as exc:
        # 502: we're reporting someone else's failure, and the element shows the
        # reason rather than rendering an empty month as if the calendar were.
        raise HTTPException(502, str(exc))
    return {"timezone": tzname, "events": events}


@app.post("/api/images")
async def api_upload_image(file: UploadFile):
    data = await file.read()
    name = file.filename or "image"
    image_id = store.save_image(name, file.content_type or "application/octet-stream", data)
    url = f"/image/{image_id}/{name}"   # filename gives the URL an extension
    return {"id": image_id, "url": url, "markdown": f"![{name}]({url})"}


class AIRequest(BaseModel):
    prompt: str
    page_context: str | None = None
    use_rag: bool = True
    slug: str | None = None


@app.post("/api/ai/stream")
async def api_ai_stream(body: AIRequest):
    """Stream generated tokens as SSE. The editor appends them live.

    The first frame names the backend that answered (a Doorman agent, Anthropic
    or Ollama) so the editor can show it: which model wrote your page is not
    something to leave the user guessing about.
    """
    async def event_gen():
        try:
            async for evt in ai.stream_events(
                body.prompt, body.page_context, body.use_rag, body.slug or ""
            ):
                yield f"data: {json.dumps(evt)}\n\n"
            yield "data: {\"done\": true}\n\n"
        except Exception as exc:  # surface errors to the client
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# =============================================================================
# Collaboration injection (called by the MCP server so Claude writes live)
# =============================================================================

class CollabAppend(BaseModel):
    text: str


class CollabReplace(BaseModel):
    markdown: str


class CollabEdit(BaseModel):
    old: str
    new: str


@app.post("/api/collab/{slug}/edit")
async def api_collab_edit(slug: str, body: CollabEdit):
    wiki = db.active_wiki()
    if not store.get_page(slug):
        raise HTTPException(404, "Page not found")
    result = await collab.apply_edit(
        wiki, slug, lambda s: edits.plan_edit(s, body.old, body.new))
    return {"wiki": wiki, "slug": slug, **result}


@app.post("/api/collab/{slug}/op")
async def api_collab_op(slug: str, body: dict):
    """Apply a structured text op (edit/remove/prepend/insert/replace_section)
    as a live surgical edit. Used by the MCP text tools."""
    wiki = db.active_wiki()
    if not store.get_page(slug):
        raise HTTPException(404, "Page not found")
    planner = edits.make_planner(body)
    if planner is None:
        return {"wiki": wiki, "slug": slug, "ok": False,
                "error": f"unknown op '{body.get('op')}'"}
    try:
        result = await collab.apply_edit(wiki, slug, planner)
    except KeyError as exc:
        return {"wiki": wiki, "slug": slug, "ok": False,
                "error": f"missing argument {exc}"}
    return {"wiki": wiki, "slug": slug, **result}


@app.post("/api/collab/{slug}/append")
async def api_collab_append(slug: str, body: CollabAppend):
    wiki = db.active_wiki()
    if not store.get_page(slug):
        raise HTTPException(404, "Page not found")
    text = await collab.append_text(wiki, slug, body.text)
    return {"wiki": wiki, "slug": slug, "length": len(text)}


@app.post("/api/collab/{slug}/replace")
async def api_collab_replace(slug: str, body: CollabReplace):
    wiki = db.active_wiki()
    if not store.get_page(slug):
        raise HTTPException(404, "Page not found")
    text = await collab.replace_text(wiki, slug, body.markdown)
    return {"wiki": wiki, "slug": slug, "length": len(text)}


class VersionsIn(BaseModel):
    slugs: list[str]


@app.post("/api/collab/versions")
async def api_collab_versions(body: VersionsIn):
    """Fingerprint many pages in ONE call, using live text where a room is open.

    This is the cheap revalidation path: an agent holding several pages in
    context can re-check all of them without pulling any page bodies back."""
    wiki = db.active_wiki()
    out: dict[str, dict] = {}
    for slug in (body.slugs or [])[:200]:
        page = store.get_page(slug)
        if not page:
            out[slug] = {"exists": False}
            continue
        saved = page["markdown"]
        md = await collab.live_markdown(wiki, slug)
        live = md is not None and md != saved
        out[slug] = {
            "exists": True,
            "version": store.content_version(md if md is not None else saved),
            "updated_at": page.get("updated_at"),
            "trashed": bool(page.get("deleted_at")),
            "live": live,
        }
    return {"wiki": wiki, "pages": out}


@app.get("/api/collab/{slug}/live")
async def api_collab_live(slug: str):
    """Current live (possibly unsaved) markdown for a page."""
    wiki = db.active_wiki()
    md = await collab.live_markdown(wiki, slug)
    if md is None:
        page = store.get_page(slug)
        if not page:
            raise HTTPException(404, "Page not found")
        md = page["markdown"]
    return {"wiki": wiki, "slug": slug, "markdown": md}
