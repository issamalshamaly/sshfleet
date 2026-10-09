"""Port forwards: Kibana, Seq, Elasticsearch, databases."""

from __future__ import annotations

import shutil
import subprocess

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from ...tunnels import TunnelManager

COLUMNS = ("name", "env", "state", "local", "remote", "via", "url")


class TunnelsScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("enter", "toggle", "open/close"),
        Binding("o", "open_url", "browser"),
        Binding("r", "reload", "reload"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._names: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("Tunnels ride on the connections sshfleet already has open.", classes="panel-title")
        yield DataTable(id="tunnels", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tunnels", DataTable)
        for column in COLUMNS:
            table.add_column(column, key=column)
        table.focus()
        self.action_reload()

    @property
    def manager(self) -> TunnelManager:
        return self.app.tunnels

    def action_reload(self) -> None:
        table = self.query_one("#tunnels", DataTable)
        table.clear()
        self._names = []
        states = self.manager.status()
        if not states:
            self.app.notify("no tunnels configured — add them under `tunnels:` in config.yaml", severity="warning")
        for state in states:
            table.add_row(
                state.name,
                state.env,
                Text("up", style="green") if state.up else Text("down", style="dim"),
                state.endpoint,
                state.remote,
                state.via,
                state.open_url or "",
                key=state.name,
            )
            self._names.append(state.name)

    @property
    def selected(self) -> str | None:
        table = self.query_one("#tunnels", DataTable)
        index = table.cursor_row
        return self._names[index] if 0 <= index < len(self._names) else None

    def action_toggle(self) -> None:
        name = self.selected
        if name:
            self.run_worker(self._toggle(name), group="tunnel", exclusive=True)

    async def _toggle(self, name: str) -> None:
        current = {s.name: s for s in self.manager.status()}.get(name)
        context = self.app.context
        if current and current.wanted:
            state = await self.manager.down(name)
            context.audit.append(
                "tunnel.down", env=state.env, host=state.via, target=name,
                status="failed" if state.held else "ok", note=state.error or None,
            )
            if state.held:
                self.app.notify(f"{name}: {state.error}", severity="error", timeout=10)
            else:
                self.app.notify(f"closed {name}")
        else:
            state = await self.manager.up(name)
            context.audit.append(
                "tunnel.up", env=state.env, host=state.via, target=name,
                status="ok" if state.up else "failed", note=state.endpoint,
            )
            if state.up:
                self.app.notify(f"{name} on {state.endpoint}")
            else:
                self.app.notify(f"{name}: {state.error}", severity="error", timeout=10)
        self.action_reload()

    def action_open_url(self) -> None:
        name = self.selected
        if not name:
            return
        state = {s.name: s for s in self.manager.status()}.get(name)
        if state and state.open_url and shutil.which("xdg-open"):
            subprocess.Popen(
                ["xdg-open", state.open_url],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.app.notify(f"opened {state.open_url}")
        else:
            self.app.notify("no URL for this tunnel", severity="warning")
