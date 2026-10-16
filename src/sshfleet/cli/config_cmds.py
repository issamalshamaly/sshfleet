"""``sshfleet config``: every state the configuration has been in, and putting one back."""

from __future__ import annotations

import calendar
import time
from dataclasses import asdict
from typing import Annotated

import typer
from rich.syntax import Syntax

from ..config import loader as config_loader
from ..context import build_context
from ..history import (
    History,
    HistoryError,
    RestoreRefused,
    StateRecord,
    apply_restore,
    compare,
    plan_restore,
    record_now,
    restored_reason,
    tracked_files,
    with_content,
)
from .output import Exit, console, emit_json, fail, info, table

app = typer.Typer(help="The configuration's history: list it, compare states, put one back.", no_args_is_help=True)

#: Who caused a state, in words. "disk" and "watcher" are both a hand edit, noticed by the CLI or
#: by the server.
SOURCES = {
    "web": "web page",
    "watcher": "outside edit",
    "disk": "outside edit",
    "cli": "command line",
    "start": "server start",
    "restore": "restore",
    "import": "import",
}


def _when(at: str) -> str:
    """A state's UTC timestamp in this machine's local time."""
    try:
        seconds = calendar.timegm(time.strptime(at[:19], "%Y-%m-%dT%H:%M:%S"))
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))
    except ValueError:
        return at


def _changed(record: StateRecord) -> str:
    changes = record.changes or {}
    if changes.get("first"):
        return "first record"
    parts: list[str] = []
    if changes.get("config"):
        parts.append(", ".join(changes["config"][:4]) + ("…" if len(changes["config"]) > 4 else ""))
    if changes.get("hosts_added"):
        parts.append(f"+{len(changes['hosts_added'])} host(s)")
    if changes.get("hosts_removed"):
        parts.append(f"-{len(changes['hosts_removed'])} host(s)")
    ssh = [f for f in changes.get("files", []) if f.get("role") == "ssh"]
    if ssh and not (changes.get("hosts_added") or changes.get("hosts_removed")):
        parts.append(", ".join(f["path"].rsplit("/", 1)[-1] for f in ssh[:3]))
    return "; ".join(parts) or "—"


def _history(context) -> History:
    return History(context.paths.history_dir)


