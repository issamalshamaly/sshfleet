"""A per-session token so a page in another tab cannot drive this tool.

The server only listens on localhost, but any website you have open can still send requests to
``http://127.0.0.1:8765``. Requiring a header that only the served page knows blocks that: a
cross-origin page cannot read the token, and a custom header forces a CORS preflight the server
refuses. The token is generated at startup and embedded in the page.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, WebSocket, status

HEADER = "x-sshfleet-token"


def require_token(request: Request) -> None:
    state = request.app.state.web
    provided = request.headers.get(HEADER) or request.query_params.get("token")
    if provided != state.token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing or invalid session token")


def websocket_token_ok(websocket: WebSocket) -> bool:
    state = websocket.app.state.web
    provided = websocket.query_params.get("token") or websocket.headers.get(HEADER)
    return provided == state.token
