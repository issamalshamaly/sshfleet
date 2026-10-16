"""``sshfleet config``: every state the configuration has been in, and putting one back."""

from __future__ import annotations

import calendar
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
from rich.syntax import Syntax

from ..bundle import (
    BundleError,
    ImportPlan,
    apply_import,
    build,
    load,
    parse,
    plan_import,
    to_json,
    write_dir,
)
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
from .output import Exit, console, emit_json, fail, info, table, warn

app = typer.Typer(
    help="The configuration's history (list, compare, restore) and bundles to move or share it (export, import).",
    no_args_is_help=True,
)

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
        note=f"cli; {len(written)} file(s) written" + (f"; undo by restoring {before.code}" if before else ""),
    )
    if as_json:
        emit_json({"plan": plan.to_dict(), "written": written, "before": before.id if before else None, "after": after.id})
        return
    console.print(f"[green]restored[/green] {target.code}: {len(written)} file(s) written. A running server picks it up within seconds.")
    if before:
        console.print(f"Undo with [bold]sshfleet config restore {before.code}[/bold]")


@app.command("export")
def export(
    out: Annotated[str, typer.Option("--out", "-o", help="A .json file, a folder (for git), or - for standard output.")] = "-",
    project: Annotated[list[str] | None, typer.Option("--project", help="Only this project (repeatable).")] = None,
    env: Annotated[list[str] | None, typer.Option("--env", help="Only this environment (repeatable).")] = None,
    with_local: Annotated[bool, typer.Option("--with-local", help="Also carry this machine's key paths, file paths and pauses.")] = False,
    allow_commands: Annotated[bool, typer.Option("--allow-commands", help="Carry SSH lines that run programs (ProxyCommand, Include, …).")] = False,
) -> None:
    """Write the setup as a bundle: to move it to another machine, or to share it through git.

    Keys, key paths and anything that looks like a credential stay behind; the export is refused
    if a secret would still be in it.
    """
    context = build_context(write_ssh_config=False)
    try:
        bundle, warnings = build(
            context.config,
            context.inventory,
            context.paths,
            projects=project or None,
            environments=env or None,
            with_local=with_local,
            allow_commands=allow_commands,
        )
    except BundleError as exc:
        fail(str(exc))
        context.audit.append("config.export", status="denied", note=f"cli; {str(exc)[:300]}")
        raise typer.Exit(Exit.FAILED) from exc
    for message in warnings:
        warn(message)
    environments = len(bundle["fleet"].get("environments") or [])
    if out == "-":
        typer.echo(to_json(bundle).decode(), nl=False)
        target = "standard output"
    elif out.endswith(".json"):
        path = Path(out).expanduser()
        path.write_bytes(to_json(bundle))
        path.chmod(0o600 if with_local else 0o644)
        target = str(path)
    else:
        try:
            write_dir(bundle, Path(out).expanduser())
        except BundleError as exc:
            fail(str(exc))
            raise typer.Exit(Exit.USAGE) from exc
        target = str(Path(out).expanduser())
    context.audit.append("config.export", target=", ".join((project or []) + (env or [])) or "everything", status="ok", note=f"cli; {target}")
    if out != "-":
        console.print(f"[green]exported[/green] {environments} environment(s) to {target}")


ACTION_STYLE = {
    "add": "green",
    "update": "yellow",
    "remove": "red",
    "conflict": "red",
    "kept-local": "cyan",
    "skipped": "dim",
}


def _read_bundle(source: str) -> dict:
    if source == "-":
        import sys

        return parse(sys.stdin.buffer.read())
    return load(Path(source))


def _show_plan(plan: ImportPlan, *, diffs: bool) -> None:
    shown = [c for c in plan.changes if c.action != "same"]
    for change in shown:
        style = ACTION_STYLE.get(change.action, "")
        note = f"  [dim]{change.note}[/dim]" if change.note else ""
        console.print(f"  [{style}]{change.action:10}[/{style}] {change.kind:11} {change.name}{note}")
    if not shown:
        console.print("  [dim]everything in the bundle is already here[/dim]")
    for message in plan.warnings:
        warn(message)
    for name in plan.needs_key:
        info(f"environment {name} has no key here: set it in Settings › Projects, or `sshfleet env add {name} <file> --key <path> --replace`")
    for name in plan.needs_value:
        info(f"{name} came as <redacted>: set it here if this machine needs it")
    if diffs:
        for path, text in plan.diffs.items():
            if text:
                console.print(f"\n[bold]{path}[/bold]")
                console.print(Syntax(text, "diff", theme="ansi_dark", background_color="default"))


