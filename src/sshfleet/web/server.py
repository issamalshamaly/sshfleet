"""The local web server: API plus the built single-page app.

Listens on 127.0.0.1 only. The page is served with a per-session token baked in, which every
request from the page carries back; nothing else on the machine, or in another browser tab, can
talk to the API without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..context import Context, build_context
from .api import ROUTERS
from .state import WebState

STATIC_DIR = Path(__file__).parent / "static"
#: The server binds 127.0.0.1 only. Starlette cannot match a bracketed IPv6 literal, and nothing
#: serves on ::1 anyway.
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
#: The terminal's WebSocket. Named explicitly so an upgrade cannot swap it underneath: plain
#: "websockets" is uvicorn's wrapper over the library's legacy API, deprecated and due to become
#: an alias for this one.
WS_IMPLEMENTATION = "websockets-sansio"


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
        html = index_path.read_text()
        tag = f'<meta name="sshfleet-token" content="{state.token}">'
        html = html.replace("<head>", f"<head>{tag}", 1) if "<head>" in html else tag + html
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return render_index()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str, request: Request):
        if path.startswith(("api/", "ws/")):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = STATIC_DIR / path
        if path and candidate.is_file() and STATIC_DIR.resolve() in candidate.resolve().parents:
            return FileResponse(candidate)
        return render_index()

    return app


def run_web(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = True, context: Context | None = None) -> None:
    app = create_app(context)
    url = f"http://{host}:{port}/"
    if open_browser:
        # Give uvicorn a moment to bind before the browser asks.
        import threading

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"sshfleet web interface: {url}")
    server = make_server(app, host=host, port=port, log_level="warning", ws=WS_IMPLEMENTATION, timeout_graceful_shutdown=2)
    # uvicorn.run cannot take our Server; this is what it does around Server.run.
    with contextlib.suppress(KeyboardInterrupt):
        server.run()
    if not server.started:
        raise SystemExit(3)


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
    return server


#: Browsers that can open a URL as a plain window, without tabs or an address bar.
APP_MODE_BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave", "brave-browser", "microsoft-edge", "microsoft-edge-stable")


def open_desktop_window(url: str, *, which=shutil.which, run=subprocess.run) -> str:
    """Show ``url`` in the best window available; returns what was used.

    pywebview first, when it is installed *and* has a working GUI backend (on Linux that is GTK
    or Qt with Python bindings, which a plain ``pip install pywebview`` does not bring). Then a
    Chromium-family browser in app mode, which is a clean window. Then any browser at all.
    """
    try:
        import webview  # type: ignore[import-not-found]

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
    raise SystemExit(
        "desktop mode needs a browser or pywebview with GTK/Qt bindings (`uv sync --extra desktop` plus "
        f"`pip install pywebview[qt]`); alternatively run `sshfleet web` and open {url}"
    )


def _already_serving(port: int) -> tuple[bool, bool]:
    """``(serving, stale)`` for an sshfleet server on the port.

    Stale means it started before the code now on disk, so it would serve the new page from the
    new bundle while answering it with the routes it imported at startup.
    """
    import json
    import re
    import urllib.request

    from .freshness import is_stale

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            page = response.read(8192)
    except OSError:
        return (False, False)
    if b"sshfleet-token" not in page:
        return (False, False)

    match = re.search(rb'name="sshfleet-token" content="([^"]+)"', page)
    if match is None:
        return (True, False)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/config", headers={"x-sshfleet-token": match.group(1).decode()}
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            started_at = float(json.loads(response.read()).get("started_at") or 0.0)
    except (OSError, ValueError, json.JSONDecodeError):
        return (True, True)  # it cannot answer the current API at all, which is the stale case
    return (True, is_stale(started_at))


def run_desktop(port: int = 8765, *, context: Context | None = None) -> None:
    """A native window around the same server; see :func:`open_desktop_window` for what "native" means.

    If `sshfleet web` is already running on the port, the window simply opens onto it instead
    of failing to bind a second server.
    """
    url = f"http://127.0.0.1:{port}/"
    serving, stale = _already_serving(port)
    if serving and stale:
        raise SystemExit(
            f"an older sshfleet is already serving {url}. It would hand the window the rebuilt "
            "page and answer it with the routes it started with, so half of it would fail.\n"
            "Stop that one first, then run this again."
        )
    if serving:
        print(f"sshfleet is already serving {url}; opening a window onto it", file=sys.stderr)
        open_desktop_window(url)
        return
    server = _start_background_server(port, context)
    try:
        open_desktop_window(url)
    finally:
        server.should_exit = True
