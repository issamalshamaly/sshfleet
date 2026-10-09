"""The sshfleet terminal interface.

All remote work runs in Textual workers that post messages back; no screen ever waits on SSH.
The fleet refreshes on a timer, results stream into the table as each host answers, and the
last known state is painted immediately at startup so the window is never empty.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from functools import partial

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.screen import Screen

from .. import shell as shell_module
from ..collector import transitions
from ..context import Context, build_context
from ..fleet import FleetService
from ..logs import LogSource, container_source_for, host_sources, main_sources
from ..tunnels import TunnelManager
from .messages import HostUpdated, RefreshFinished, RefreshProgressed
from .screens import (
    FileBrowserScreen,
    FleetScreen,
    HelpScreen,
    HostScreen,
    LogSearchScreen,
    RestartConfirmScreen,
    RestartProgressScreen,
    TunnelsScreen,
)


class HostProvider(Provider):
    """Fuzzy jump to any host by alias, address, service or role."""

    async def startup(self) -> None:
        self.hosts = list(self.app.context.inventory.hosts())

    async def discover(self) -> Hits:
        for host in self.hosts[:20]:
            yield DiscoveryHit(
                f"{host.alias}  ({host.log_label or host.role})",
                partial(self.app.open_host, host.alias),
                help=f"{host.env} · {host.hostname or ''}",
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for host in self.hosts:
            haystack = f"{host.alias} {host.hostname or ''} {host.log_label} {host.env} {host.role}"
            score = matcher.match(haystack)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(host.alias),
                    partial(self.app.open_host, host.alias),
                    help=f"{host.env} · {host.log_label or host.role} · {host.hostname or ''}",
                )


class ContainerProvider(Provider):
    """Fuzzy jump to a container that was seen on the last refresh."""

    async def startup(self) -> None:
        self.entries: list[tuple[str, str]] = []
        for alias, snapshot in self.app.fleet.state.snapshots.items():
            for container in snapshot.containers:
                self.entries.append((container.name, alias))

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, alias in self.entries:
            score = matcher.match(f"{name} {alias}")
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(f"{name} @ {alias}"),
                    partial(self.app.open_host, alias, "containers"),
                    help="open the host's containers",
                )


class SshFleetApp(App):
    """Fleet monitor and support tool."""

    CSS_PATH = "styles.tcss"
    TITLE = "sshfleet"
    COMMANDS = App.COMMANDS | {HostProvider, ContainerProvider}
    BINDINGS = [
        Binding("q", "request_quit", "quit"),
        Binding("R", "refresh_all", "refresh", show=False),
        Binding("question_mark", "open_help", "help", show=False),
    ]

    def __init__(self, context: Context | None = None, env: str | None = None) -> None:
        super().__init__()
        self.context = context or build_context()
        self.fleet = FleetService(self.context)
        self.tunnels = TunnelManager(self.context.config, self.context.mux, self.context.paths.tunnels_file)
        self.env_filter = env
        self._previous_alerts: dict[str, list] = {}
        self._notified: dict[str, float] = {}

    # ----------------------------------------------------------------- setup

    def compose(self) -> ComposeResult:  # pragma: no cover - the screen provides the layout
        yield from ()

    def on_mount(self) -> None:
        self.fleet.load_cached()
        self.push_screen(FleetScreen())
        refresh = self.context.config.refresh
        self.set_interval(refresh.fast_s, self.action_refresh_all)
        self.set_interval(refresh.age_tick_s, self._tick_ages)
        self.call_after_refresh(self.action_refresh_all)

    def _tick_ages(self) -> None:
        screen = self.screen
        if isinstance(screen, FleetScreen):
            screen.tick_ages()

    # --------------------------------------------------------------- refresh

    def action_refresh_all(self) -> None:
        self.run_worker(self._refresh_all(), group="refresh-all", exclusive=True)

    async def _refresh_all(self) -> None:
        def on_update(snapshot) -> None:
            self.post_message(HostUpdated(snapshot))

        def on_progress(progress) -> None:
            self.post_message(RefreshProgressed(progress))

        async for _ in self.fleet.iter_refresh(self.env_filter, on_update=on_update, on_progress=on_progress):
            pass
        self.fleet.save()
        self._announce_alerts()
        self.post_message(RefreshFinished(self.env_filter))

    def refresh_one(self, alias: str) -> None:
        self.run_worker(self._refresh_one(alias), group=f"host:{alias}", exclusive=True)

    async def _refresh_one(self, alias: str) -> None:
        snapshot = await self.fleet.refresh_host(alias)
        self.post_message(HostUpdated(snapshot))

    def _announce_alerts(self) -> None:
        """Only transitions are worth interrupting for."""
        for alias, snapshot in self.fleet.state.snapshots.items():
            appeared, _ = transitions(self._previous_alerts.get(alias, []), snapshot.alerts)
            self._previous_alerts[alias] = list(snapshot.alerts)
            for alert in appeared:
                if alert.severity.value == "info":
                    continue
                self.notify(
                    f"{alias}: {alert.message}",
                    title=alert.code,
                    severity="error" if alert.severity.value == "crit" else "warning",
                    timeout=10,
                )

    # ------------------------------------------------------------ navigation

    def on_host_updated(self, message: HostUpdated) -> None:
        self._forward(message)

    def on_refresh_progressed(self, message: RefreshProgressed) -> None:
        self._forward(message)

    def on_refresh_finished(self, message: RefreshFinished) -> None:
        self._forward(message)

    def _forward(self, message) -> None:
        """Hand the message to whichever screen is on top; it re-reads the fleet state itself."""
        if self.screen_stack:
            self.screen.post_message(message)

    def open_host(self, alias: str, tab: str = "system") -> None:
        self.push_screen(HostScreen(alias, tab))

    def open_files(self, alias: str, path: str | None = None) -> None:
        self.push_screen(FileBrowserScreen(alias, path))

    def open_tunnels(self) -> None:
        self.push_screen(TunnelsScreen())

    def open_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_open_help(self) -> None:
        self.open_help()

    def open_logs(
        self,
        *,
        host_alias: str | None = None,
        container: str | None = None,
        env: str | None = None,
        sources: Iterable[LogSource] | None = None,
    ) -> None:
        """Logs for one container, one host, or every main service in the environment."""
        environment = env or (self.context.inventory.host(host_alias).env if host_alias else None)
        if environment is None:
            environment = self.context.inventory.environments[0].name if self.context.inventory.environments else ""

        if sources is not None:
            chosen = list(sources)
        elif container and host_alias:
            chosen = [container_source_for(self.context.inventory.host(host_alias), container)]
        elif host_alias:
            host = self.context.inventory.host(host_alias)
            chosen = host_sources(host, self.fleet.snapshot(host_alias))
        else:
            chosen = main_sources(self.context.inventory, environment, self.fleet.snapshot)

        if not chosen:
            self.notify("no log sources for this selection", severity="warning")
            return
        self.push_screen(LogSearchScreen(chosen, environment))

    # ------------------------------------------------------------- container

    def confirm_restart(self, alias: str, container: str) -> None:
        def proceed(confirmed: bool | None) -> None:
            if confirmed:
                self.push_screen(RestartProgressScreen(alias, container))

        self.push_screen(RestartConfirmScreen(alias, container), proceed)

    # ------------------------------------------------------------------ ssh

    def open_ssh(self, alias: str, *, new_window: bool = False) -> None:
        self.run_worker(self._open_ssh(alias, new_window), group="ssh", exclusive=True)

    async def _open_ssh(self, alias: str, new_window: bool) -> None:
        # Warm first so the prompt appears immediately instead of after a handshake.
        if alias not in self.context.mux.alive:
            self.notify(f"connecting to {alias}…")
            budget = self.context.inventory.host(alias).connect_timeout_s or self.context.config.refresh.connect_timeout_s
            result = await self.context.mux.warm(alias, timeout=budget)
            if not result.ok:
                self.notify(f"{alias}: {result.message}", severity="error", timeout=10)
                return

        mode = shell_module.detect_mode(self.context.config.ui.get("open_ssh", "auto"), new_window=new_window)
        host = self.context.inventory.host(alias)
        self.context.audit.append("ssh.open", env=host.env, host=alias, status="ok", note=mode)

        if mode in {"konsole", "tmux"}:
            shell_module.open_shell(self.context.mux.ssh_config, alias, mode=mode)
            self.notify(f"opened a shell on {alias}")
            return

        plan = shell_module.plan(self.context.mux.ssh_config, alias, mode="suspend")
        try:
            with self.suspend():
                subprocess.run(plan.argv, check=False)
        except Exception as exc:  # SuspendNotSupported under textual-serve
            self.notify(f"cannot suspend here: {exc}. Try S for a new tab.", severity="warning", timeout=10)
            return
        self.refresh_one(alias)

    def show_command(self, command: str) -> None:
        self.notify(command, title="command", timeout=20)

    # ----------------------------------------------------------------- quit

    def action_request_quit(self) -> None:
        self.exit()

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand("Refresh all hosts", "Collect status from every host now", self.action_refresh_all)
        yield SystemCommand("Tunnels", "Open or close SSH port forwards", self.open_tunnels)
        yield SystemCommand("Help", "Key bindings and safety rules", self.open_help)
        yield SystemCommand("Reset connections", "Remove stale control sockets", self._reset_mux)

    def _reset_mux(self) -> None:
        removed = self.context.mux.reset_all()
        self.context.audit.append("mux.reset", status="ok", note=f"{removed} sockets")
        self.notify(f"removed {removed} control socket(s)")


def run_tui(env: str | None = None) -> None:
    SshFleetApp(env=env).run()
