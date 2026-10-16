"""A per-session token so a page in another tab cannot drive this tool.

The server only listens on localhost, but any website you have open can still send requests to
``http://127.0.0.1:8765``. Requiring a header that only the served page knows blocks that: a
cross-origin page cannot read the token, and a custom header forces a CORS preflight the server
refuses. The token is generated at startup and embedded in the page.

The header is the only way in, with two narrow exceptions. A browser download is a plain link, and
a WebSocket cannot carry custom headers, so those few routes also take the token from the address.
Everywhere else a token in the address is ignored: addresses end up in history, logs and the
Referer header, and a token that works on every route from there is a token that leaked.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, WebSocket, status

HEADER = "x-sshfleet-token"


def _matches(provided: str | None, token: str) -> bool:
    if not provided:
        return False
    # Constant time, so the token cannot be guessed a character at a time from response timings.
    return secrets.compare_digest(provided.encode(), token.encode())


def _refuse() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing or invalid session token")


def require_token(request: Request) -> None:
    if not _matches(request.headers.get(HEADER), request.app.state.web.token):
        raise _refuse()


def require_token_or_query(request: Request) -> None:
    """For a route the browser opens as a plain link — a download — where no header can be set."""
    provided = request.headers.get(HEADER) or request.query_params.get("token")
    if not _matches(provided, request.app.state.web.token):
        raise _refuse()


def websocket_token_ok(websocket: WebSocket) -> bool:
    provided = websocket.query_params.get("token") or websocket.headers.get(HEADER)
    return _matches(provided, websocket.app.state.web.token)
