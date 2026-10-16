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

from ...bundle import MAX_BUNDLE_BYTES, BundleError, StalePlan, build, parse, plan_import, to_json
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


async def _body(request: Request) -> bytes:
    """The uploaded bundle, read with a cap: nothing past the limit is ever held in memory."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BUNDLE_BYTES:
            raise HTTPException(413, f"a bundle is at most {MAX_BUNDLE_BYTES // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/import")
async def import_bundle(
    request: Request,
    dry_run: bool = True,
    mode: str = "merge",
    prefer: str = "local",
    allow_commands: bool = False,
    with_local: bool = False,
    digest: str = "",
):
    """The bundle is the request body. ``dry_run`` (the default) answers with everything the import
    would change and a digest of that plan; sending the same bundle back with that digest and
    ``dry_run=false`` applies exactly that plan — or answers 409 if anything moved in between."""
    state = _state(request)
    try:
        bundle = parse(await _body(request))
    except BundleError as exc:
        raise HTTPException(400, str(exc)) from exc
    options = {"mode": mode, "prefer": prefer, "allow_commands": allow_commands, "with_local": with_local}
    manifest = bundle.get("manifest") or {}
    about = {k: manifest.get(k) for k in ("created_at", "created_on", "sshfleet_version", "scope", "with_local")}
    if dry_run:
        await state.catch_up()
        try:
            plan = await asyncio.to_thread(plan_import, bundle, config=state.context.config, paths=state.context.paths, **options)
        except BundleError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"bundle": about, "plan": plan.to_dict()}
    if not digest:
        raise HTTPException(400, "an import is applied with the digest of the dry run it was looked at in")
    if state.config_busy:
        raise HTTPException(409, "another restore or import is writing the configuration; try again in a moment")
    try:
        result = await state.import_bundle(bundle, digest=digest, **options)
    except StalePlan as exc:
        raise HTTPException(409, str(exc)) from exc
    except BundleError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"bundle": about, **result}
