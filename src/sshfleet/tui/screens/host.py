"""One host in detail: system facts, containers, alerts."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static, TabbedContent, TabPane

from ...collector import HostSnapshot
from ..messages import HostUpdated
from ..widgets import SEVERITY_STYLE, fmt_bytes, fmt_uptime

CONTAINER_COLUMNS = ("name", "image", "state", "status", "ports", "restarts", "log")


class HostScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("x", "restart", "restart"),
        Binding("l", "logs", "logs"),
        Binding("s", "ssh", "ssh"),
        Binding("f", "files", "files"),
        Binding("r", "refresh", "refresh"),
        Binding("y", "copy_command", "copy cmd", show=False),
    ]

    def __init__(self, alias: str, tab: str = "system") -> None:
        super().__init__()
        self.alias = alias
        self.initial_tab = tab
        self._container_names: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static(self.alias, id="host-header")
        with TabbedContent(initial=self.initial_tab, id="host-tabs"):
            with TabPane("System", id="system"):
                yield Static(id="system-body")
            with TabPane("Containers", id="containers"):
                yield DataTable(id="containers-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Alerts", id="alerts"):
                yield Static(id="alerts-body")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#containers-table", DataTable)
        for column in CONTAINER_COLUMNS:
            table.add_column(column, key=column)
        self.render_all()
        if self.initial_tab == "containers":
            table.focus()

    # ------------------------------------------------------------- rendering

    @property
    def snapshot(self) -> HostSnapshot | None:
        return self.app.fleet.snapshot(self.alias)

    def render_all(self) -> None:
        host = self.app.context.inventory.host(self.alias)
        environment = self.app.context.config.environment(host.env)
        snapshot = self.snapshot

        header = Text()
        header.append(host.label, style="bold")
        header.append("  ")
        header.append(environment.name.upper(), style=f"bold {environment.color}")
        header.append(f"  {host.hostname or ''}", style="dim")
        if host.proxy_jump:
            header.append(f" via {host.proxy_jump}", style="dim")
        header.append(f"  {host.user or ''}", style="dim")
        if snapshot and snapshot.reachable:
            header.append(f"  {snapshot.runtime}", style="dim")
        self.query_one("#host-header", Static).update(header)

        self.render_system(snapshot)
        self.render_containers(snapshot)
        self.render_alerts(snapshot)

    def render_system(self, snapshot: HostSnapshot | None) -> None:
        body = self.query_one("#system-body", Static)
        if snapshot is None:
            body.update(Text("no data yet — press r to refresh", style="dim"))
            return
        if not snapshot.reachable:
            body.update(Text(f"{snapshot.error_kind}: {snapshot.error_message}", style="red"))
            return

        text = Text()
        text.append(f"up {fmt_uptime(snapshot.uptime_s)}   ")
        text.append(f"load {snapshot.load[0]:.2f} {snapshot.load[1]:.2f} {snapshot.load[2]:.2f} / {snapshot.cpus} cpu   ")
        text.append(f"mem {snapshot.mem_used_pct}%   ")
        for disk in snapshot.disks:
            text.append(f"{disk.mount} {disk.pct}%   ")
        if not snapshot.sudo_ok:
            text.append("sudo -n failed", style="red")
        text.append("\n")
        if snapshot.listening:
            text.append("listening: " + ", ".join(str(p) for p in snapshot.listening) + "\n", style="dim")
        if snapshot.probes:
            text.append("probes: " + "  ".join(f"{p.port}{p.path}={p.code}" for p in snapshot.probes) + "\n", style="dim")
        if snapshot.frontends:
            text.append("web roots: " + ", ".join(f["name"] for f in snapshot.frontends) + "\n", style="dim")
        if snapshot.nginx and snapshot.nginx != "absent":
            text.append(f"nginx: {snapshot.nginx}\n", style="dim")
        if snapshot.kernel:
            text.append(f"kernel: {snapshot.kernel}\n", style="dim")
        body.update(text)

    def render_containers(self, snapshot: HostSnapshot | None) -> None:
        table = self.query_one("#containers-table", DataTable)
        table.clear()
        self._container_names = []
        if snapshot is None or not snapshot.reachable:
            return
        for container in snapshot.containers:
            table.add_row(
                container.name,
                container.image.split("/")[-1][:34],
                Text(container.state, style="green" if container.running else "red"),
                container.status,
                container.ports or ("host-net" if container.host_network else ""),
                str(container.restart_count or ""),
                fmt_bytes(container.log_bytes),
                key=container.name,
            )
            self._container_names.append(container.name)
        for name in snapshot.missing_containers:
            table.add_row(
                name, "—", Text("absent", style="red"), "expected here, nothing restarts it", "", "", "",
                key=name,
            )
            self._container_names.append(name)

    def render_alerts(self, snapshot: HostSnapshot | None) -> None:
        body = self.query_one("#alerts-body", Static)
        if snapshot is None or not snapshot.alerts:
            body.update(Text("no alerts", style="dim green"))
            return
        text = Text()
        for alert in snapshot.alerts:
            text.append(f"{alert.code:<12}", style=SEVERITY_STYLE.get(alert.severity, ""))
            text.append(f"{alert.message}\n")
        body.update(text)

    # -------------------------------------------------------------- messages

    def on_host_updated(self, message: HostUpdated) -> None:
        if message.snapshot.alias == self.alias:
            self.render_all()

    # --------------------------------------------------------------- actions

    @property
    def selected_container(self) -> str | None:
        table = self.query_one("#containers-table", DataTable)
        index = table.cursor_row
        if 0 <= index < len(self._container_names):
            return self._container_names[index]
        return None

    def action_refresh(self) -> None:
        self.app.refresh_one(self.alias)

    def action_restart(self) -> None:
        name = self.selected_container
        if name:
            self.app.confirm_restart(self.alias, name)
        else:
            self.app.notify("Select a container first", severity="warning")

    def action_logs(self) -> None:
        name = self.selected_container
        self.app.open_logs(host_alias=self.alias, container=name)

    def action_ssh(self) -> None:
        self.app.open_ssh(self.alias)

    def action_files(self) -> None:
        self.app.open_files(self.alias)

    def action_copy_command(self) -> None:
        name = self.selected_container
        if not name:
            return
        host = self.app.context.inventory.host(self.alias)
        prefix = "sudo -n " if host.sudo else ""
        runtime = host.runtime or "docker"
        self.app.show_command(f"ssh -F {self.app.context.mux.ssh_config} {self.alias} -- {prefix}{runtime} logs --tail 100 {name}")
