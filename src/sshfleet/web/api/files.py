"""Browse, download and upload files. The deny list applies exactly as in the CLI."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from ...files import DeniedPath, download, download_dir_for, list_dir, preview, upload
from ...files.browse import resolve_file
from ...files.denylist import reason_denied
from ..security import require_token, require_token_or_query
from ..state import WebState

router = APIRouter(prefix="/api/files", dependencies=[Depends(require_token)])
# The browser opens a download as a plain link, which cannot carry the header.
downloads = APIRouter(prefix="/api/files", dependencies=[Depends(require_token_or_query)])


def _state(request: Request) -> WebState:
    return request.app.state.web


def _host(state: WebState, alias: str):
    try:
        host = state.context.inventory.host(alias)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    reason = state.context.inventory.paused_reason(alias)
    if reason is not None:
        raise HTTPException(409, f"{alias}: {reason}")
    return host

#: What a copy answers in the demo, where there is no file behind any name.
NOT_IN_DEMO = "Not in the demo: these servers are made up, so there is no file to copy."


@router.get("/ls")
async def ls(request: Request, alias: str, path: str = "", hidden: bool = False):
    state = _state(request)
    host = _host(state, alias)
    target = path or f"/home/{host.user or 'ubuntu'}"
    try:
        listing = await list_dir(state.context.runner, host, target, deny_extra=state.context.config.files.deny_extra)
    except NotADirectoryError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(502, str(exc)) from exc
    # Reads are recorded as well as changes: what was looked at on a host can matter later.
    state.context.audit.append(
        "file.list", env=host.env, host=alias, target=listing.path, status="ok", note="with sudo" if listing.used_sudo else None
    )
    return {
        "host": alias,
        "path": listing.path,
        "used_sudo": listing.used_sudo,
        "truncated": listing.truncated,
        "entries": [
            {
                "name": e.name,
                "kind": e.kind,
                "size": e.size,
                "mtime": e.mtime,
                "mode": e.mode,
                "owner": e.owner,
                "group": e.group,
                "link": e.link_target,
                "denied": e.denied_reason,
            }
            for e in listing.entries
            if hidden or not e.name.startswith(".")
        ],
    }


@router.get("/preview", response_class=PlainTextResponse)
async def file_preview(request: Request, alias: str, path: str, bytes: int = 65536):
    state = _state(request)
    host = _host(state, alias)
    audit = state.context.audit
    try:
        text = await preview(
            state.context.runner, host, path, max_bytes=min(bytes, 1_000_000),
            deny_extra=state.context.config.files.deny_extra,
        )
    except DeniedPath as exc:
        audit.append("file.preview", env=host.env, host=alias, target=path, status="denied", note=exc.reason)
        raise HTTPException(403, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(502, str(exc)) from exc
    audit.append("file.preview", env=host.env, host=alias, target=path, status="ok")
    return text


@downloads.get("/download")
async def file_download(request: Request, alias: str, path: str):
    """Fetch through the tool (deny list, audit, sudo fallback), then hand it to the browser."""
    state = _state(request)
    context = state.context
    host = _host(state, alias)
    if state.demo:
        # The deny list still answers first, both checks of it, so the demo shows what would be
        # refused: the name asked for, then what it leads to.
        try:
            reason = reason_denied(path, context.config.files.deny_extra)
            if reason:
                raise DeniedPath(path, reason)
            await resolve_file(context.runner, host, path, deny_extra=context.config.files.deny_extra)
        except DeniedPath as exc:
            raise HTTPException(403, str(exc)) from exc
        raise HTTPException(409, NOT_IN_DEMO)
    target = download_dir_for(context.download_dir, host) / Path(path).name
    try:
        result = await download(context.runner, host, path, target, deny_extra=context.config.files.deny_extra)
    except DeniedPath as exc:
        context.audit.append("file.download", env=host.env, host=alias, target=path, status="denied", note=exc.reason)
        raise HTTPException(403, str(exc)) from exc
    context.audit.append(
        "file.download", env=host.env, host=alias, target=path,
        status="ok" if result.ok else "failed", note=f"web, {result.method}, {result.bytes} bytes",
    )
    if not result.ok:
        raise HTTPException(502, result.message or "download failed")
    return FileResponse(result.destination, filename=Path(path).name, media_type="application/octet-stream")


@router.post("/upload")
async def file_upload(
    request: Request,
    alias: Annotated[str, Form()],
    directory: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
):
    state = _state(request)
    context = state.context
    host = _host(state, alias)
    if state.demo:
        raise HTTPException(409, NOT_IN_DEMO)
    name = Path(file.filename or "upload").name
    remote = directory.rstrip("/") + "/" + name

    with tempfile.NamedTemporaryFile(delete=False, prefix="sshfleet-upload-") as handle:
        while chunk := await file.read(1 << 20):
            handle.write(chunk)
        local = Path(handle.name)

    try:
        result = await upload(context.runner, host, local, remote, deny_extra=context.config.files.deny_extra)
    except DeniedPath as exc:
        raise HTTPException(403, str(exc)) from exc
    finally:
        local.unlink(missing_ok=True)

    context.audit.append(
        "file.upload", env=host.env, host=alias, target=remote,
        status="ok" if result.ok else "failed", note=f"web, {result.method}, {result.bytes} bytes",
    )
    if not result.ok:
        raise HTTPException(502, result.message or "upload failed")
    return result.to_dict()