@app.command("history")
def history_cmd(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many states to show, newest first.")] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List the states the configuration has been in, newest first."""
    context = build_context(write_ssh_config=False)
    records, total = _history(context).list(limit)
    if as_json:
        emit_json({"states": [r.to_dict() for r in records], "total": total})
        return
    if not records:
        info("no history yet")
        return
    rows = [
        [r.code, _when(r.at), SOURCES.get(r.source, r.source), r.reason + ("" if r.valid else " [does not load]"), _changed(r)]
        for r in records
    ]
    console.print(
        table(
            f"Configuration history ({len(records)} of {total})",
            ["code", "when", "by", "what", "changed"],
            rows,
            caption="sshfleet config show <code> --diff   ·   sshfleet config restore <code>",
        )
    )


@app.command("show")
def show(
    ref: Annotated[str, typer.Argument(help="A state's code (six characters) or id.")],
    diff: Annotated[bool, typer.Option("--diff", help="Print the differences, file by file.")] = False,
    against: Annotated[str, typer.Option("--against", help="previous: what this state changed; current: what restoring it would change.")] = "previous",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """One state: why it was recorded, its files, and what changed."""
    context = build_context(write_ssh_config=False)
    history = _history(context)
    try:
        record = history.resolve(ref)
    except HistoryError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc
    if against == "current":
        try:
            diffs = plan_restore(history, record.id, tracked_files(context.config, context.paths)).diffs
        except RestoreRefused as exc:
            fail(str(exc))
            raise typer.Exit(Exit.FAILED) from exc
    else:
        previous = history.previous(record.id)
        diffs = compare(with_content(history, previous) if previous else [], with_content(history, record))
    if as_json:
        emit_json({"state": record.to_dict(), "against": against, "diffs": [asdict(d) for d in diffs]})
        return
    console.print(f"[bold]{record.code}[/bold]  {record.id}")
    console.print(f"  {_when(record.at)} · {SOURCES.get(record.source, record.source)} · {record.reason}")
    if not record.valid:
        console.print(f"  [red]does not load:[/red] {record.error}")
    if record.restored_from:
        console.print(f"  restored from {record.restored_from}")
    for entry in record.files:
        status = "kept" if entry.kept else ("missing" if entry.missing else f"not kept: {entry.skipped}")
        console.print(f"  {entry.role:6} {entry.path}  [dim]{status}[/dim]")
    if diff:
        for item in diffs:
            if item.change == "same":
                continue
            console.print(f"\n[bold]{item.change}[/bold] {item.path}" + (f"  [dim]{item.note}[/dim]" if item.note else ""))
            if item.diff:
                console.print(Syntax(item.diff, "diff", theme="ansi_dark", background_color="default"))


@app.command("restore")
def restore(
    ref: Annotated[str, typer.Argument(help="A state's code (six characters) or id.")],
    only: Annotated[list[str] | None, typer.Option("--only", help="Restore only this file (repeatable).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be written, write nothing.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for the code.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Put an earlier state back. Checked first; the state it replaces is kept, so this can be undone."""
    context = build_context(write_ssh_config=False)
    history = _history(context)
    try:
        plan = plan_restore(history, ref, tracked_files(context.config, context.paths), only=set(only) if only else None)
    except HistoryError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc
    except RestoreRefused as exc:
        fail(str(exc))
        raise typer.Exit(Exit.FAILED) from exc

    target = plan.target
    if as_json and (dry_run or not plan.writes):
        emit_json({"plan": plan.to_dict(), "written": []})
        return
    if not as_json:
        console.print(f"Restoring [bold]{target.code}[/bold] — {_when(target.at)}, {target.reason}")
        for item in plan.diffs:
            console.print(f"  [yellow]write[/yellow]  {item.path}")
        for path in plan.unchanged:
            console.print(f"  [dim]same[/dim]   {path}")
        for path in plan.left_in_place:
            console.print(f"  [dim]left[/dim]   {path} (not part of that state; left as it is)")
        for item in plan.unavailable:
            console.print(f"  [dim]kept[/dim]   {item['path']} ({item['why']}; left as it is)")
        if plan.migrated:
            console.print("  config.yaml is upgraded to the current layout on the way back")
    if not plan.writes:
        info("nothing to change: the files already hold this state")
        return
    if dry_run:
        for item in plan.diffs:
            if item.diff:
                console.print(Syntax(item.diff, "diff", theme="ansi_dark", background_color="default"))
        return
    if not yes:
        typed = typer.prompt(f"Type {target.code} to put this state back")
        if typed.strip() != target.code:
            fail("not confirmed; nothing was written")
            raise typer.Exit(Exit.USAGE)

    before = context.remember(f"before restoring {target.code}", source="restore")
    written = apply_restore(plan, context.paths)
    context.config = config_loader.load(context.paths.config_file)
    after, _ = record_now(
        context.config,
        context.paths,
        reason=restored_reason(target),
        source="restore",
        restored_from=target.id,
    )
    context.audit.append(
        "config.restore",
        target=target.id,
        status="ok",
        note=f"cli; {len(written)} file(s) written; before {before.id if before else '?'}, after {after.id}",
    )
    if as_json:
        emit_json({"plan": plan.to_dict(), "written": written, "before": before.id if before else None, "after": after.id})
        return
    console.print(f"[green]restored[/green] {target.code}: {len(written)} file(s) written. A running server picks it up within seconds.")
    if before:
        console.print(f"Undo with [bold]sshfleet config restore {before.code}[/bold]")
