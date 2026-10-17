"""``sshfleet logs …`` — search and follow logs across services."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.text import Text

from .. import tz as tz_module
from ..context import Context, build_context
from ..fleet import FleetService
from ..logs import (
    Bundle,
    LogLine,
    LogSource,
    SearchCoordinator,
    SearchDone,
    SearchRequest,
    SourceDone,
    SourceError,
    SourceProgress,
    TimeWindow,
    bundle_dir,
    container_source_for,
    host_sources,
    known_source_for,
    main_sources,
    workload_sources,
)
from ..logs.sources import describe_sources
from .output import Exit, console, err_console, fail, table

app = typer.Typer(no_args_is_help=True, help="Search and follow logs across services.")

PALETTE = [
    "bold cyan", "bold magenta", "bold green", "bold yellow", "bold blue", "bold red",
    "bright_cyan", "bright_magenta", "bright_green", "bright_yellow", "bright_blue", "bright_white",
]


def _styles(sources: list[LogSource]) -> dict[str, str]:
    labels = sorted({s.label for s in sources})
    return {label: PALETTE[index % len(PALETTE)] for index, label in enumerate(labels)}


def _snapshots(context: Context, aliases: list[str]) -> Callable[[str], object | None]:
    """What each host is running, from the cache, refreshing the ones the cache does not know.

    Workloads are discovered, not declared, so the search has to know what is there. The cached
    snapshot is usually enough; a host missing from it costs one round-trip.
    """
    fleet = FleetService(context)
    fleet.load_cached()
    unknown = [alias for alias in aliases if fleet.snapshot(alias) is None]
    if unknown:
        async def warm() -> None:
            await asyncio.gather(*(fleet.refresh_host(alias) for alias in unknown), return_exceptions=True)

        asyncio.run(warm())
    return fleet.snapshot


def _resolve_sources(
    context: Context,
    env: str,
    *,
    main: bool,
    hosts: list[str] | None,
    containers: list[str] | None,
    files: list[str] | None,
    include_files: bool,
) -> list[LogSource]:
    starred = [h.alias for h in context.inventory.hosts(env) if h.main]
    file_hosts = [spec.partition(":")[0] for spec in files or []]
    wanted = list(dict.fromkeys([*(starred if main else []), *(hosts or []), *file_hosts]))
    snapshots = _snapshots(context, wanted)

    sources: list[LogSource] = []
    if main:
        sources += main_sources(context.inventory, env, snapshots)

    for alias in hosts or []:
        host = context.inventory.host(alias)
        snapshot = snapshots(alias)
        sources += host_sources(host, snapshot) if include_files else workload_sources(host, snapshot)

    for spec in containers or []:
        alias, _, name = spec.partition(":")
        if not name:
            fail(f"--container expects host:name, got {spec!r}")
            raise typer.Exit(Exit.USAGE)
        try:
            sources.append(container_source_for(context.inventory.host(alias), name))
        except ValueError as exc:
            fail(str(exc))
            raise typer.Exit(Exit.USAGE) from exc

    for spec in files or []:
        alias, _, path = spec.partition(":")
        if not path:
            fail(f"--file expects host:/path, got {spec!r}")
            raise typer.Exit(Exit.USAGE)
        # A path the host already declares keeps its format and its rotated glob; anything else
        # is read as a plain file, which is what typing a path by hand asks for.
        host = context.inventory.host(alias)
        sources.append(known_source_for(host, path, snapshots(alias)))

    seen: set[str] = set()
    unique: list[LogSource] = []
    for source in sources:
        if source.id not in seen:
            seen.add(source.id)
            unique.append(source)
    return unique


def _format_line(line: LogLine, styles: dict[str, str], tz: ZoneInfo, show_time: bool = True) -> Text:
    text = Text()
    text.append(f"{line.label:<9}", style=styles.get(line.label, "white"))
    if show_time:
        when = line.when
        stamp = when.astimezone(tz).strftime("%H:%M:%S.%f")[:-3] if when else "--:--:--.---"
        text.append(f" {stamp} ", style="dim")
    # Context lines are dimmed so the actual matches stand out in a -B 60 window.
    text.append(line.text, style="" if line.is_match else "dim")
    return text


@app.command("search")
def search(
    env: Annotated[str, typer.Option("--env", "-e", help="Environment name.")],
    pattern: Annotated[str, typer.Option("--pattern", "-p", help="What to search for (regex by default).")] = "",
    main: Annotated[bool, typer.Option("--main", help="Search every starred host together.")] = False,
    host: Annotated[list[str] | None, typer.Option("--host", "-H", help="Every workload on this host (repeatable).")] = None,
    container: Annotated[list[str] | None, typer.Option("--container", "-c", help="host:container (repeatable).")] = None,
    file: Annotated[list[str] | None, typer.Option("--file", "-f", help="host:/path/to/log (repeatable).")] = None,
    since: Annotated[str, typer.Option("--since", help="Window: 30m, 2h, 10:00..11:30, or an absolute time.")] = "30m",
    until: Annotated[str | None, typer.Option("--until", help="End of the window (default: now).")] = None,
    before: Annotated[int, typer.Option("-B", "--before", help="Context lines before each match.")] = 60,
    after: Annotated[int, typer.Option("-A", "--after", help="Context lines after each match.")] = 0,
    fixed: Annotated[bool, typer.Option("--fixed", help="Treat the pattern as a fixed string.")] = False,
    case_sensitive: Annotated[bool, typer.Option("--case-sensitive", help="Match case exactly.")] = False,
    tail: Annotated[int, typer.Option("--tail", help="How many lines back the producer reads.")] = 0,
    max_bytes: Annotated[int, typer.Option("--max-bytes", help="Per-source byte cap.")] = 0,
    include_files: Annotated[bool, typer.Option("--include-files", help="Also search log files, not just stdout.")] = False,
    tz: Annotated[str | None, typer.Option("--tz", help="Display timezone: an IANA name such as Europe/Berlin, UTC, or local.")] = None,
    no_merge: Annotated[bool, typer.Option("--no-merge", help="Show each source as it arrives instead of one timeline.")] = False,
    download: Annotated[bool, typer.Option("--download", "-d", help="Write the results to a bundle folder.")] = False,
    download_to: Annotated[Path | None, typer.Option("--download-to", help="Bundle folder (implies --download).")] = None,
    raw_dump: Annotated[bool, typer.Option("--raw-dump", help="Reproduce the manual tail+grep, uncapped.")] = False,
    strict: Annotated[bool, typer.Option("--strict", help="Exit non-zero when results were truncated.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="NDJSON events for scripting.")] = False,
) -> None:
    """Search logs across one or many services in the same time window."""
    context = build_context()
    logs_cfg = context.config.logs

    try:
        environment = context.config.environment(env)
    except Exception as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    if not any([main, host, container, file]):
        main = True  # the common case: every starred host

    try:
        sources = _resolve_sources(
            context, environment.name,
            main=main, hosts=host, containers=container, files=file, include_files=include_files,
        )
    except KeyError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    if not sources:
        fail("no log sources matched")
        raise typer.Exit(Exit.USAGE)

    spec = f"{since}..{until}" if until else since
    request = SearchRequest(
        sources=tuple(sources),
        allowed_paths=tuple(logs_cfg.allowed_paths),
        pattern=pattern,
        mode="fixed" if fixed else "regex",
        ignore_case=not case_sensitive,
        before=before,
        after=after,
        window=TimeWindow(spec=spec, tz=tz or logs_cfg.display_tz),
        tail_lines=tail or logs_cfg.tail_lines,
        max_bytes=max_bytes or logs_cfg.max_bytes,
        max_line_bytes=logs_cfg.max_line_bytes,
        max_file_bytes=logs_cfg.max_file_bytes,
        timeout_s=logs_cfg.timeout_s,
        display_tz=tz or logs_cfg.display_tz,
        raw_dump=raw_dump,
    )

    zone = tz_module.zone_for(request.display_tz)
    styles = _styles(sources)
    bundle: Bundle | None = None
    if download or download_to:
        directory = download_to or bundle_dir(context.download_dir, environment.name, pattern or "all")
        bundle = Bundle(directory=directory, request=request, env=environment.name)

    summary: SearchDone | None = None
    context.audit.append(
        "logs.search", env=environment.name, target=(pattern or "")[:200] or None, status="started",
        note="cli; " + describe_sources(sources) + ("; saved as a bundle" if bundle else ""),
    )
    coordinator = SearchCoordinator(context.runner, context.inventory, context.config)

    async def run() -> None:
        nonlocal summary
        async for event in coordinator.run(request, merge=not no_merge):
            if isinstance(event, LogLine):
                if bundle:
                    bundle.add(event)
                if as_json:
                    sys.stdout.write(json.dumps({
                        "type": "line", "source": event.source_id, "label": event.label,
                        "ts": event.when.isoformat() if event.when else None,
                        "match": event.is_match, "text": event.text,
                    }) + "\n")
                else:
                    console.print(_format_line(event, styles, zone), soft_wrap=True)
            elif isinstance(event, SourceProgress):
                if as_json:
                    sys.stdout.write(json.dumps({
                        "type": "progress", "source": event.source_id, "lines": event.lines,
                        "bytes": event.bytes, "matches": event.matches,
                    }) + "\n")
            elif isinstance(event, SourceError):
                if as_json:
                    sys.stdout.write(json.dumps({
                        "type": "error", "source": event.source_id, "kind": event.kind, "message": event.message,
                    }) + "\n")
                else:
                    err_console.print(f"[red]{event.label}[/red] {event.kind}: {event.message}")
            elif isinstance(event, SourceDone):
                if as_json:
                    sys.stdout.write(json.dumps({
                        "type": "source_done", "source": event.source_id, "lines": event.lines,
                        "matches": event.matches, "bytes": event.bytes, "truncated": event.truncated,
                        "coverage_ok": event.coverage_ok,
                    }) + "\n")
            elif isinstance(event, SearchDone):
                summary = event

    asyncio.run(run())
    assert summary is not None

    if bundle:
        directory = bundle.close(summary)
        context.audit.append(
            "logs.download", env=environment.name, target=str(directory), status="ok",
            note=f"{summary.matches} matches from {len(sources)} sources",
        )

    if as_json:
        sys.stdout.write(json.dumps({
            "type": "summary",
            "window": {"since": summary.window.since.isoformat(), "until": summary.window.until.isoformat()},
            "lines": summary.lines, "matches": summary.matches, "bytes": summary.bytes,
            "elapsed": round(summary.elapsed, 2),
            "truncated": summary.truncated_sources, "failed": summary.failed_sources,
            "bundle": str(bundle.directory) if bundle else None,
        }) + "\n")
    else:
        _print_summary(summary, sources, request, bundle)

    if summary.failed_sources and len(summary.failed_sources) == len(sources):
        raise typer.Exit(Exit.FAILED)
    if summary.failed_sources:
        raise typer.Exit(Exit.PARTIAL)
    if strict and summary.truncated_sources:
        raise typer.Exit(Exit.TRUNCATED)
    raise typer.Exit(Exit.OK if summary.matches else Exit.NO_RESULTS)


def _print_summary(summary: SearchDone, sources: list[LogSource], request: SearchRequest, bundle: Bundle | None) -> None:
    rows = []
    for source in sources:
        outcome = summary.per_source.get(source.id)
        if isinstance(outcome, SourceDone):
            note = ""
            if outcome.truncated:
                note = "truncated"
            elif not outcome.coverage_ok:
                note = "tail did not reach the window start"
            rows.append([source.label, source.host_alias or "", str(outcome.matches), str(outcome.lines),
                         f"{outcome.bytes / 1024:.0f}K", f"{outcome.elapsed:.1f}s", note])
        elif isinstance(outcome, SourceError):
            rows.append([source.label, source.host_alias or "", "—", "—", "—", "—", f"{outcome.kind}: {outcome.message}"])
    err_console.print(table(
        f"{summary.matches} matches in {summary.window.describe(request.display_tz)}",
        ["service", "host", "matches", "lines", "bytes", "time", "note"],
        rows,
        caption=f"{summary.elapsed:.1f}s total",
        wrap={"note"},
    ))
    if summary.truncated_sources:
        err_console.print(
            f"[yellow]truncated[/yellow] for {', '.join(summary.truncated_sources)} — "
            "narrow the window or raise --max-bytes"
        )
    if bundle:
        err_console.print(f"[green]bundle[/green] {bundle.directory}")


@app.command("sources")
def sources_cmd(
    env: Annotated[str, typer.Option("--env", "-e", help="Environment name.")],
    main: Annotated[bool, typer.Option("--main", help="Only the starred hosts.")] = True,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show what ``--main`` would search in this environment."""
    context = build_context()
    environment = context.config.environment(env)
    sources = _resolve_sources(
        context, environment.name,
        main=True, hosts=None if main else [h.alias for h in context.inventory.hosts(environment.name)],
        containers=None, files=None, include_files=not main,
    )
    if as_json:
        from .output import emit_json

        emit_json([
            {"id": s.id, "kind": s.kind.value, "label": s.label, "host": s.host_alias,
             "container": s.container, "path": s.path, "format": s.log_format.value}
            for s in sources
        ])
        return
    rows = [[s.label, s.kind.value, s.host_alias or "", s.container or s.path or "", s.log_format.value] for s in sources]
    console.print(table(f"Log sources for {environment.name}", ["label", "kind", "host", "target", "format"], rows))


