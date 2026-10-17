"""The local web server: API plus the built single-page app.

Listens on 127.0.0.1 only. The page is served with a per-session token baked in, which every
request from the page carries back; nothing else on the machine, or in another browser tab, can
talk to the API without it. The page itself is only served to a browser that has been let in
with a launch link (see session.py), so the token cannot simply be read out of it.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..context import Context, build_context
from ..paths import Paths
from .api import ROUTERS
from .session import COOKIE_MAX_AGE_S, LAUNCH_TTL_S, cookie_name, matches, read_token
from .state import WebState

STATIC_DIR = Path(__file__).parent / "static"
#: The server binds 127.0.0.1 only. Starlette cannot match a bracketed IPv6 literal, and nothing
#: serves on ::1 anyway.
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
#: The terminal's WebSocket. Named explicitly so an upgrade cannot swap it underneath: plain
#: "websockets" is uvicorn's wrapper over the library's legacy API, deprecated and due to become
#: an alias for this one.
WS_IMPLEMENTATION = "websockets-sansio"

#: What the page may load and talk to: itself. Mantine sets styles inline, the favicon is a data:
#: address, and the terminal is a WebSocket back to this server. No frame may hold the page, so
#: another site cannot overlay it and borrow clicks.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self' ws://127.0.0.1:* ws://localhost:*",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
SECURITY_HEADERS = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    # A link out of the page must not carry its address along, whatever is in it.
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
)


class SecurityHeaders:
    """Adds :data:`SECURITY_HEADERS` to every response. Plain ASGI rather than
    ``BaseHTTPMiddleware``, which would sit between the NDJSON streams and the client."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers += [(name, value) for name, value in SECURITY_HEADERS if name not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


