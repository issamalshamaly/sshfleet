"""Fleet, hosts and the live event stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ... import tz
from ...config import defaults as D
from ...inventory import EnvScope
from ..freshness import is_stale
from ..security import require_token
from ..state import WebState

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def _state(request: Request) -> WebState:
    return request.app.state.web


@router.get("/config")
async def get_config(request: Request):
    state = _state(request)
    config = state.context.config
    from ... import __version__

    return {
        "version": __version__,
        "projects": [
            {"name": p.name, "color": p.color, "description": p.description, "paused": p.paused}
            for p in config.projects
        ],
        "environments": state.environments_payload(),
        # slotted dataclasses have no __dict__, so vars() would fail here
        "thresholds": asdict(config.thresholds),
        "refresh": asdict(config.refresh),
        "logs": {
            "default_before": config.logs.default_before,
            "default_after": config.logs.default_after,
            "default_window": config.logs.default_window,
            "tail_lines": config.logs.tail_lines,
            "max_bytes": config.logs.max_bytes,
            "display_tz": tz.resolve_name(config.logs.display_tz),
        },
        "ui": {**D.DEFAULT_CONFIG["ui"], **(config.ui or {}), "sounds": {**D.DEFAULT_CONFIG["ui"]["sounds"], **((config.ui or {}).get("sounds") or {})}},
        "download_dir": str(state.context.download_dir),
        "warnings": state.context.warnings,
        "started_at": state.started_at,
        # The page is read from disk every time, the routes behind it only once. When they no
        # longer match, say so rather than let the mismatch surface as a puzzling error.
        "stale": is_stale(state.started_at),
        "demo": state.demo,
    }


@router.get("/environments")
async def environments(request: Request):
    return _state(request).environments_payload()


@router.get("/fleet")
async def fleet(request: Request, env: str | None = None):
    return _state(request).fleet_payload(env)


@router.post("/fleet/refresh")
async def refresh(request: Request, env: str | None = None, alias: str | None = None, project: str | None = None):
    """Re-read one host, one environment, one project's environments, or (none given) all."""
    state = _state(request)
    if alias:
        try:
            state.context.inventory.host(alias)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        reason = state.context.inventory.paused_reason(alias)
        if reason is not None:
            raise HTTPException(409, f"{alias}: {reason}")
        await state.refresh(alias=alias)
        return {"ok": True, "host": state.host_payload(alias)}
    scope: EnvScope = env
    if not env and project:
        config = state.context.config
        if config.project(project) is None:
            raise HTTPException(404, f"unknown project {project!r}")
        scope = [e.id for e in config.environments_of(project)]
    state.spawn(state.refresh(scope))
    return {"ok": True, "started": True}


@router.get("/hosts/{alias}")
async def host(request: Request, alias: str):
    state = _state(request)
    try:
        return state.host_payload(alias)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/alerts")
async def alerts(request: Request, env: str | None = None):
    from ..state import alert_payload

    return [alert_payload(a) for a in _state(request).fleet.state.alerts(env)]


@router.get("/events")
async def events(request: Request):
    """Newline-delimited JSON, one event per line, for as long as the browser stays connected."""
    state = _state(request)
    queue = state.events.subscribe()

    async def stream() -> AsyncGenerator[bytes, None]:
        try:
            yield (json.dumps({"type": "hello", "clients": state.events.clients}) + "\n").encode()
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield b'{"type":"ping"}\n'
                    continue
                if event is None:  # the server is shutting down
                    return
                yield (json.dumps(event, default=str) + "\n").encode()
        finally:
            state.events.unsubscribe(queue)

    return StreamingResponse(state.until_shutdown(stream()), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})
