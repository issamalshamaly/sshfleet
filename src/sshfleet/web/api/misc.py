"""Audit trail and health."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ...audit.log import AuditFilter, parse_since, render
from ..security import require_token
from ..state import WebState

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def _state(request: Request) -> WebState:
    return request.app.state.web


def _filter(action: str | None, envs: str | None, host: str | None, status: str | None, since: str | None, until: str | None) -> AuditFilter:
    """``envs`` is the comma-separated environments in view; entries with no environment always
    match. ``action`` may be a glob (``container.*``)."""
    try:
        return AuditFilter(
            since=parse_since(since) if since else None,
            until=parse_since(until) if until else None,
            actions=tuple(a for a in (action or "").split(",") if a),
            envs=tuple(e for e in envs.split(",") if e) if envs is not None else None,
            host=host or None,
            status=status or None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/audit")
async def audit(
    request: Request,
    limit: int = 100,
    action: str | None = None,
    envs: str | None = None,
    host: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
):
    """The newest matching entries first, how many match in the whole trail — so filtering is not
    limited to what happens to be loaded — and every action name, for the filter to offer."""
    flt = _filter(action, envs, host, status, since, until)
    entries, total, actions = _state(request).context.audit.query(flt, min(max(limit, 1), 5000))
    return {"entries": entries, "total": total, "actions": actions}


@router.get("/audit/export")
async def audit_export(
    request: Request,
    format: str = "csv",
    action: str | None = None,
    envs: str | None = None,
    host: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
):
    """Every matching entry, oldest first, as a CSV for a spreadsheet or JSON Lines for a script."""
    if format not in {"csv", "jsonl"}:
        raise HTTPException(400, "format is csv or jsonl")
    state = _state(request)
    flt = _filter(action, envs, host, status, since, until)
    body, count = render(state.context.audit.iter_entries(flt), format)
    state.context.audit.append("audit.export", status="ok", note=f"{count} entries as {format}")
    return Response(
        body,
        media_type="text/csv; charset=utf-8" if format == "csv" else "application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="sshfleet-audit-{time.strftime("%Y%m%d")}.{format}"',
            "Cache-Control": "no-store",
        },
    )


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


@router.post("/session/launch")
async def launch_link(request: Request) -> dict[str, str]:
    """A fresh launch link, for `sshfleet web` or `sshfleet desktop` run again while this server
    is up. Behind the token, which only this user's processes can read from the session file."""
    return {"path": f"/?launch={_state(request).launch.issue()}"}
