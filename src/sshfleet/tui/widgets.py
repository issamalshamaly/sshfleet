"""Small shared widgets: the status bar, the warning banner, and cell formatting."""

from __future__ import annotations

import time

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from ..collector import HostSnapshot, Severity

SEVERITY_STYLE = {Severity.CRIT: "red", Severity.WARN: "yellow", Severity.INFO: "cyan"}


def fmt_uptime(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    days, rest = divmod(seconds, 86400)
    hours, minutes = divmod(rest // 60, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h"


def fmt_bytes(value: int | None) -> str:
    if not value:
        return ""
    size = float(value)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.0f}T"


def reach_cell(snapshot: HostSnapshot | None, stale: bool = False) -> Text:
    if snapshot is None:
        return Text("◌", style="dim")
    if not snapshot.reachable:
        return Text(f"○ {(snapshot.error_kind or 'err').upper()[:5]}", style="red")
    return Text("◌" if stale else "●", style="yellow" if stale else "green")


def pct_cell(value: int | None, warn: int, crit: int) -> Text:
    if value is None:
        return Text("—", style="dim")
    style = "red" if value >= crit else ("yellow" if value >= warn else "")
    return Text(str(value), style=style)


def containers_cell(snapshot: HostSnapshot | None) -> Text:
    if snapshot is None or not snapshot.reachable:
        return Text("—", style="dim")
    if snapshot.runtime == "none":
        return Text("static", style="dim")
    running = len(snapshot.running_containers)
    expected = snapshot.expected_total
    style = "red" if snapshot.missing_containers else ("green" if running else "dim")
    return Text(f"{running}/{expected}" if expected else str(running), style=style)


def health_cell(snapshot: HostSnapshot | None) -> Text:
    if snapshot is None or not snapshot.probes:
        return Text("—", style="dim")
    ok = sum(1 for probe in snapshot.probes if probe.ok)
    if len(snapshot.probes) == 1:
        probe = snapshot.probes[0]
        return Text(probe.code, style="green" if probe.ok else "red")
    return Text(f"{ok}/{len(snapshot.probes)}", style="green" if ok == len(snapshot.probes) else "red")


def alerts_cell(snapshot: HostSnapshot | None, limit: int = 3) -> Text:
    if snapshot is None or not snapshot.alerts:
        return Text("")
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
    return Text(", ".join(parts), style=SEVERITY_STYLE.get(snapshot.severity or Severity.INFO, ""))


class StatusBar(Static):
    """One line: environments, connection state, refresh progress, alerts, clock."""

    environments: reactive[dict[str, str]] = reactive(dict, layout=False)
    mux_alive: reactive[int] = reactive(0)
    mux_total: reactive[int] = reactive(0)
    progress: reactive[tuple[int, int]] = reactive((0, 0))
    alerts: reactive[int] = reactive(0)
    tunnels: reactive[int] = reactive(0)
    note: reactive[str] = reactive("")

    def render(self) -> Text:
        text = Text()
        text.append("sshfleet ", style="bold")
        for name, state in self.environments.items():
            text.append("· ", style="dim")
            text.append(f"{name} ", style="bold")
            text.append(state + " ", style="green" if state == "●" else "red")
        text.append(f"· mux {self.mux_alive}/{self.mux_total} ", style="dim")
        done, total = self.progress
        if total and done < total:
            text.append(f"· ◐ {done}/{total} ", style="yellow")
        if self.tunnels:
            text.append(f"· tunnels {self.tunnels} ", style="cyan")
        if self.alerts:
            text.append(f"· alerts {self.alerts} ", style="red")
        if self.note:
            text.append(f"· {self.note} ", style="yellow")
        text.append("· " + time.strftime("%H:%M:%S"), style="dim")
        return text


class Banner(Static):
    """A warning line, hidden when there is nothing wrong."""

    message: reactive[str] = reactive("")

    def show(self, message: str) -> None:
        self.message = message
        self.update(Text(f"⚠ {message}", style="bold"))
        self.remove_class("hidden")

    def hide(self) -> None:
        self.message = ""
        self.add_class("hidden")
