"""A per-session token so a page in another tab cannot drive this tool.

The server only listens on localhost, but any website you have open can still send requests to
``http://127.0.0.1:8765``. Requiring a header that only the served page knows blocks that: a
cross-origin page cannot read the token, and a custom header forces a CORS preflight the server
refuses. The page itself is only served to a browser that has been let in (see session.py), so
another user or program on the machine cannot read the token out of it either.

The header is the only way in, with two narrow exceptions. A browser download is a plain link, and
a WebSocket cannot carry custom headers, so those few routes also take the token from the address.
Everywhere else a token in the address is ignored: addresses end up in history, logs and the
Referer header, and a token that works on every route from there is a token that leaked.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request, WebSocket, status

from .session import matches

HEADER = "x-sshfleet-token"


def _refuse() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing or invalid session token")


def require_token(request: Request) -> None:
    if not matches(request.headers.get(HEADER), request.app.state.web.token):
        raise _refuse()


def require_token_or_query(request: Request) -> None:
    """For a route the browser opens as a plain link — a download — where no header can be set."""
    provided = request.headers.get(HEADER) or request.query_params.get("token")
    if not matches(provided, request.app.state.web.token):
        raise _refuse()


def same_origin(websocket: WebSocket) -> bool:
    """A browser names the page that opens a WebSocket in ``Origin``, and the server must check
    it: unlike ``fetch``, a WebSocket is not held back by CORS. Only the page this server served
    may open one. A client that is not a browser sends no Origin, and still needs the token."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and parts.netloc == websocket.headers.get("host", "")


def websocket_token_ok(websocket: WebSocket) -> bool:
    provided = websocket.query_params.get("token") or websocket.headers.get(HEADER)
    return same_origin(websocket) and matches(provided, websocket.app.state.web.token)
