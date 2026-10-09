"""``sshfleet doctor``, ``hosts`` and ``audit`` — small commands with no subgroup."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from .. import doctor as doctor_module
from ..context import build_context
from .output import Exit, console, emit_json, table

MARKS = {"ok": "[green]ok[/green]", "warn": "[yellow]warn[/yellow]", "fail": "[red]fail[/red]"}


def doctor(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check the local setup: binaries, permissions, config, key modes, inventory."""
    context = build_context(write_ssh_config=False)
    checks = doctor_module.run_checks(context)
    level = doctor_module.worst(checks)

    if as_json:
        emit_json([{"name": c.name, "level": c.level, "detail": c.detail, "fix": c.fix} for c in checks], ok=level != "fail")
    else:
        for check in checks:
            line = f"{MARKS[check.level]:<18} {check.name:<24} {check.detail}"
            console.print(line)
            if check.fix:
                console.print(f"{'':<18} {'':<24} [dim]fix: {check.fix}[/dim]")
    raise typer.Exit(Exit.OK if level != "fail" else Exit.FAILED)


def hosts(
    env: Annotated[str | None, typer.Option("--env", "-e", help="Restrict to one environment.")] = None,
    service: Annotated[str | None, typer.Option("--service", "-s", help="Restrict to one service.")] = None,
    role: Annotated[str | None, typer.Option("--role", help="Restrict to one role (service, gateway, frontend, data, ops).")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List the hosts sshfleet derived from the SSH config files."""
    context = build_context(write_ssh_config=False)
    selected = context.inventory.hosts(env)
    if service:
        selected = [h for h in selected if h.log_label == service or h.alias == service]
    if role:
        selected = [h for h in selected if h.role == role]

    data = [
        {
            "alias": h.alias,
            "env": h.env,
            "hostname": h.hostname,
            "user": h.user,
            "via": h.proxy_jump,
            "bastion": h.is_bastion,
            "label": h.log_label,
            "role": h.role,
            "runtime": h.runtime,
            "sudo": h.sudo,
            "main": h.main,
        }
        for h in selected
    ]

    if as_json:
        emit_json(data)
        raise typer.Exit(Exit.OK if data else Exit.NO_RESULTS)

    rows = [
        [
            d["alias"],
            d["env"],
            d["hostname"] or "",
            d["user"] or "",
            d["via"] or ("(bastion)" if d["bastion"] else ""),
            d["service"] or "—",
            d["role"],
            "yes" if d["main"] else "",
        ]
        for d in data
    ]
    console.print(table("Hosts", ["alias", "env", "address", "user", "via", "service", "role", "expected containers"], rows))
    raise typer.Exit(Exit.OK if data else Exit.NO_RESULTS)


def ssh(
    alias: Annotated[str, typer.Argument(help="Host alias.")],
    command: Annotated[str | None, typer.Argument(help="Optional command to run instead of a shell.")] = None,
    tab: Annotated[bool, typer.Option("--tab", "-t", help="Open in a new terminal tab or tmux window.")] = False,
    print_only: Annotated[bool, typer.Option("--print", help="Print the ssh command instead of running it.")] = False,
) -> None:
    """Open an interactive shell on a host, reusing the already-open connection."""
    from .. import shell as shell_module

    context = build_context()
    try:
        host = context.inventory.host(alias)
    except KeyError as exc:
        from .output import fail

        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc
    paused = context.inventory.paused_reason(alias)
    if paused is not None and not print_only:
        from .output import fail

        fail(f"{alias}: {paused} (resume it in Settings › Hosts)")
        raise typer.Exit(Exit.USAGE)

    mode = "print" if print_only else shell_module.detect_mode(
        context.config.ui.get("open_ssh", "auto"), new_window=tab
    )
    if not print_only:
        # Warming first means the prompt appears immediately instead of after a handshake.
        import asyncio

        asyncio.run(context.mux.warm(alias))
        context.audit.append("ssh.open", env=host.env, host=alias, status="ok", note=mode)
    code = shell_module.open_shell(context.mux.ssh_config, alias, mode=mode, remote=command)
    raise typer.Exit(code)


def web(
    port: Annotated[int, typer.Option("--port", "-p", help="Local port to listen on.")] = 8765,
    no_open: Annotated[bool, typer.Option("--no-open", help="Do not open the browser automatically.")] = False,
) -> None:
    """Start the web interface on localhost and open it in your browser."""
    from ..web import run_web

    run_web(port=port, open_browser=not no_open)


def desktop(
    port: Annotated[int, typer.Option("--port", "-p", help="Local port for the embedded server.")] = 8765,
) -> None:
    """Open the web interface in its own desktop window."""
    from ..web import run_desktop

    run_desktop(port=port)


def audit(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many entries to show.")] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the local audit trail of mutating actions."""
    context = build_context(write_ssh_config=False)
    entries = context.audit.tail(limit)
    if as_json:
        emit_json(entries)
        return
    if not entries:
        console.print("no audit entries yet")
        return
    rows = [
        [
            e.get("ts", ""),
            e.get("action", ""),
            e.get("env", ""),
            e.get("host", ""),
            e.get("target", ""),
            e.get("status", ""),
            json.dumps(e.get("verify")) if e.get("verify") else "",
        ]
        for e in entries
    ]
    console.print(table(f"Audit (last {len(entries)})", ["time", "action", "env", "host", "target", "status", "verify"], rows))
