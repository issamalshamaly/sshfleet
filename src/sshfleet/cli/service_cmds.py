"""``sshfleet service`` — the web interface as a background service of your login session."""

from __future__ import annotations

import shlex
import subprocess
from typing import Annotated

import typer

from .. import service as service_module
from ..service import DEFAULT_PORT, ServiceError, Status, Supervisor
from .output import Exit, console, emit_json, fail, warn

app = typer.Typer(
    help="Keep the web interface running in the background: a systemd user service on Linux, a launch agent on macOS.",
    no_args_is_help=True,
)

STATE_STYLE = {"running": "green", "starting": "yellow", "stopped": "yellow", "failed": "red", "not installed": "dim"}


def _supervisor() -> Supervisor:
    try:
        return service_module.supervisor()
    except ServiceError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.FAILED) from exc


def _row(label: str, value: str) -> None:
    console.print(f"[bold]{label:<16}[/bold]{value}")


def _state(status: Status) -> str:
    return f"[{STATE_STYLE.get(status.state, 'white')}]{status.state}[/{STATE_STYLE.get(status.state, 'white')}]"


def _status_json(manager: Supervisor, status: Status) -> dict[str, object]:
    return {
        "manager": manager.kind,
        "installed": status.installed,
        "path": str(status.path),
        "enabled": status.enabled,
        "active": status.active,
        "state": status.state,
        "pid": status.pid,
        "since": status.since,
        "restarts": status.restarts,
        "port": status.port,
        "url": status.url,
        "detail": status.detail,
        "logs": manager.logs_command(lines=100, follow=True) if status.installed else None,
    }


def _address(url: str) -> str:
    """The address, and how to get a browser in: the page is only served to one let in by a
    launch link, which ``sshfleet web`` hands out while the service runs."""
    port = url.rsplit(":", 1)[-1].rstrip("/")
    command = "sshfleet web" if port == str(DEFAULT_PORT) else f"sshfleet web --port {port}"
    return f"{url} [dim]· open it with[/dim] [bold]{command}[/bold]"


def _print_status(manager: Supervisor, status: Status) -> None:
    _row("service", _state(status) + (f" · {status.detail}" if status.detail else ""))
    if not status.installed:
        _row("definition", "none — run [bold]sshfleet service install[/bold]")
        return
    _row("definition", str(status.path))
    _row("starts at login", "yes" if status.enabled else "no")
    if status.pid:
        _row("process", f"pid {status.pid}" + (f", since {status.since}" if status.since else ""))
    if status.restarts:
        _row("restarts", str(status.restarts))
    if status.url:
        _row("address", _address(status.url))
    _row("output", manager.logs_hint())


def install(
    port: Annotated[int, typer.Option("--port", "-p", help="Local port the service listens on.")] = DEFAULT_PORT,
    at_boot: Annotated[
        bool, typer.Option("--at-boot", help="Linux: keep it running while you are logged out (loginctl enable-linger).")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the definition and where it would go, without writing or starting anything.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Register the web interface as a service that starts when you log in, and start it now."""
    manager = _supervisor()
    try:
        plan = manager.plan(port) if dry_run else manager.install(port)
        warnings = list(plan.warnings)
        if at_boot and not dry_run:
            if isinstance(manager, service_module.Systemd):
                manager.linger()
            else:
                warnings.append("--at-boot applies to Linux only; a launch agent runs while you are logged in")
    except ServiceError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.FAILED) from exc

    if as_json:
        emit_json(
            {
                "manager": manager.kind,
                "path": str(plan.path),
                "written": not dry_run,
                "started": plan.started,
                "command": plan.command,
                "environment": plan.environment,
                "port": plan.port,
                "url": plan.url,
                "logs": manager.logs_command(lines=100, follow=True),
                "note": plan.note or None,
                "warnings": warnings,
                "definition": plan.content,
            }
        )
        return

    if dry_run:
        _row("definition", f"{plan.path}  [dim](not written: --dry-run)[/dim]")
        console.print()
        console.print(f"[dim]{plan.content.rstrip()}[/dim]")
        console.print()
    else:
        _row("definition", str(plan.path))
    _row("runs", shlex.join(plan.command))
    if dry_run:
        _row("starts", "at login, and right away" if not plan.note else "at login")
    elif plan.started:
        _row("starts", "at login — and it is running now" + (", and while you are logged out" if at_boot else ""))
    else:
        _row("starts", "at login")
    _row("address", _address(plan.url))
    _row("output", manager.logs_hint())
    if plan.note:
        console.print(f"[yellow]note[/yellow] {plan.note}")
    for message in warnings:
        warn(message)


def uninstall(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Stop the service and remove its definition. Nothing else is touched."""
    manager = _supervisor()
    try:
        removed = manager.uninstall()
    except ServiceError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.FAILED) from exc
    if as_json:
        emit_json({"manager": manager.kind, "removed": removed, "path": str(manager.unit_path)})
    elif removed:
        console.print(f"removed {manager.unit_path}; the service is stopped and will not start again")
    else:
        console.print("nothing to remove: no service is installed")
    raise typer.Exit(Exit.OK if removed else Exit.NO_RESULTS)


def status(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Is the service installed, is it running, and where."""
    manager = _supervisor()
    try:
        current = manager.status()
    except ServiceError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.FAILED) from exc
    if as_json:
        emit_json(_status_json(manager, current), ok=current.installed)
    else:
        _print_status(manager, current)
    if current.active:
        raise typer.Exit(Exit.OK)
    raise typer.Exit(Exit.NO_RESULTS if not current.installed else Exit.PARTIAL)


def _drive(verb: str) -> None:
    manager = _supervisor()
    if not manager.installed:
        fail("no service is installed — run `sshfleet service install` first")
        raise typer.Exit(Exit.NO_RESULTS)
    try:
        getattr(manager, verb)()
        current = manager.status()
    except ServiceError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.FAILED) from exc
    _print_status(manager, current)
    raise typer.Exit(Exit.OK if (verb == "stop") == (not current.active) else Exit.PARTIAL)


def start() -> None:
    """Start the installed service now."""
    _drive("start")


def stop() -> None:
    """Stop the service; it starts again at the next login unless you uninstall it."""
    _drive("stop")


def restart() -> None:
    """Stop and start the service — after upgrading sshfleet, for instance."""
    _drive("restart")


def logs(
    lines: Annotated[int, typer.Option("--lines", "-n", help="How many of the latest lines to show.")] = 100,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep showing new output until Ctrl+C.")] = False,
) -> None:
    """Show the service's output: the journal on Linux, its log file on macOS."""
    manager = _supervisor()
    if not manager.installed:
        fail("no service is installed — run `sshfleet service install` first")
        raise typer.Exit(Exit.NO_RESULTS)
    command = manager.logs_command(lines=lines, follow=follow)
    try:
        code = subprocess.call(command)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        code = 0
    except OSError as exc:
        fail(f"could not run {command[0]}: {exc.strerror or exc}")
        raise typer.Exit(Exit.FAILED) from exc
    raise typer.Exit(code)


app.command("install")(install)
app.command("uninstall")(uninstall)
app.command("status")(status)
app.command("start")(start)
app.command("stop")(stop)
app.command("restart")(restart)
app.command("logs")(logs)
