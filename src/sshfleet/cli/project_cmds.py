"""``sshfleet project …`` — group environments under a project.

A project is a name and nothing else: it carries no behaviour, and an environment works exactly
the same whether it is filed under one or not.
"""

from __future__ import annotations

from typing import Annotated

import typer

from .. import envsetup
from ..config import ConfigError
from ..config import loader as config_loader
from ..context import build_context
from .output import Exit, console, emit_json, fail, info, table

app = typer.Typer(no_args_is_help=True, help="Group environments under a project.")


@app.command("list")
def list_projects(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List projects and the environments filed under each."""
    context = build_context(write_ssh_config=False)
    config = context.config
    data = [
        {
            "name": project.name,
            "color": project.color,
            "description": project.description,
            "environments": [e.name for e in config.environments if e.project == project.name],
        }
        for project in config.projects
    ]
    if as_json:
        emit_json(data)
        return
    if not data:
        info("no projects yet — create one with `sshfleet project add <name>`")
        return
    console.print(
        table(
            "Projects",
            ["name", "environments", "description"],
            [[d["name"], ", ".join(d["environments"]) or "—", d["description"]] for d in data],
        )
    )


@app.command("add")
def add(
    name: Annotated[str, typer.Argument(help="Project name.")],
    color: Annotated[str | None, typer.Option("--color", help="Colour for its chip.")] = None,
    description: Annotated[str, typer.Option("--description", help="What it is.")] = "",
) -> None:
    """Create a project. Environments are added to it afterwards with `env add --project`."""
    context = build_context(write_ssh_config=False)
    try:
        project = envsetup.add_project(context.config, name=name, color=color, description=description)
    except ConfigError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc
    config_loader.save(context.config, context.paths.config_file)
    context.remember(f"project {project.name} added")
    context.audit.append("settings.project.add", target=project.name, status="ok", note="cli")
    console.print(f"[green]created[/green] project [bold]{project.name}[/bold]")


@app.command("rename")
def rename(
    name: Annotated[str, typer.Argument(help="Current name.")],
    new_name: Annotated[str, typer.Argument(help="New name.")],
) -> None:
    """Rename a project; every environment filed under it follows."""
    context = build_context(write_ssh_config=False)
    try:
        envsetup.rename_project(context.config, name, new_name)
    except ConfigError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc
    config_loader.save(context.config, context.paths.config_file)
    context.remember(f"project {name} renamed to {new_name}")
    context.audit.append("settings.project.rename", target=new_name, status="ok", note=f"cli, was {name}")
    console.print(f"[green]renamed[/green] {name} → [bold]{new_name}[/bold]")


@app.command("remove")
def remove(name: Annotated[str, typer.Argument(help="Project name.")]) -> None:
    """Forget a project. It must be empty first: an environment always belongs to one."""
    context = build_context(write_ssh_config=False)
    try:
        envsetup.remove_project(context.config, name)
    except ConfigError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc
    config_loader.save(context.config, context.paths.config_file)
    context.remember(f"project {name} removed")
    context.audit.append("settings.project.remove", target=name, status="ok", note="cli")
    console.print(f"[green]removed[/green] project [bold]{name}[/bold]")
