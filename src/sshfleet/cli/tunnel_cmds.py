"""``sshfleet tunnel …`` — port forwards to Kibana, Seq, Elasticsearch, databases."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import Annotated

import typer

from ..context import build_context
from ..tunnels import TunnelManager
from .output import Exit, console, emit_json, fail, table

app = typer.Typer(no_args_is_help=True, help="Manage SSH port forwards.")


def _manager(context) -> TunnelManager:
    return TunnelManager(context.config, context.mux, context.paths.tunnels_file)


@app.command("up")
def up(
    name: Annotated[str, typer.Argument(help="Tunnel name from config.yaml.")],
    local: Annotated[int | None, typer.Option("--local", "-l", help="Override the local port.")] = None,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the URL afterwards.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Open a port forward on the existing master connection."""
    context = build_context()
    manager = _manager(context)
    try:
        state = asyncio.run(manager.up(name, local=local))
    except KeyError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    context.audit.append("tunnel.up", env=state.env, host=state.via, target=name, status="ok" if state.up else "failed", note=state.endpoint)

    if as_json:
        emit_json(state.to_dict(), ok=state.up)
    elif state.up:
        console.print(f"[green]up[/green] {name}: {state.endpoint} -> {state.remote} via {state.via}")
        if state.open_url:
            console.print(f"     {state.open_url}")
    else:
        fail(f"{name}: {state.error}")

    if state.up and open_browser and state.open_url and shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", state.open_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    raise typer.Exit(Exit.OK if state.up else Exit.FAILED)


@app.command("down")
def down(
    name: Annotated[str, typer.Argument(help="Tunnel name.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Close a port forward (the connection itself stays open)."""
    context = build_context()
    manager = _manager(context)
    try:
        state = asyncio.run(manager.down(name))
    except KeyError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc
    context.audit.append(
        "tunnel.down", env=state.env, host=state.via, target=name,
        status="failed" if state.held else "ok", note=state.error or None,
    )
    if as_json:
        emit_json(state.to_dict(), ok=not state.held)
    elif state.held:
        fail(f"{name}: {state.error}")
    else:
        console.print(f"closed {name}")
    raise typer.Exit(Exit.FAILED if state.held else Exit.OK)


def _state_word(state) -> str:
    if state.up:
        return "up"
    if state.dropped:
        return "dropped"  # opened and never closed, but its connection went
    return "port busy" if state.held else "down"


@app.command("status")
def status(
    env: Annotated[str | None, typer.Option("--env", "-e")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show configured tunnels and which are currently open."""
    context = build_context()
    states = _manager(context).status(env)
    if as_json:
        emit_json([s.to_dict() for s in states])
        return
    if not states:
        console.print("no tunnels configured — add them under `tunnels:` in config.yaml")
        return
    rows = [
        [s.name, s.env, _state_word(s), s.endpoint, s.remote, s.via, s.open_url or ""]
        for s in states
    ]
    console.print(table("Tunnels", ["name", "env", "state", "local", "remote", "via", "url"], rows))