@app.command("import")
def import_cmd(
    source: Annotated[str, typer.Argument(help="A bundle: a .json file, a folder, or - for standard input.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change, write nothing.")] = False,
    mode: Annotated[str, typer.Option("--mode", help="merge: add and update, never remove. replace: the fleet becomes the bundle's.")] = "merge",
    prefer: Annotated[str, typer.Option("--prefer", help="Where a setting differs: keep the local one, or take the bundle's.")] = "local",
    allow_commands: Annotated[bool, typer.Option("--allow-commands", help="Accept SSH lines that run programs (ProxyCommand, Include, …).")] = False,
    with_local: Annotated[bool, typer.Option("--with-local", help="Also take the bundle's key paths, file paths and pauses.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask before writing.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Bring a bundle in. Checked first; the state before is kept, so an import can be undone with
    `sshfleet config restore`."""
    context = build_context(write_ssh_config=False)
    try:
        bundle = _read_bundle(source)
        plan = plan_import(
            bundle, config=context.config, paths=context.paths, mode=mode, prefer=prefer, allow_commands=allow_commands, with_local=with_local
        )
    except BundleError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.FAILED) from exc
    if as_json and (dry_run or not plan.changed):
        emit_json({"plan": plan.to_dict(), "written": []})
        return
    if not as_json:
        manifest = bundle.get("manifest") or {}
        console.print(f"Bundle from [bold]{manifest.get('created_on', '?')}[/bold], {manifest.get('created_at', '?')} ({plan.mode}, prefer {plan.prefer})")
        _show_plan(plan, diffs=dry_run)
    if not plan.changed:
        info("nothing to change")
        return
    if dry_run:
        return
    if not yes:
        if mode == "replace":
            if typer.prompt("Replace mode unregisters environments the bundle does not carry. Type replace to go on").strip() != "replace":
                fail("not confirmed; nothing was written")
                raise typer.Exit(Exit.USAGE)
        elif not typer.confirm("Import these changes?"):
            fail("nothing was written")
            raise typer.Exit(Exit.USAGE)
    before = context.remember("before import", source="import")
    try:
        written = apply_import(plan, context.paths)
    except BundleError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.FAILED) from exc
    context.config = config_loader.load(context.paths.config_file)
    origin = (bundle.get("manifest") or {}).get("created_on") or "a bundle"
    after, _ = record_now(context.config, context.paths, reason=f"imported from {origin}", source="import")
    context.audit.append("config.import", target=origin, status="ok", note=f"cli; {plan.mode}; {len(written)} file(s) written")
    if as_json:
        emit_json({"plan": plan.to_dict(), "written": written, "before": before.id if before else None, "after": after.id})
        return
    console.print(f"[green]imported[/green]: {len(written)} file(s) written. A running server picks it up within seconds.")
    if before:
        console.print(f"Undo with [bold]sshfleet config restore {before.code}[/bold]")


@app.command("diff")
def diff_cmd(
    source: Annotated[str, typer.Argument(help="A bundle: a .json file, a folder, or - for standard input.")],
    mode: Annotated[str, typer.Option("--mode")] = "merge",
    prefer: Annotated[str, typer.Option("--prefer")] = "local",
    allow_commands: Annotated[bool, typer.Option("--allow-commands")] = False,
) -> None:
    """Would importing this bundle change anything? Exit 0: no. Exit 1: yes (listed). Exit 2: it
    would be refused. Made for a pre-commit check on a shared folder."""
    context = build_context(write_ssh_config=False)
    try:
        plan = plan_import(_read_bundle(source), config=context.config, paths=context.paths, mode=mode, prefer=prefer, allow_commands=allow_commands)
    except BundleError as exc:
        fail(str(exc))
        raise typer.Exit(2) from exc
    if not plan.changed:
        info("no changes")
        return
    _show_plan(plan, diffs=True)
    raise typer.Exit(1)
