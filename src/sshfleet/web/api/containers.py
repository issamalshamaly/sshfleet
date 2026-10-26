"""Container restart as a job the browser can watch."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...config import loader as config_loader
from ...config.schema import HostOverride
from ...containers import RestartOutcome, remove, restart_and_verify, tail_logs
from ...containers.names import check_container_name
from ...containers.quick import offers
from ...containers.runtime import ForbiddenCommand, restart_command
from ...inventory import ignore_glob_for
from ..security import require_token
from ..state import WebState

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def _state(request: Request) -> WebState:
    return request.app.state.web


class RestartBody(BaseModel):
    confirm: str = ""


@router.get("/hosts/{alias}/containers/{name}/preview")
async def preview(request: Request, alias: str, name: str):
    """What a restart would run, so the confirmation can show it verbatim."""
    state = _state(request)
    try:
        host = state.context.inventory.host(alias)
        command = restart_command(host, name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ForbiddenCommand as exc:
        raise HTTPException(400, str(exc)) from exc
    environment = state.context.config.environment(host.env)
    snapshot = state.fleet.snapshot(alias)
    current = snapshot.container(name) if snapshot else None
    return {
        "host": alias,
        "container": name,
        "env": environment.name,
        "production": environment.is_production,
        "command": command.render(),
        "preview": command.preview(str(state.context.mux.ssh_config)),
        "current": {"state": current.state, "status": current.status, "restarts": current.restart_count} if current else None,
    }


@router.get("/hosts/{alias}/containers/{name}/actions")
async def quick_actions(request: Request, alias: str, name: str):
    """The quick actions worth offering for this container, each with the line it would run.

    Which ones come back depends on what has been discovered: the image, the container's
    state, the host's role and whether a health endpoint is configured. The browser shows this
    list and names one back — it never composes a command of its own.
    """
    state = _state(request)
    try:
        host = state.context.inventory.host(alias)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    snapshot = state.fleet.snapshot(alias)
    current = snapshot.container(name) if snapshot else None
    return {
        "host": alias,
        "container": name,
        "actions": offers(
            host,
            name,
            image=current.image if current else "",
            state=current.state if current else "running",
        ),
    }


@router.post("/hosts/{alias}/containers/{name}/restart")
async def restart(request: Request, alias: str, name: str, body: RestartBody):
    state = _state(request)
    context = state.context
    try:
        host = context.inventory.host(alias)
        command = restart_command(host, name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ForbiddenCommand as exc:
        raise HTTPException(400, str(exc)) from exc

    environment = context.config.environment(host.env)
    if environment.is_production and body.confirm.strip() != name:
        raise HTTPException(400, "production restarts require the container name to be typed")

    job = state.new_job("restart", f"{alias}/{name}")
    watched = host.watches(name)
    port = host.health_port if watched else None
    path = host.health_path if watched else None

    context.audit.append(
        "container.restart", env=host.env, host=alias, target=name, command=command.render(), status="started",
        note=f"web job {job.id}",
    )

    async def run() -> None:
        outcome: RestartOutcome | None = None
        try:
            async for event in restart_and_verify(context.runner, host, name, port=port, path=path):
                if isinstance(event, RestartOutcome):
                    outcome = event
                else:
                    job.steps.append(
                        {
                            "phase": event.phase.value,
                            "message": event.message,
                            "elapsed": round(event.elapsed, 1),
                            "state": event.state,
                            "probe": event.probe_code,
                        }
                    )
            if outcome is not None:
                result = outcome.to_dict()
                if not outcome.ok:
                    result["logs"] = (await tail_logs(context.runner, host, name, 40))[-6000:]
                job.result = result
                context.audit.append(
                    "container.restart", env=host.env, host=alias, target=name, command=outcome.command,
                    status="ok" if outcome.ok else "failed", duration_ms=int(outcome.elapsed * 1000),
                    verify={"phase": outcome.phase.value, "state": outcome.state, "probe": outcome.probe_code},
                )
        except Exception as exc:  # pragma: no cover - surfaced to the browser
            job.result = {"ok": False, "message": str(exc), "phase": "failed"}
        finally:
            job.done = True
            state.events.publish({"type": "job_done", "job": job.id, "ok": bool(job.result and job.result.get("ok"))})
            state.spawn(state.refresh(alias=alias))

    state.spawn(run())
    return {"job": job.id}


def _ignore(state: WebState, alias: str, pattern: str) -> list[str]:
    """Record that a container is not this host's business, so it never raises an alert again."""
    config = state.context.config
    override = config.hosts.get(alias) or HostOverride()
    if pattern not in override.ignored_containers:
        override.ignored_containers.append(pattern)
    config.hosts[alias] = override
    config_loader.save(config, state.context.paths.config_file)
    return list(override.ignored_containers)


