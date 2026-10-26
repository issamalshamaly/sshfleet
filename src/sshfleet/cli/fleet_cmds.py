"""``sshfleet fleet`` and ``sshfleet host`` — fleet status in the terminal."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.text import Text

from ..collector import HostSnapshot, Severity
from ..context import build_context
from ..fleet import FleetService
from .output import Exit, console, emit_json, fail, table

SEVERITY_STYLE = {Severity.CRIT: "red", Severity.WARN: "yellow", Severity.INFO: "cyan"}


def _fmt_uptime(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    days, rest = divmod(seconds, 86400)
    hours, minutes = divmod(rest // 60, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _reach_cell(snapshot: HostSnapshot) -> Text:
    if snapshot.reachable:
        return Text("●", style="green")
    # In words, whole: clipped to five capitals it read "○ BASTI" and "○ TIMEO".
    reason = (snapshot.error_kind or "error").replace("_", " ")
    return Text(f"○ {reason}", style="red")


def _containers_cell(snapshot: HostSnapshot) -> Text:
    if not snapshot.reachable:
        return Text("—", style="dim")
    if snapshot.runtime == "none":
        return Text("static", style="dim")
    running = len(snapshot.running_containers)
    expected = snapshot.expected_total
    style = "red" if snapshot.vanished_containers else ("green" if running else "dim")
    return Text(f"{running}/{expected}" if expected else str(running), style=style)


def _health_cell(snapshot: HostSnapshot) -> Text:
    if not snapshot.probes:
        return Text("—", style="dim")
    ok = sum(1 for p in snapshot.probes if p.ok)
    if len(snapshot.probes) == 1:
        probe = snapshot.probes[0]
        return Text(probe.code, style="green" if probe.ok else "red")
    return Text(f"{ok}/{len(snapshot.probes)}", style="green" if ok == len(snapshot.probes) else "red")


def _pct_cell(value: int | None, warn: int, crit: int) -> Text:
    if value is None:
        return Text("—", style="dim")
    style = "red" if value >= crit else ("yellow" if value >= warn else "")
    return Text(str(value), style=style)


def alert_summary(snapshot: HostSnapshot, limit: int = 3) -> str:
    """Distinct alert codes with a count, most severe first: ``ABSENT x3, DISK``."""
    order = {Severity.CRIT: 0, Severity.WARN: 1, Severity.INFO: 2}
    counts: dict[str, int] = {}
    ranks: dict[str, int] = {}
    for alert in snapshot.alerts:
        counts[alert.code] = counts.get(alert.code, 0) + 1
        ranks[alert.code] = min(ranks.get(alert.code, 9), order.get(alert.severity, 3))
    codes = sorted(counts, key=lambda code: (ranks[code], -counts[code], code))
    parts = [f"{code} x{counts[code]}" if counts[code] > 1 else code for code in codes[:limit]]
    if len(codes) > limit:
        parts.append(f"+{len(codes) - limit}")
    return ", ".join(parts)


def fleet(
    env: Annotated[str | None, typer.Option("--env", "-e", help="Restrict to one environment.")] = None,
    host_filter: Annotated[str | None, typer.Option("--host", "-H", help="Only aliases containing this text.")] = None,
    service: Annotated[str | None, typer.Option("--service", "-s", help="Only this service.")] = None,
    main: Annotated[bool, typer.Option("--main", help="Only the configured main services.")] = False,
    alerts_only: Annotated[bool, typer.Option("--alerts", help="Only hosts that have an alert.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Collect status from every host and print it as a table."""
    context = build_context()
    if not context.config.environments:
        fail("no environments registered — run `sshfleet env add --name … --ssh-config …`")
        raise typer.Exit(Exit.USAGE)

    service_filter = {"*starred*"} if main else ({service} if service else None)
    fleet_service = FleetService(context)

    selected = fleet_service.hosts(env)
    if host_filter:
        selected = [h for h in selected if host_filter in h.alias]
    if service_filter:
        selected = [h for h in selected if h.main] if main else [h for h in selected if h.log_label in service_filter]
    if not selected:
        fail("no hosts matched")
        raise typer.Exit(Exit.NO_RESULTS)

    aliases = {h.alias for h in selected}

    async def run() -> None:
        async for _ in fleet_service.iter_refresh(env):
            pass

    asyncio.run(run())
    fleet_service.save()

    snapshots = [s for alias, s in fleet_service.state.snapshots.items() if alias in aliases]
    snapshots.sort(key=lambda s: (s.env, s.alias))
    if alerts_only:
        snapshots = [s for s in snapshots if s.alerts]

    if as_json:
        emit_json([s.to_dict() for s in snapshots], ok=all(s.reachable for s in snapshots))
    else:
        thresholds = context.config.thresholds
        rows = []
        for snapshot in snapshots:
            host = context.inventory.host(snapshot.alias)
            disk = snapshot.worst_disk
            alert_text = Text(alert_summary(snapshot))
            if snapshot.alerts:
                alert_text.stylize(SEVERITY_STYLE.get(snapshot.severity or Severity.INFO, ""))
            environment = context.config.environment(snapshot.env)
            rows.append(
                [
                    # Its name, not its id: an id such as mudeer-staging would show as "mude".
                    Text(environment.name[:4], style=environment.color),
                    host.label,
                    _reach_cell(snapshot),
                    f"{snapshot.load[0]:.2f}" if snapshot.reachable else "—",
                    _pct_cell(snapshot.mem_used_pct if snapshot.reachable else None, thresholds.mem_warn_pct, thresholds.mem_crit_pct),
                    _pct_cell(disk.pct if disk else None, thresholds.disk_warn_pct, thresholds.disk_crit_pct),
                    _containers_cell(snapshot),
                    _health_cell(snapshot),
                    _fmt_uptime(snapshot.uptime_s) if snapshot.reachable else "—",
                    alert_text,
                ]
            )
        console.print(
            table(
                f"Fleet ({len(snapshots)} hosts)",
                ["env", "host", "reach", "load", "mem%", "disk%", "ctrs", "health", "uptime", "alerts"],
                rows,
                caption=f"refreshed in {max((s.elapsed for s in snapshots), default=0):.1f}s",
            )
        )

    unreachable = [s for s in snapshots if not s.reachable]
    if unreachable and len(unreachable) == len(snapshots):
        raise typer.Exit(Exit.FAILED)
    if unreachable:
        raise typer.Exit(Exit.PARTIAL)


