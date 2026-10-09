"""``sshfleet files …`` — browse, download and upload."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.text import Text

from ..context import build_context
from ..files import DeniedPath, download, download_dir_for, list_dir, preview, upload
from .output import Exit, console, emit_json, fail, table

app = typer.Typer(no_args_is_help=True, help="Browse and transfer files on the servers.")


def _fmt_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.0f}T"


@app.command("ls")
def ls(
    alias: Annotated[str, typer.Argument(help="Host alias.")],
    path: Annotated[str, typer.Argument(help="Remote directory.")] = ".",
    hidden: Annotated[bool, typer.Option("--hidden", "-a", help="Include dot files.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List a remote directory."""
    context = build_context()
    try:
        host = context.inventory.host(alias)
    except KeyError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    target = path if path != "." else f"/home/{host.user or 'ubuntu'}"
    try:
        listing = asyncio.run(list_dir(context.runner, host, target, deny_extra=context.config.files.deny_extra))
    except (OSError, NotADirectoryError) as exc:
        fail(str(exc))
        raise typer.Exit(Exit.FAILED) from exc

    entries = [e for e in listing.entries if hidden or not e.name.startswith(".")]
    if as_json:
        emit_json(
            {
                "host": alias,
                "path": listing.path,
                "used_sudo": listing.used_sudo,
                "entries": [
                    {
                        "name": e.name,
                        "kind": e.kind,
                        "size": e.size,
                        "mtime": e.mtime,
                        "mode": e.mode,
                        "owner": e.owner,
                        "group": e.group,
                        "denied": e.denied_reason,
                    }
                    for e in entries
                ],
            }
        )
        return

    rows = []
    for entry in entries:
        name = Text(entry.name + ("/" if entry.is_dir else ""))
        if entry.denied:
            name = Text(entry.name + "  🔒", style="dim red")
        rows.append(
            [
                name,
                entry.mode,
                f"{entry.owner}:{entry.group}",
                "" if entry.is_dir else _fmt_size(entry.size),
                datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M"),
                entry.denied_reason or "",
            ]
        )
    console.print(
        table(
            f"{alias}:{listing.path}" + (" (sudo)" if listing.used_sudo else ""),
            ["name", "mode", "owner", "size", "modified", "note"],
            rows,
            wrap={"note"},
        )
    )


@app.command("get")
def get(
    alias: Annotated[str, typer.Argument(help="Host alias.")],
    remote_path: Annotated[str, typer.Argument(help="Remote file to download.")],
    destination: Annotated[Path | None, typer.Option("--to", "-o", help="Local destination (default: the downloads folder).")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Download one file from a host."""
    context = build_context()
    try:
        host = context.inventory.host(alias)
    except KeyError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    target = destination or (download_dir_for(context.download_dir, host) / Path(remote_path).name)

    async def run():
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.fields[rate]}"),
            TimeElapsedColumn(),
            console=console,
            disable=as_json,
        ) as progress:
            task = progress.add_task(f"{alias}:{remote_path}", total=100, rate="")
            return await download(
                context.runner,
                host,
                remote_path,
                target,
                deny_extra=context.config.files.deny_extra,
                on_progress=lambda p: progress.update(task, completed=p.percent, rate=p.rate),
            )

    try:
        result = asyncio.run(run())
    except DeniedPath as exc:
        fail(f"refused: {exc}")
        context.audit.append("file.download", env=host.env, host=alias, target=remote_path, status="denied", note=exc.reason)
        raise typer.Exit(Exit.USAGE) from exc

    context.audit.append(
        "file.download",
        env=host.env,
        host=alias,
        target=remote_path,
        status="ok" if result.ok else "failed",
        note=f"{result.method}, {result.bytes} bytes",
    )
    if as_json:
        emit_json(result.to_dict(), ok=result.ok)
    elif result.ok:
        console.print(f"[green]downloaded[/green] {_fmt_size(result.bytes)} to {result.destination} ({result.method})")
    else:
        fail(result.message)
    raise typer.Exit(Exit.OK if result.ok else Exit.FAILED)


@app.command("put")
def put(
    alias: Annotated[str, typer.Argument(help="Host alias.")],
    local_path: Annotated[Path, typer.Argument(help="Local file to upload.")],
    remote_path: Annotated[str, typer.Argument(help="Remote destination path.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Upload one file to a host (writes to the server, so it asks first)."""
    context = build_context()
    try:
        host = context.inventory.host(alias)
    except KeyError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    environment = context.config.environment(host.env)
    if not yes:
        console.print(Text.assemble("Environment  ", Text(environment.name.upper(), style=f"bold {environment.color}")))
        console.print(f"Upload       {local_path}  ->  {alias}:{remote_path}")
        typer.confirm("Continue?", abort=True)

    async def run():
        return await upload(
            context.runner, host, local_path, remote_path, deny_extra=context.config.files.deny_extra
        )

    try:
        result = asyncio.run(run())
    except DeniedPath as exc:
        fail(f"refused: {exc}")
        raise typer.Exit(Exit.USAGE) from exc
    except FileNotFoundError as exc:
        fail(f"no such local file: {exc}")
        raise typer.Exit(Exit.USAGE) from exc

    context.audit.append(
        "file.upload",
        env=host.env,
        host=alias,
        target=remote_path,
        status="ok" if result.ok else "failed",
        note=f"{result.method}, {result.bytes} bytes",
    )
    if as_json:
        emit_json(result.to_dict(), ok=result.ok)
    elif result.ok:
        console.print(f"[green]uploaded[/green] {_fmt_size(result.bytes)} to {result.destination} ({result.method})")
    else:
        fail(result.message)
    raise typer.Exit(Exit.OK if result.ok else Exit.FAILED)


@app.command("cat")
def cat(
    alias: Annotated[str, typer.Argument(help="Host alias.")],
    remote_path: Annotated[str, typer.Argument(help="Remote file to preview.")],
    limit: Annotated[int, typer.Option("--bytes", "-n", help="How many bytes to show.")] = 65536,
) -> None:
    """Print the first bytes of a remote text file."""
    context = build_context()
    try:
        host = context.inventory.host(alias)
    except KeyError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    try:
        text = asyncio.run(preview(context.runner, host, remote_path, max_bytes=limit, deny_extra=context.config.files.deny_extra))
    except DeniedPath as exc:
        fail(f"refused: {exc}")
        raise typer.Exit(Exit.USAGE) from exc
    except OSError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.FAILED) from exc
    console.print(text)
