"""Audit trail and health."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..security import require_token
from ..state import WebState

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def _state(request: Request) -> WebState:
    return request.app.state.web


@router.get("/audit")
async def audit(request: Request, limit: int = 100):
    """The newest entries first, with the total so the page knows if there is more to show."""
    entries, total = _state(request).context.audit.page(min(limit, 5000))
    return {"entries": list(reversed(entries)), "total": total}


@router.get("/health")
async def health(request: Request):
    state = _state(request)
    return {
        "ok": True,
        "clients": state.events.clients,
        "mux_alive": len(state.context.mux.alive),
        "hosts": len(state.context.inventory.hosts()),
        "refreshing": state.fleet.state.refreshing,
        "last_refresh": state.fleet.state.last_refresh,
    }