@router.delete("/hosts/{alias}/containers/{name}")
async def remove_container(request: Request, alias: str, name: str, confirm: str = "", save_logs: str = "1"):
    """Remove a container that is not running. Its last log lines are saved first unless told not to.

    The name must be typed back on every environment: this is the one action that leaves no
    trace on the server, so the confirmation is the same everywhere.
    """
    state = _state(request)
    context = state.context
    try:
        host = context.inventory.host(alias)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        check_container_name(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if confirm.strip() != name:
        raise HTTPException(400, "type the container name to confirm removing it")
    snapshot = state.fleet.snapshot(alias)
    current = snapshot.container(name) if snapshot else None
    if current is not None and current.running:
        raise HTTPException(409, f"{name} is running; only a stopped container can be removed")

    destination = None
    if save_logs not in {"0", "false", "no"}:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = context.download_dir / host.env / alias / f"{name}-{stamp}.log"

    context.audit.append("container.remove", env=host.env, host=alias, target=name, status="started")
    outcome = await remove(context.runner, host, name, save_logs_to=destination)
    context.audit.append(
        "container.remove",
        env=host.env,
        host=alias,
        target=name,
        command=outcome.command or None,
        status="ok" if outcome.ok else "failed",
        duration_ms=int(outcome.elapsed * 1000),
        note=f"was {outcome.state}" + (f"; log saved to {outcome.saved_log}" if outcome.saved_log else ""),
    )
    if not outcome.ok:
        raise HTTPException(409 if outcome.state not in {"missing"} else 404, outcome.message)

    ignored = _ignore(state, alias, name)
    # Gone on purpose, so not an alert: said in the response and kept in the trail, where it can
    # be found and undone like an ignore made by hand.
    context.audit.append("container.ignore", env=host.env, host=alias, target=name, status="ok", note="after removing it")
    await state.reload(reason=f"{name} removed from {alias}")
    state.spawn(state.refresh(alias=alias))
    return {**outcome.to_dict(), "ignored": ignored}


@router.post("/hosts/{alias}/containers/{name}/ignore")
async def ignore_container(request: Request, alias: str, name: str, scope: str = "name"):
    """Stop alerting on a container: this one, or every one named like it.

    ``scope=family`` is for the containers nobody named. A scheduler that runs a task per sync —
    Airbyte is the usual one — invents a name each time, so ignoring the name that is on screen
    silences one run and none of the thousand after it. The saved glob covers them all.
    """
    state = _state(request)
    try:
        host = state.context.inventory.host(alias)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if scope not in {"name", "family"}:
        raise HTTPException(400, "scope must be 'name' or 'family'")
    pattern = ignore_glob_for(name) if scope == "family" else name
    ignored = _ignore(state, alias, pattern)
    state.context.audit.append("container.ignore", env=host.env, host=alias, target=pattern, status="ok")
    await state.reload(reason=f"{pattern} ignored on {alias}")
    state.spawn(state.refresh(alias=alias))
    return {"ok": True, "pattern": pattern, "ignored": ignored}


@router.get("/jobs/{job_id}/stream")
async def job_stream(request: Request, job_id: str):
    state = _state(request)
    job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")

    async def stream() -> AsyncGenerator[bytes, None]:
        sent = 0
        while True:
            while sent < len(job.steps):
                yield (json.dumps({"type": "step", **job.steps[sent]}) + "\n").encode()
                sent += 1
            if job.done:
                yield (json.dumps({"type": "result", **(job.result or {})}) + "\n").encode()
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(state.until_shutdown(stream()), media_type="application/x-ndjson")


@router.get("/jobs/{job_id}")
async def job_status(request: Request, job_id: str):
    job = _state(request).jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return {"id": job.id, "kind": job.kind, "target": job.target, "done": job.done, "steps": job.steps, "result": job.result}
