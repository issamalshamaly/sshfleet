"""Port forwards."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..security import require_token
from ..state import WebState

router = APIRouter(prefix="/api/tunnels", dependencies=[Depends(require_token)])


def _state(request: Request) -> WebState:
    return request.app.state.web


@router.get("")
async def list_tunnels(request: Request, env: str | None = None):
    state = _state(request)
    return [state.tunnel_watch.describe(s) for s in state.tunnels.status(env)]


@router.post("/{name}/up")
async def tunnel_up(request: Request, name: str):
    state = _state(request)
    try:
        result = await state.tunnels.up(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    state.context.audit.append(
        "tunnel.up", env=result.env, host=result.via, target=name,
        status="ok" if result.up else "failed", note=result.endpoint,
    )
    if not result.up:
        raise HTTPException(502, result.error or "tunnel failed")
    return state.tunnel_watch.describe(result)


@router.post("/{name}/down")
async def tunnel_down(request: Request, name: str):
    state = _state(request)
    try:
        result = await state.tunnels.down(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    state.context.audit.append(
        "tunnel.down", env=result.env, host=result.via, target=name,
        status="failed" if result.held else "ok", note=result.error or None,
    )
    if result.held:
        # Closed as far as sshfleet is concerned, but the port is not free: say so, and who.
        raise HTTPException(409, result.error)
    return state.tunnel_watch.describe(result)
