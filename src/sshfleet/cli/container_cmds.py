"""``sshfleet containers`` and ``sshfleet restart``."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.text import Text

from ..containers import Phase, RestartOutcome, VerifyStep, restart_and_verify, tail_logs
from ..context import build_context
from ..fleet import FleetService
from ..inventory import Host
from .output import Exit, console, emit_json, err_console, fail, table


def _resolve_container(context, alias: str, name: str) -> tuple[Host, int | None, str | None]:
    """The host, and the endpoint to call after a restart — the host's, when it watches this one."""
    host = context.inventory.host(alias)
    if not host.watches(name):
        return host, None, None
    return host, host.health_port, host.health_path


def containers(
    alias: Annotated[str, typer.Argument(help="Host alias.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List the containers on one host."""
    context = build_context()
    try:
        context.inventory.host(alias)
    except KeyError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    fleet = FleetService(context)
    snapshot = asyncio.run(fleet.refresh_host(alias))

    if as_json:
        emit_json(
            {
                "host": alias,
                "runtime": snapshot.runtime,
                "containers": snapshot.to_dict()["containers"],
                "gone": snapshot.vanished_containers,
            },
            ok=snapshot.reachable,
        )
        raise typer.Exit(Exit.OK if snapshot.reachable else Exit.FAILED)

    if not snapshot.reachable:
        fail(f"{alias}: {snapshot.error_message}")
        raise typer.Exit(Exit.FAILED)

    rows = [
        [
            c.name,
            c.image.split("/")[-1][:36],
            Text(c.state, style="green" if c.running else "red"),
            c.status,
            c.ports or ("host-net" if c.host_network else ""),
            str(c.restart_count or ""),
        ]
        for c in snapshot.containers
    ]
    for name in snapshot.vanished_containers:
        rows.append([name, "—", Text("gone", style="red"), "was on this host, no longer listed", "", ""])
    console.print(table(f"{alias} ({snapshot.runtime})", ["name", "image", "state", "status", "ports", "restarts"], rows))


def restart(
    alias: Annotated[str, typer.Argument(help="Host alias.")],
    name: Annotated[str, typer.Argument(help="Container name, e.g. order-production.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    verify_timeout: Annotated[float, typer.Option("--verify-timeout", help="Seconds to wait for it to come back.")] = 90.0,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Restart one container in place and wait until it is demonstrably back."""
    context = build_context()
    try:
        host, port, path = _resolve_container(context, alias, name)
    except KeyError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    from ..containers.runtime import ForbiddenCommand, restart_command

    try:
        command = restart_command(host, name)
    except ForbiddenCommand as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc
    environment = context.config.environment(host.env)
    is_production = "prod" in environment.name.lower()

    if not yes:
        env_label = Text(environment.name.upper(), style=f"bold {environment.color}")
        console.print(Text.assemble("Environment  ", env_label))
        console.print(f"Host         {host.alias}  ({host.hostname or ''}" + (f" via {host.proxy_jump}" if host.proxy_jump else "") + ")")
        console.print(f"Container    {name}")
        console.print(f"Command      {command.preview(str(context.mux.ssh_config))}")
        console.print("[dim]Restarts in place. sshfleet never stops, removes or prunes a container.[/dim]")
        if is_production:
            typed = typer.prompt(f"Type the container name to confirm ({name})")
            if typed.strip() != name:
                fail("name did not match; nothing was done")
                raise typer.Exit(Exit.USAGE)
        else:
            typer.confirm("Restart now?", abort=True)

    context.audit.append("container.restart", env=host.env, host=host.alias, target=name, command=command.render(), status="started")

    outcome: RestartOutcome | None = None
    steps: list[VerifyStep] = []

    async def run() -> None:
        nonlocal outcome
        async for event in restart_and_verify(
            context.runner, host, name, port=port, path=path, verify_timeout=verify_timeout
        ):
            if isinstance(event, RestartOutcome):
                outcome = event
            else:
                steps.append(event)
                if not as_json:
                    style = {Phase.VERIFIED: "green", Phase.FAILED: "red"}.get(event.phase, "dim")
                    err_console.print(f"[{style}]{event.phase.value}[/{style}] {event.message} ({event.elapsed:.0f}s)")

    asyncio.run(run())
    assert outcome is not None

    context.audit.append(
        "container.restart",
        env=host.env,
        host=host.alias,
        target=name,
        command=outcome.command,
        status="ok" if outcome.ok else "failed",
        duration_ms=int(outcome.elapsed * 1000),
        verify={"phase": outcome.phase.value, "state": outcome.state, "probe": outcome.probe_code},
    )

    if not outcome.ok:
        logs = asyncio.run(tail_logs(context.runner, host, name, 40))
        if as_json:
            emit_json(outcome.to_dict() | {"logs": logs}, ok=False)
        else:
            fail(outcome.message)
            if logs.strip():
                console.print("[dim]last log lines:[/dim]")
                console.print(logs[-4000:])
        raise typer.Exit(Exit.FAILED)

    if as_json:
        emit_json(outcome.to_dict())
    else:
        console.print(f"[green]restarted[/green] {name} on {host.alias}: {outcome.message}")