LOCKED_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="sshfleet-locked" content="1">
<title>sshfleet</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='16' r='14' fill='%2322c55e'/%3E%3C/svg%3E">
<style>
body {{ font: 16px/1.55 system-ui, sans-serif; max-width: 34rem; margin: 12vh auto; padding: 0 16px; background: #1f1f1f; color: #ddd; }}
h1 {{ font-size: 1.4rem; }}
code {{ background: #2e2e2e; padding: 0.1em 0.35em; border-radius: 4px; }}
</style>
</head>
<body>
<h1>sshfleet is running here</h1>
<p>{why}</p>
<p>Run <code>{command}</code> on this computer. It opens a link that lets this browser in, and
the browser stays in afterwards.</p>
</body>
</html>
"""
NOT_LET_IN = "This browser has not been let in yet."
LINK_SPENT = (
    f"That link has been used already, or is more than {LAUNCH_TTL_S // 60} minutes old: each one works once."
)


def create_app(context: Context | None = None) -> FastAPI:
    ctx = context or build_context()
    state = WebState(ctx)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await state.start()
        try:
            yield
        finally:
            await state.stop()

    app = FastAPI(title="sshfleet", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.web = state
    # A page on another site can point a name it controls at 127.0.0.1 (DNS rebinding) and then
    # read the token out of the page as if it were same-origin. Answering only to the loopback
    # names closes that: the rebound name arrives in the Host header and is refused.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS))
    app.add_middleware(SecurityHeaders)
    for router in ROUTERS:
        app.include_router(router)

    index_path = STATIC_DIR / "index.html"
    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    def render_index() -> HTMLResponse:
        if not index_path.exists():
            return HTMLResponse(
                "<h1>sshfleet</h1><p>The web interface has not been built. Run <code>pnpm install && pnpm build</code> in <code>web/</code>.</p>",
                status_code=503,
            )
        text = index_path.read_text()
        tag = f'<meta name="sshfleet-token" content="{state.token}">'
        text = text.replace("<head>", f"<head>{tag}", 1) if "<head>" in text else tag + text
        return HTMLResponse(text, headers={"Cache-Control": "no-store"})

    def locked(request: Request, why: str) -> HTMLResponse:
        port = request.url.port
        command = "sshfleet web" if port in (None, 8765) else f"sshfleet web --port {port}"
        return HTMLResponse(
            LOCKED_PAGE.format(why=html.escape(why), command=html.escape(command)),
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )

    def let_in(response: Response, name: str) -> Response:
        response.set_cookie(
            name, state.keys.cookie, max_age=COOKIE_MAX_AGE_S, path="/", httponly=True, samesite="lax"
        )
        return response

    def page(request: Request) -> Response:
        """The page, for a browser that has been let in; see session.py."""
        name = cookie_name(request.url.port)
        signed_in = matches(request.cookies.get(name), state.keys.cookie)
        if "launch" in request.query_params:
            rest = str(request.url.remove_query_params("launch").query)
            back = request.url.path + (f"?{rest}" if rest else "")
            if state.launch.redeem(request.query_params.get("launch")):
                state.context.audit.append("web.open", status="ok", note="launch link")
                return let_in(RedirectResponse(back, status_code=303), name)
            if signed_in:
                # An old link from the history, opened in a browser that is in already.
                return RedirectResponse(back, status_code=303)
            return locked(request, LINK_SPENT)
        if not signed_in:
            return locked(request, NOT_LET_IN)
        # Kept in for another COOKIE_MAX_AGE_S from every visit.
        return let_in(render_index(), name)

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> Response:
        return page(request)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str, request: Request):
        if path.startswith(("api/", "ws/")):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = STATIC_DIR / path
        if path and candidate.is_file() and STATIC_DIR.resolve() in candidate.resolve().parents:
            return FileResponse(candidate)
        return page(request)

    return app


def _port_in_use(host: str, port: int) -> bool:
    """Whether binding the port would fail. Same socket options as uvicorn, so a port that only
    has connections in TIME_WAIT counts as free, as it does for uvicorn."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def run_web(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = True, context: Context | None = None) -> None:
    url = f"http://{host}:{port}/"
    # A taken port used to surface as uvicorn's "[Errno 98] address already in use", printed
    # above a line announcing the address as if it had started. Say who has it instead.
    if _port_in_use(host, port):
        link = _link_into_running(port, context.paths if context else Paths.detect())
        print(f"sshfleet is already running at {url}", file=sys.stderr)
        _hand_over(link, open_browser)
        return
    app = create_app(context)
    link = f"{url}?launch={app.state.web.launch.issue()}"
    print(f"sshfleet web interface: {url}")
    if open_browser:
        # Give uvicorn a moment to bind before the browser asks.
        import threading

        threading.Timer(0.8, lambda: _hand_over(link, True)).start()
    else:
        _hand_over(link, False)
    server = make_server(app, host=host, port=port, log_level="warning", ws=WS_IMPLEMENTATION, timeout_graceful_shutdown=2)
    # uvicorn.run cannot take our Server; this is what it does around Server.run.
    with contextlib.suppress(KeyboardInterrupt):
        server.run()
    if not server.started:
        raise SystemExit(3)


def _hand_over(link: str, open_browser: bool) -> None:
    """Open the launch link in the browser, or show it to whoever started this.

    Shown only on a terminal: the output of a service goes to a journal that other people may be
    able to read, and a link waiting there could be opened before its owner gets to it.
    """
    if open_browser and webbrowser.open(link):
        return
    if sys.stdout.isatty():
        reason = "no browser could be opened; " if open_browser else ""
        print(f"{reason}open this link to let a browser in (it works once, within {LAUNCH_TTL_S // 60} minutes):\n  {link}")
    else:
        print("run `sshfleet web` in a terminal on this computer to let a browser in")


def make_server(app: FastAPI, **config):
    """A uvicorn server that ends the app's open streams the moment it starts shutting down.

    See :meth:`WebState.begin_shutdown`: without it, uvicorn waits on those streams first.
    """
    import uvicorn

    class Server(uvicorn.Server):
        async def shutdown(self, sockets=None) -> None:
            app.state.web.begin_shutdown()
            await super().shutdown(sockets)
            if self.force_exit:
                # A second Ctrl+C makes uvicorn skip the app's shutdown, and asyncio.run then
                # cancels the lifespan instead: the cleanup still runs, but the cancellation is
                # reported as a failed shutdown, traceback and all. With the streams already
                # ended there is nothing left to force, so shut down properly. Bounded, in case
                # the cleanup itself is what hangs.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.lifespan.shutdown(), timeout=2)

    return Server(uvicorn.Config(app, **config))


def _start_background_server(port: int, context: Context | None):
    import threading

    app = create_app(context)
    server = make_server(app, host="127.0.0.1", port=port, log_level="warning", ws=WS_IMPLEMENTATION)
    thread = threading.Thread(target=server.run, daemon=True, name="sshfleet-web")
    thread.start()
    return server, app


#: Browsers that can open a URL as a plain window, without tabs or an address bar.
APP_MODE_BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave", "brave-browser", "microsoft-edge", "microsoft-edge-stable")


def open_desktop_window(url: str, *, which=shutil.which, run=subprocess.run, open_default=webbrowser.open) -> str:
    """Show ``url`` in the best window available; returns what was used.

    pywebview first, when it is installed *and* has a working GUI backend (on Linux that is GTK
    or Qt with Python bindings, which a plain ``pip install pywebview`` does not bring). Then a
    Chromium-family browser in app mode, which is a clean window. Then any browser at all.
    """
    try:
        import webview

        webview.create_window("sshfleet", url, width=1500, height=950, min_size=(1000, 650))
        webview.start()
        return "pywebview"
    except ImportError:
        pass
    except Exception as exc:  # pywebview is present but cannot draw (no GTK/Qt bindings, no display, ...)
        print(f"pywebview cannot open a window here ({type(exc).__name__}); using a browser window instead", file=sys.stderr)

    browser = next((which(b) for b in APP_MODE_BROWSERS if which(b)), None)
    if browser is not None:
        run([browser, f"--app={url}", "--window-size=1500,950"], check=False)
        return f"app-mode:{browser}"
    plain = next((which(b) for b in ("firefox", "epiphany", "xdg-open") if which(b)), None)
    if plain is not None:
        run([plain, url], check=False)
        return f"browser:{plain}"
    # macOS has none of the above on PATH; the standard library knows `open`.
    if open_default(url):
        return "browser:default"
    raise SystemExit(
        "desktop mode needs a browser or pywebview with GTK/Qt bindings (`uv sync --extra desktop` plus "
        f"`pip install pywebview[qt]`); alternatively run `sshfleet web` and open {url}"
    )


@dataclass(frozen=True, slots=True)
class Running:
    """What answers on a port, when it is sshfleet."""

    #: Started before the code now on disk, so it would serve the new page from the new bundle
    #: while answering it with the routes it imported at startup.
    stale: bool
    #: A fresh launch link into it, or None when it keeps another configuration's keys.
    link: str | None


def _running(port: int, paths: Paths) -> Running | None:
    """The sshfleet serving on the port, if that is what is there."""
    import json
    import re
    import urllib.error
    import urllib.request

    from .freshness import is_stale

    base = f"http://127.0.0.1:{port}"

    def call(path: str, token: str, *, method: str = "GET") -> dict:
        request = urllib.request.Request(base + path, method=method, headers={"x-sshfleet-token": token})
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read())

    try:
        with urllib.request.urlopen(base + "/", timeout=1) as response:
            page = response.read(8192)
    except urllib.error.HTTPError as exc:
        page = exc.read(8192)  # the page a browser that is not in yet is given, with a 401
    except OSError:
        return None

    if b"sshfleet-locked" in page:
        token = read_token(paths.web_session_file)
    elif match := re.search(rb'name="sshfleet-token" content="([^"]+)"', page):
        # A version from before launch links: it hands its page, token and all, to anyone.
        token = match.group(1).decode()
    elif b"sshfleet-token" in page:
        return Running(stale=False, link=None)
    else:
        return None
    if token is None:
        return Running(stale=False, link=None)

    try:
        started_at = float(call("/api/config", token).get("started_at") or 0.0)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return Running(stale=False, link=None)
        return Running(stale=True, link=None)
    except (OSError, ValueError):
        return Running(stale=True, link=None)  # it cannot answer the current API at all, which is the stale case
    if is_stale(started_at):
        return Running(stale=True, link=None)
    try:
        path = call("/api/session/launch", token, method="POST")["path"]
    except (OSError, ValueError, KeyError, TypeError):
        return Running(stale=True, link=None)  # older than launch links
    return Running(stale=False, link=base + path)


def _link_into_running(port: int, paths: Paths) -> str:
    """A launch link into the sshfleet already on the port, or the reason there is none."""
    url = f"http://127.0.0.1:{port}/"
    running = _running(port, paths)
    if running is None:
        raise SystemExit(f"port {port} is taken by another program; choose another with `sshfleet web --port {port + 1}`")
    if running.stale:
        raise SystemExit(
            f"an older sshfleet is still running at {url}, started before this version was installed. "
            "Stop it (Ctrl+C where it runs, or `sshfleet service stop`) and start this one again."
        )
    if running.link is None:
        raise SystemExit(
            f"another sshfleet is running at {url}, with a configuration other than this one (another "
            "SSHFLEET_HOME or user), so it cannot be opened from here. Open it from where it was started, "
            f"or start this one with `--port {port + 1}`."
        )
    return running.link


def run_desktop(port: int = 8765, *, context: Context | None = None) -> None:
    """A native window around the same server; see :func:`open_desktop_window` for what "native" means.

    If `sshfleet web` is already running on the port, the window simply opens onto it instead
    of failing to bind a second server.
    """
    url = f"http://127.0.0.1:{port}/"
    if _port_in_use("127.0.0.1", port):
        link = _link_into_running(port, context.paths if context else Paths.detect())
        print(f"sshfleet is already serving {url}; opening a window onto it", file=sys.stderr)
        open_desktop_window(link)
        return
    server, app = _start_background_server(port, context)
    try:
        open_desktop_window(f"{url}?launch={app.state.web.launch.issue()}")
    finally:
        server.should_exit = True
