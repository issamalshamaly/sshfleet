"""Log search over a streaming response, and bundles the browser can download."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...logs import (
    Bundle,
    LogLine,
    LogSource,
    SearchCoordinator,
    SearchDone,
    SearchRequest,
    SourceDone,
    SourceError,
    SourceProgress,
    TimeWindow,
    bundle_dir,
    container_source_for,
    host_sources,
    known_source_for,
    main_sources,
    workload_sources,
)
from ...logs.follow import follow
from ..security import require_token
from ..state import WebState

router = APIRouter(prefix="/api/logs", dependencies=[Depends(require_token)])


def _state(request: Request) -> WebState:
    return request.app.state.web


class Selection(BaseModel):
    """What to search. Several fields may be combined; duplicates are removed."""

    main: bool = False
    #: Aliases of starred hosts to narrow the default search to; empty means all of them.
    starred_hosts: list[str] = Field(default_factory=list)
    hosts: list[str] = Field(default_factory=list)
    containers: list[dict] = Field(default_factory=list)  # {host, name}
    #: ``{host, id}`` as the sources listing gave it; ``{host, path}`` for an ad-hoc file.
    files: list[dict] = Field(default_factory=list)
    include_files: bool = False


class SearchBody(BaseModel):
    env: str
    selection: Selection
    pattern: str = ""
    mode: str = "regex"
    ignore_case: bool = True
    before: int = 60
    after: int = 0
    window: str = "30m"  # "30m" | "10:00..11:30" | ISO range
    tz: str | None = None
    tail_lines: int | None = None
    max_bytes: int | None = None
    download: bool = False
    raw_dump: bool = False


class FollowBody(BaseModel):
    env: str
    selection: Selection
    pattern: str = ""
    since: str = "1m"


def source_payload(source: LogSource) -> dict:
    return {
        "id": source.id,
        "kind": source.kind.value,
        "env": source.env,
        "label": source.label,
        "host": source.host_alias,
        "service": source.service,
        "container": source.container,
        "path": source.path,
        "format": source.log_format.value,
    }


def resolve_sources(state: WebState, env: str, selection: Selection) -> list[LogSource]:
    context = state.context
    out: list[LogSource] = []
    if selection.main or selection.starred_hosts:
        out += main_sources(context.inventory, env, state.fleet.snapshot, hosts=selection.starred_hosts or None)
    for alias in selection.hosts:
        host = context.inventory.host(alias)
        snapshot = state.fleet.snapshot(alias)
        out += host_sources(host, snapshot) if selection.include_files else workload_sources(host, snapshot)
    for item in selection.containers:
        out.append(container_source_for(context.inventory.host(item["host"]), item["name"]))
    for item in selection.files:
        host = context.inventory.host(item["host"])
        wanted = str(item.get("id") or item.get("path") or "")
        out.append(known_source_for(host, wanted, state.fleet.snapshot(host.alias)))
    seen: set[str] = set()
    unique = []
    for source in out:
        if source.id not in seen:
            seen.add(source.id)
            unique.append(source)
    return unique


@router.get("/sources")
async def sources(request: Request, env: str, host: str | None = None):
    """Everything searchable in an environment: the main services, plus per-host containers."""
    state = _state(request)
    context = state.context
    try:
        context.config.environment(env)
    except Exception as exc:
        raise HTTPException(404, str(exc)) from exc

    main = main_sources(context.inventory, env, state.fleet.snapshot)
    hosts_payload = []
    for h in context.inventory.hosts(env):
        if host and h.alias != host:
            continue
        snapshot = state.fleet.snapshot(h.alias)
        containers = [
            {"name": c.name, "state": c.state, "watched": h.watches(c.name)}
            for c in (snapshot.containers if snapshot else [])
        ]
        hosts_payload.append(
            {
                "alias": h.alias,
                "label": h.label,
                "main": h.main,
                "role": h.role,
                "containers": containers,
                "files": [source_payload(s) for s in host_sources(h, snapshot) if s.kind.value != "container_stdout"],
            }
        )
    return {"main": [source_payload(s) for s in main], "hosts": hosts_payload}


def _line_payload(line: LogLine) -> dict:
    return {
        "type": "line",
        "source": line.source_id,
        "label": line.label,
        "ts": line.ts_ns / 1_000_000 if line.ts_ns else None,  # epoch milliseconds for JS
        "match": line.is_match,
        "text": line.text,
        "entry": line.entry_id,
        "cont": line.is_continuation,
        "level": line.level,
        "group": line.group_id,
    }


@router.post("/search")
async def search(request: Request, body: SearchBody):
    state = _state(request)
    context = state.context
    logs_cfg = context.config.logs
    try:
        environment = context.config.environment(body.env)
        chosen = resolve_sources(state, environment.name, body.selection)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    if not chosen:
        raise HTTPException(400, "no log sources matched the selection")

    tz = body.tz or logs_cfg.display_tz
    search_request = SearchRequest(
        sources=tuple(chosen),
        allowed_paths=tuple(logs_cfg.allowed_paths),
        pattern=body.pattern,
        mode="fixed" if body.mode == "fixed" else "regex",
        ignore_case=body.ignore_case,
        before=max(0, min(body.before, 500)),
        after=max(0, min(body.after, 500)),
        window=TimeWindow(spec=body.window, tz=tz),
        tail_lines=body.tail_lines or logs_cfg.tail_lines,
        max_bytes=body.max_bytes or logs_cfg.max_bytes,
        max_line_bytes=logs_cfg.max_line_bytes,
        max_file_bytes=logs_cfg.max_file_bytes,
        timeout_s=logs_cfg.timeout_s,
        display_tz=tz,
        raw_dump=body.raw_dump,
    )
    try:
        search_request.window.resolve()
    except ValueError as exc:
        raise HTTPException(400, f"bad time window: {exc}") from exc

    bundle = None
    if body.download:
        directory = bundle_dir(context.download_dir, environment.name, body.pattern or "all")
        bundle = Bundle(directory=directory, request=search_request, env=environment.name)

    coordinator = SearchCoordinator(context.runner, context.inventory, context.config)

    async def stream() -> AsyncIterator[bytes]:
        yield (json.dumps({"type": "start", "sources": [source_payload(s) for s in chosen]}) + "\n").encode()
        summary: SearchDone | None = None
        try:
            async for event in coordinator.run(search_request):
                if isinstance(event, LogLine):
                    if bundle:
                        bundle.add(event)
                    payload = _line_payload(event)
                elif isinstance(event, SourceProgress):
                    payload = {"type": "progress", "source": event.source_id, "lines": event.lines,
                               "bytes": event.bytes, "matches": event.matches}
                elif isinstance(event, SourceDone):
                    payload = {"type": "source_done", "source": event.source_id, "label": event.label,
                               "lines": event.lines, "matches": event.matches, "bytes": event.bytes,
                               "elapsed": round(event.elapsed, 2), "truncated": event.truncated,
                               "truncated_reason": event.truncated_reason, "coverage_ok": event.coverage_ok}
                elif isinstance(event, SourceError):
                    payload = {"type": "source_error", "source": event.source_id, "label": event.label,
                               "kind": event.kind, "message": event.message}
                elif isinstance(event, SearchDone):
                    summary = event
                    continue
                else:
                    continue
                yield (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        finally:
            bundle_path = None
            if bundle and summary is not None:
                bundle_path = str(bundle.close(summary))
                context.audit.append("logs.download", env=environment.name, target=bundle_path, status="ok",
                                     note=f"web, {summary.matches} matches")
            if summary is not None:
                yield (json.dumps({
                    "type": "summary",
                    "lines": summary.lines, "matches": summary.matches, "bytes": summary.bytes,
                    "elapsed": round(summary.elapsed, 2),
                    "since": summary.window.since.isoformat(), "until": summary.window.until.isoformat(),
                    "truncated": summary.truncated_sources, "failed": summary.failed_sources,
                    "bundle": bundle_path, "bundle_name": Path(bundle_path).name if bundle_path else None,
                }) + "\n").encode()

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


@router.post("/follow")
async def follow_logs(request: Request, body: FollowBody):
    state = _state(request)
    context = state.context
    try:
        environment = context.config.environment(body.env)
        chosen = resolve_sources(state, environment.name, body.selection)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    if not chosen:
        raise HTTPException(400, "no log sources matched the selection")
    limit = context.config.logs.follow_max_sources
    chosen = chosen[:limit]

    async def stream() -> AsyncIterator[bytes]:
        yield (json.dumps({"type": "start", "sources": [source_payload(s) for s in chosen]}) + "\n").encode()
        async for event in follow(context.runner, context.inventory, chosen, pattern=body.pattern, since=body.since):
            if isinstance(event, LogLine):
                yield (json.dumps(_line_payload(event), ensure_ascii=False) + "\n").encode()
            elif isinstance(event, SourceError):
                yield (json.dumps({"type": "source_error", "source": event.source_id, "label": event.label,
                                   "kind": event.kind, "message": event.message}) + "\n").encode()

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


@router.get("/bundles")
async def bundles(request: Request):
    base = _state(request).context.download_dir
    if not base.exists():
        return []
    out = []
    for directory in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        manifest = directory / "manifest.json"
        if not directory.is_dir() or not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        size = sum(f.stat().st_size for f in directory.iterdir() if f.is_file())
        out.append({
            "name": directory.name,
            "env": data.get("env"),
            "pattern": data.get("pattern"),
            "created_at": data.get("created_at_utc"),
            "totals": data.get("totals"),
            "window": data.get("window"),
            "size": size,
            "files": sorted(f.name for f in directory.iterdir() if f.is_file()),
        })
    return out[:100]


@router.get("/bundles/{name}.zip")
async def bundle_zip(request: Request, name: str):
    base = _state(request).context.download_dir
    directory = (base / name).resolve()
    if not directory.is_dir() or base.resolve() not in directory.parents or not (directory / "manifest.json").exists():
        raise HTTPException(404, "no such bundle")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(directory.iterdir()):
            if file.is_file():
                archive.write(file, arcname=f"{name}/{file.name}")
    buffer.seek(0)
    return StreamingResponse(
        buffer, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
    )
