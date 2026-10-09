"""The fleet table: every host, refreshed in the background."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input

from ...collector import HostSnapshot
from ..messages import HostUpdated, RefreshFinished, RefreshProgressed
from ..widgets import (
    Banner,
    StatusBar,
    alerts_cell,
    containers_cell,
    fmt_age,
    fmt_uptime,
    health_cell,
    pct_cell,
    reach_cell,
)

#: Explicit widths: the first paint happens before any host has answered, so auto-sizing would
#: lock every column to the width of a placeholder dash and clip the real values afterwards.
COLUMNS: tuple[tuple[str, int], ...] = (
    ("env", 5),
    ("host", 32),
    ("reach", 8),
    ("load", 5),
    ("mem%", 5),
    ("disk%", 6),
    ("ctrs", 7),
    ("health", 7),
    ("up", 8),
    ("age", 5),
    ("alerts", 38),
)
COLUMN_KEYS = tuple(name for name, _ in COLUMNS)


class FleetScreen(Screen):
    """Rows are updated in place; the table is only rebuilt when the filter changes."""

    BINDINGS = [
        Binding("enter", "open_host", "detail"),
        Binding("s", "ssh", "ssh"),
        Binding("S", "ssh_tab", "ssh tab", show=False),
        Binding("l", "logs", "logs"),
        Binding("f", "files", "files"),
        Binding("x", "restart", "restart"),
        Binding("r", "refresh_host", "refresh host", show=False),
        Binding("R", "refresh_all", "refresh"),
        Binding("t", "tunnels", "tunnels"),
        Binding("a", "alerts_only", "alerts"),
        Binding("slash", "focus_filter", "filter"),
        Binding("0", "env_all", "all envs", show=False),
        Binding("1", "env_first", "env 1", show=False),
        Binding("2", "env_second", "env 2", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("g", "cursor_top", "top", show=False),
        Binding("G", "cursor_bottom", "bottom", show=False),
        Binding("question_mark", "help", "help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.env_filter: str | None = None
        self.text_filter: str = ""
        self.alerts_only = False
        self._row_keys: list[str] = []

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status-bar")
        yield Banner("", id="banner", classes="hidden")
        with Horizontal(id="filter-bar"):
            yield Input(placeholder="filter hosts…  (esc to clear)", id="filter")
        yield DataTable(id="fleet", cursor_type="row", zebra_stripes=True, fixed_columns=2)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#fleet", DataTable)
        for name, width in COLUMNS:
            table.add_column(name, key=name, width=width)
        self.rebuild()
        table.focus()

    # ------------------------------------------------------------- rendering

    @property
    def app_ref(self):
        return self.app  # typed access without importing the app module

    def visible_hosts(self):
        fleet = self.app_ref.fleet
        hosts = fleet.hosts(self.env_filter)
        if self.text_filter:
            needle = self.text_filter.lower()
            hosts = [
                h for h in hosts
                if needle in h.alias.lower() or needle in h.log_label.lower() or needle in (h.hostname or "")
            ]
        if self.alerts_only:
            hosts = [h for h in hosts if (fleet.snapshot(h.alias) and fleet.snapshot(h.alias).alerts)]
        return sorted(hosts, key=lambda h: (h.env, h.alias))

    def _row(self, host, snapshot: HostSnapshot | None) -> list:
        thresholds = self.app_ref.context.config.thresholds
        environment = self.app_ref.context.config.environment(host.env)
        stale = self.app_ref.fleet.is_stale(host.alias) and snapshot is not None
        disk = snapshot.worst_disk if snapshot and snapshot.reachable else None
        return [
            Text(environment.short, style=environment.color),
            host.label,
            reach_cell(snapshot, stale),
            f"{snapshot.load[0]:.2f}" if snapshot and snapshot.reachable else "—",
            pct_cell(snapshot.mem_used_pct if snapshot and snapshot.reachable else None, thresholds.mem_warn_pct, thresholds.mem_crit_pct),
            pct_cell(disk.pct if disk else None, thresholds.disk_warn_pct, thresholds.disk_crit_pct),
            containers_cell(snapshot),
            health_cell(snapshot),
            fmt_uptime(snapshot.uptime_s) if snapshot and snapshot.reachable else "—",
            fmt_age(self.app_ref.fleet.age(host.alias)),
            alerts_cell(snapshot),
        ]

    def rebuild(self) -> None:
        table = self.query_one("#fleet", DataTable)
        cursor = table.cursor_row
        table.clear()
        self._row_keys = []
        for host in self.visible_hosts():
            snapshot = self.app_ref.fleet.snapshot(host.alias)
            table.add_row(*self._row(host, snapshot), key=host.alias)
            self._row_keys.append(host.alias)
        if self._row_keys:
            table.move_cursor(row=min(cursor, len(self._row_keys) - 1))
        self.update_status()

    def update_row(self, alias: str) -> None:
        if alias not in self._row_keys:
            return
        table = self.query_one("#fleet", DataTable)
        host = self.app_ref.context.inventory.host(alias)
        snapshot = self.app_ref.fleet.snapshot(alias)
        for column, value in zip(COLUMN_KEYS, self._row(host, snapshot), strict=True):
            try:
                table.update_cell(alias, column, value, update_width=False)
            except KeyError:
                return

    def tick_ages(self) -> None:
        table = self.query_one("#fleet", DataTable)
        for alias in self._row_keys:
            try:
                table.update_cell(alias, "age", fmt_age(self.app_ref.fleet.age(alias)), update_width=False)
            except KeyError:
                continue

    def update_status(self) -> None:
        status = self.query_one("#status-bar", StatusBar)
        fleet = self.app_ref.fleet
        environments = {}
        for environment in self.app_ref.context.inventory.environments:
            bastions = environment.bastions
            ok = all(fleet.state.bastion_ok.get(b, True) for b in bastions) if bastions else True
            environments[environment.short] = "●" if ok else "○"
        status.environments = environments
        status.mux_alive = len(self.app_ref.context.mux.alive)
        status.mux_total = len(self.app_ref.context.inventory.hosts())
        status.alerts = len(fleet.state.alerts(self.env_filter))
        status.note = "" if not self.alerts_only else "alerts only"

        banner = self.query_one("#banner", Banner)
        down = [alias for alias, ok in fleet.state.bastion_ok.items() if not ok]
        if down:
            affected = sum(len(self.app_ref.context.inventory.behind(alias)) for alias in down)
            banner.show(f"bastion {', '.join(down)} unreachable — {affected} hosts skipped (R to retry)")
        else:
            banner.hide()

    # -------------------------------------------------------------- messages

    def on_host_updated(self, message: HostUpdated) -> None:
        self.update_row(message.snapshot.alias)
        self.update_status()

    def on_refresh_progressed(self, message: RefreshProgressed) -> None:
        status = self.query_one("#status-bar", StatusBar)
        status.progress = (message.progress.done, message.progress.total)

    def on_refresh_finished(self, message: RefreshFinished) -> None:
        self.query_one("#status-bar", StatusBar).progress = (0, 0)
        self.update_status()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.text_filter = event.value
            self.rebuild()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#fleet", DataTable).focus()

    # --------------------------------------------------------------- actions

    @property
    def selected_alias(self) -> str | None:
        table = self.query_one("#fleet", DataTable)
        if not self._row_keys:
            return None
        index = table.cursor_row
        return self._row_keys[index] if 0 <= index < len(self._row_keys) else None

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_cursor_down(self) -> None:
        self.query_one("#fleet", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#fleet", DataTable).action_cursor_up()

    def action_cursor_top(self) -> None:
        self.query_one("#fleet", DataTable).move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        table = self.query_one("#fleet", DataTable)
        table.move_cursor(row=max(len(self._row_keys) - 1, 0))

    def action_alerts_only(self) -> None:
        self.alerts_only = not self.alerts_only
        self.rebuild()

    def action_env_all(self) -> None:
        self.env_filter = None
        self.rebuild()

    def _env_at(self, index: int) -> None:
        environments = self.app_ref.context.inventory.environments
        if index < len(environments):
            self.env_filter = environments[index].name
            self.rebuild()

    def action_env_first(self) -> None:
        self._env_at(0)

    def action_env_second(self) -> None:
        self._env_at(1)

    def action_open_host(self) -> None:
        alias = self.selected_alias
        if alias:
            self.app_ref.open_host(alias)

    def action_refresh_all(self) -> None:
        self.app_ref.action_refresh_all()

    def action_refresh_host(self) -> None:
        alias = self.selected_alias
        if alias:
            self.app_ref.refresh_one(alias)

    def action_ssh(self) -> None:
        alias = self.selected_alias
        if alias:
            self.app_ref.open_ssh(alias)

    def action_ssh_tab(self) -> None:
        alias = self.selected_alias
        if alias:
            self.app_ref.open_ssh(alias, new_window=True)

    def action_logs(self) -> None:
        alias = self.selected_alias
        self.app_ref.open_logs(host_alias=alias, env=self.env_filter)

    def action_files(self) -> None:
        alias = self.selected_alias
        if alias:
            self.app_ref.open_files(alias)

    def action_restart(self) -> None:
        alias = self.selected_alias
        if alias:
            self.app_ref.open_host(alias, tab="containers")

    def action_tunnels(self) -> None:
        self.app_ref.open_tunnels()

    def action_help(self) -> None:
        self.app_ref.open_help()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self.app_ref.open_host(str(event.row_key.value))
