"""``sshfleet audit``: the local trail of every change made through sshfleet, and its export."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from ..audit.log import AuditFilter, parse_since, render
from ..context import build_context
from .output import Exit, console, emit_json, fail, info, table

app = typer.Typer(help="The local audit trail of mutating actions, and its export.", invoke_without_command=True)


@app.callback()
def audit(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many entries to show.")] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the local audit trail of mutating actions."""
    if ctx.invoked_subcommand is not None:
        return
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


@app.command("export")
def export(
    since: Annotated[str | None, typer.Option("--since", help="30m, 12h, 7d, 2w, or a date such as 2026-10-01.")] = None,
    until: Annotated[str | None, typer.Option("--until", help="Same forms as --since.")] = None,
    env: Annotated[list[str] | None, typer.Option("--env", help="Only this environment (repeatable); fleet-wide entries stay in.")] = None,
    host: Annotated[str | None, typer.Option("--host", help="Only this host.")] = None,
    action: Annotated[list[str] | None, typer.Option("--action", help="An action or a glob such as 'container.*' (repeatable).")] = None,
    status: Annotated[str | None, typer.Option("--status", help="ok, failed, denied, …")] = None,
    jsonl: Annotated[bool, typer.Option("--jsonl", help="JSON Lines instead of CSV.")] = False,
    out: Annotated[str, typer.Option("--out", "-o", help="A file to write, or - for standard output.")] = "-",
) -> None:
    """Write the matching entries, oldest first, as CSV (for a spreadsheet) or JSON Lines."""
    context = build_context(write_ssh_config=False)
    try:
        flt = AuditFilter(
            since=parse_since(since) if since else None,
            until=parse_since(until) if until else None,
            actions=tuple(action or ()),
            envs=tuple(env) if env else None,
            host=host,
            status=status,
        )
    except ValueError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc
    fmt = "jsonl" if jsonl else "csv"
    body, count = render(context.audit.iter_entries(flt), fmt)
    if out == "-":
        sys.stdout.write(body)
    else:
        path = Path(out).expanduser()
        path.write_text(body)
        path.chmod(0o600)  # the trail names hosts and the commands run on them
        info(f"{count} entries written to {path}")
    context.audit.append("audit.export", status="ok", note=f"cli; {count} entries as {fmt}")


@app.command("verify")
def verify(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check that no line of the trail was changed, removed or put in between.

    Each line carries the hash of the one before it. This shows a line touched in the middle; a
    trail rewritten from the start shows only against a link you noted down earlier.
    """
    context = build_context(write_ssh_config=False)
    result = context.audit.verify()
    if as_json:
        emit_json(
            {"entries": result.entries, "unlinked": result.unlinked, "breaks": result.breaks, "head": result.head},
            ok=result.intact,
        )
        raise typer.Exit(Exit.OK if result.intact else Exit.FAILED)
    files = ", ".join(str(p) for p in context.audit.files()) or str(context.audit.path)
    if not result.entries:
        console.print(f"no audit entries yet ({files})")
        return
    older = f"; {result.unlinked} older lines from before the chain cannot be vouched for" if result.unlinked else ""
    if result.intact:
        console.print(f"[green]intact[/green]: {result.entries} entries in {files}{older}")
        console.print(f"last link {result.head}")
        return
    for message in result.breaks:
        fail(message)
    console.print(f"{len(result.breaks)} break(s) in {result.entries} entries ({files}){older}")
    raise typer.Exit(Exit.FAILED)
