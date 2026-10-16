"""``sshfleet`` entry point.

Typer wires the sub-apps; heavy imports (Textual) stay inside the commands that need them so
``sshfleet fleet --json`` starts fast enough to use in a shell loop.
"""

from __future__ import annotations

import sys

import typer

from . import (
    config_cmds,
    container_cmds,
    env_cmds,
    files_cmds,
    fleet_cmds,
    logs_cmds,
    misc_cmds,
    mux_cmds,
    project_cmds,
    service_cmds,
    tunnel_cmds,
)
from .output import Exit, fail

app = typer.Typer(
    name="sshfleet",
    help="Fleet monitor and support tool for a fleet of servers reached over SSH.",
    no_args_is_help=False,
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

app.add_typer(project_cmds.app, name="project")
app.add_typer(env_cmds.app, name="env")
app.add_typer(config_cmds.app, name="config")
app.add_typer(mux_cmds.app, name="mux")
app.command("fleet")(fleet_cmds.fleet)
app.command("host")(fleet_cmds.host)
app.command("containers")(container_cmds.containers)
app.command("restart")(container_cmds.restart)
app.add_typer(logs_cmds.app, name="logs")
app.add_typer(files_cmds.app, name="files")
app.add_typer(tunnel_cmds.app, name="tunnel")
app.command("web")(misc_cmds.web)
app.command("desktop")(misc_cmds.desktop)
app.add_typer(service_cmds.app, name="service")
app.command("ssh")(misc_cmds.ssh)
app.command("doctor")(misc_cmds.doctor)
app.command("hosts")(misc_cmds.hosts)
app.command("audit")(misc_cmds.audit)


@app.command("version")
def version() -> None:
    """Print the sshfleet version."""
    from .. import __version__

    typer.echo(__version__)


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    """Run with no arguments to open the terminal UI."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        from ..tui.app import run_tui
    except ImportError as exc:  # pragma: no cover - textual missing
        fail(f"the terminal UI is unavailable: {exc}")
        raise typer.Exit(Exit.FAILED) from exc
    run_tui()


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        sys.exit(Exit.INTERRUPTED)


if __name__ == "__main__":  # pragma: no cover
    main()