@app.command("tail")
def tail_cmd(
    env: Annotated[str, typer.Option("--env", "-e", help="Environment name.")],
    main: Annotated[bool, typer.Option("--main", help="Follow every starred host together.")] = False,
    host: Annotated[list[str] | None, typer.Option("--host", "-H")] = None,
    container: Annotated[list[str] | None, typer.Option("--container", "-c")] = None,
    pattern: Annotated[str, typer.Option("--pattern", "-p", help="Only show lines matching this.")] = "",
    since: Annotated[str, typer.Option("--since", help="How much history to show first.")] = "1m",
    tz: Annotated[str | None, typer.Option("--tz")] = None,
) -> None:
    """Follow several services at once, merged as they arrive."""
    from ..logs.follow import follow

    context = build_context()
    environment = context.config.environment(env)
    if not any([main, host, container]):
        main = True
    sources = _resolve_sources(
        context, environment.name, main=main, hosts=host, containers=container, files=None, include_files=False,
    )
    if not sources:
        fail("no log sources matched")
        raise typer.Exit(Exit.USAGE)

    limit = context.config.logs.follow_max_sources
    if len(sources) > limit:
        err_console.print(f"[yellow]following the first {limit} of {len(sources)} sources[/yellow]")
        sources = sources[:limit]

    zone = tz_module.zone_for(tz or context.config.logs.display_tz)
    styles = _styles(sources)
    context.audit.append(
        "logs.follow", env=environment.name, target=(pattern or "")[:200] or None, status="started",
        note="cli; " + describe_sources(sources),
    )

    async def run() -> None:
        async for event in follow(context.runner, context.inventory, sources, pattern=pattern, since=since):
            if isinstance(event, LogLine):
                console.print(_format_line(event, styles, zone), soft_wrap=True)
            elif isinstance(event, SourceError):
                err_console.print(f"[red]{event.label}[/red] {event.kind}: {event.message}")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        raise typer.Exit(Exit.INTERRUPTED) from None
