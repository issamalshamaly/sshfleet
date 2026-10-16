"""Export the setup as a bundle, and bring one in. See bundle.py for what a bundle carries.

The export is fetched with the session header like any other call and saved by the page from
the response, so the token never appears in an address. The bundle is built and checked for
secrets before a byte is sent; a refused export is a 400 that says why.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from ...bundle import BundleError, build, to_json
from ..security import require_token
from ..state import WebState

router = APIRouter(prefix="/api/settings", dependencies=[Depends(require_token)])


def _state(request: Request) -> WebState:
    return request.app.state.web


@router.get("/export")
async def export(
    request: Request,
    project: Annotated[list[str] | None, Query()] = None,
    env: Annotated[list[str] | None, Query()] = None,
    with_local: bool = False,
    allow_commands: bool = False,
):
    state = _state(request)
    context = state.context
    project, env = project or [], env or []
    try:
        bundle, warnings = await asyncio.to_thread(
            build,
            context.config,
            context.inventory,
            context.paths,
            projects=project or None,
            environments=env or None,
            with_local=with_local,
            allow_commands=allow_commands,
        )
    except BundleError as exc:
        context.audit.append("config.export", status="denied", note=str(exc)[:300])
        raise HTTPException(400, str(exc)) from exc
    scope = ", ".join(project + env) or "everything"
    context.audit.append(
        "config.export",
        target=scope,
        status="ok",
        note=f"{len(bundle['fleet'].get('environments') or [])} environment(s){', with this machine' + chr(39) + 's paths' if with_local else ''}",
    )
    label = (project or env or ["all"])[0]
    filename = f"sshfleet-{label}-{time.strftime('%Y%m%d')}.json"
    return Response(
        to_json(bundle),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            # What was left out and why, for the page to say; ASCII-safe because json.dumps escapes.
            "X-Sshfleet-Warnings": json.dumps(warnings),
        },
    )
