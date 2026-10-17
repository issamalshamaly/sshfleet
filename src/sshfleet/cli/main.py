"""``sshfleet`` entry point.

Typer wires the sub-apps; heavy imports stay inside the commands that need them so
``sshfleet fleet --json`` starts fast enough to use in a shell loop.
"""

from __future__ import annotations

import sys

import typer

from . import (
    audit_cmds,
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
from .output import Exit

app = typer.Typer(
    name="sshfleet",
    help=(
        "Fleet monitor and support tool for a fleet of servers reached over SSH.\n\n"
        "Start with `sshfleet web` for the browser interface, or `sshfleet fleet` for the same "
        "status in the terminal. `sshfleet doctor` checks the setup."
    ),
    no_args_is_help=True,
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
app.add_typer(audit_cmds.app, name="audit")


@app.command("version")
def version() -> None:
    """Print the sshfleet version."""
    from .. import __version__

    typer.echo(__version__)


def _print_version(value: bool) -> None:
    if value:
        from .. import __version__

        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False, "--version", help="Print the version and exit.", callback=_print_version, is_eager=True
    ),
) -> None:
    """Options that apply before any command."""


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        sys.exit(Exit.INTERRUPTED)


if __name__ == "__main__":  # pragma: no cover
    main()
