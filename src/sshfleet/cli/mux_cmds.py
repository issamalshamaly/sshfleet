"""``sshfleet mux …`` — inspect and manage the shared SSH master connections."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import typer

from ..context import build_context
from ..tunnels import TunnelManager
from .output import Exit, console, emit_json, fail, table

app = typer.Typer(no_args_is_help=True, help="Manage multiplexed SSH master connections.")


@app.command("warm")
def warm(
    env: Annotated[str | None, typer.Option("--env", "-e", help="Restrict to one environment.")] = None,
    host: Annotated[list[str] | None, typer.Option("--host", "-h", help="Warm these aliases instead of the bastions.")] = None,
    all_hosts: Annotated[bool, typer.Option("--all", help="Warm every host, not just the bastions.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Open the master connections so later commands are instant."""
    context = build_context()

    async def run() -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        bastion_results = await context.mux.warm_bastions(env)
        for alias, result in bastion_results.items():
            out[alias] = {"ok": result.ok, "error": result.message, "seconds": round(result.elapsed, 2), "role": "bastion"}

        targets: list[str] = list(host or [])
        if all_hosts:
            targets += [h.alias for h in context.inventory.hosts(env) if h.alias not in out]
        if targets:
            results = await asyncio.gather(*(context.mux.warm(a) for a in dict.fromkeys(targets)))
            for result in results:
                out[result.alias] = {"ok": result.ok, "error": result.message, "seconds": round(result.elapsed, 2), "role": "host"}
        return out

    results = asyncio.run(run())
    ok = all(r["ok"] for r in results.values()) if results else False

    if as_json:
        emit_json(results, ok=ok)
    else:
        if not results:
            fail("nothing to warm — register an environment first")
            raise typer.Exit(Exit.USAGE)
        rows = [[alias, r["role"], "ok" if r["ok"] else str(r["error"]), r["seconds"]] for alias, r in sorted(results.items())]
        console.print(table("Master connections", ["alias", "role", "state", "seconds"], rows))
    raise typer.Exit(Exit.OK if ok else Exit.PARTIAL)


@app.command("status")
def status(
    env: Annotated[str | None, typer.Option("--env", "-e")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show which master connections are currently alive (local check, no network)."""
    context = build_context()
    aliases = [h.alias for h in context.inventory.hosts(env)]
    alive = asyncio.run(context.mux.status(aliases)) if aliases else {}
    payload: dict[str, Any] = {
        "ssh_config": str(context.mux.ssh_config),
        "control_dir": str(context.mux.control_dir()),
        "alive": sorted(a for a, ok in alive.items() if ok),
        "total": len(aliases),
    }
    if as_json:
        emit_json(payload)
        return
    console.print(f"ssh config : {payload['ssh_config']}")
    console.print(f"control dir: {payload['control_dir']}")
    console.print(f"alive      : {len(payload['alive'])}/{payload['total']}")
    for alias in payload["alive"]:
        console.print(f"  [green]●[/green] {alias}")


@app.command("reset")
def reset() -> None:
    """Delete stale control sockets (use after a suspend/resume or a network change)."""
    context = build_context()
    removed = context.mux.reset_all()
    context.audit.append("mux.reset", status="ok", note=f"{removed} sockets")
    console.print(f"removed {removed} control socket(s) from {context.mux.control_dir()}")


@app.command("stop")
def stop(
    env: Annotated[str | None, typer.Option("--env", "-e")] = None,
) -> None:
    """Close the master connections (this also drops any port forwards riding on them)."""
    context = build_context()

    tunnels = TunnelManager(context.config, context.mux, context.paths.tunnels_file)

    async def run() -> tuple[list[str], list[str]]:
        aliases = [h.alias for h in context.inventory.hosts(env)]
        await context.mux.status(aliases)
        # Closing on purpose: the tunnels go too, and must not look dropped to the web
        # server's watcher, which would open them again straight away.
        forgotten = tunnels.forget_via(set(context.mux.alive))
        return await context.mux.stop_all(), forgotten

    stopped, forgotten = asyncio.run(run())
    context.audit.append("mux.stop", env=env, status="ok", note=f"{len(stopped)} closed")
    console.print(f"closed {len(stopped)} master connection(s)" + (f": {', '.join(stopped)}" if stopped else ""))
    if forgotten:
        console.print(f"closed the tunnels riding on them: {', '.join(forgotten)}")