def host(
    alias: Annotated[str, typer.Argument(help="Host alias.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one host in detail: system, containers, probes, alerts."""
    context = build_context()
    try:
        target = context.inventory.host(alias)
    except KeyError as exc:
        fail(str(exc))
        raise typer.Exit(Exit.USAGE) from exc

    fleet_service = FleetService(context)
    snapshot = asyncio.run(fleet_service.refresh_host(target.alias))

    if as_json:
        emit_json(snapshot.to_dict(), ok=snapshot.reachable)
        raise typer.Exit(Exit.OK if snapshot.reachable else Exit.FAILED)

    if not snapshot.reachable:
        fail(f"{alias}: {snapshot.error_kind}: {snapshot.error_message}")
        raise typer.Exit(Exit.FAILED)

    disk_text = "  ".join(f"{d.mount} {d.pct}%" for d in snapshot.disks)
    console.print(
        f"[bold]{target.label}[/bold]  [dim]{target.hostname or ''}"
        + (f" via {target.proxy_jump}" if target.proxy_jump else "")
        + f"  {target.user or ''}  {snapshot.runtime}[/dim]"
    )
    console.print(
        f"up {_fmt_uptime(snapshot.uptime_s)} · load {snapshot.load[0]:.2f} {snapshot.load[1]:.2f} {snapshot.load[2]:.2f}"
        f" / {snapshot.cpus} cpu · mem {snapshot.mem_used_pct}% · {disk_text}"
        + ("" if snapshot.sudo_ok else " · [red]sudo -n failed[/red]")
    )
    if snapshot.listening:
        console.print(f"listening: {', '.join(str(p) for p in snapshot.listening)}")

    if snapshot.containers:
        rows = [
            [
                c.name,
                c.image.split("/")[-1][:32],
                Text(c.state, style="green" if c.running else "red"),
                c.status,
                c.ports or ("host-net" if c.host_network else ""),
                c.restart_count or "",
            ]
            for c in snapshot.containers
        ]
        console.print(table("Containers", ["name", "image", "state", "status", "ports", "restarts"], rows))
    for name in snapshot.vanished_containers:
        console.print(f"[red]gone[/red] {name} (was on this host and is no longer listed: removed, not stopped)")

    if snapshot.probes:
        console.print("probes: " + "  ".join(f"{p.port}{p.path}={p.code}" for p in snapshot.probes))
    if snapshot.frontends:
        console.print("web roots: " + ", ".join(f["name"] for f in snapshot.frontends))
    for alert in snapshot.alerts:
        style = SEVERITY_STYLE.get(alert.severity, "")
        console.print(f"[{style}]{alert.code}[/{style}] {alert.message}")
