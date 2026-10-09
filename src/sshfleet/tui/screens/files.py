"""Browsing and downloading files on a host."""

from __future__ import annotations

import posixpath
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static

from ...files import DeniedPath, Listing, download, download_dir_for, list_dir
from ..widgets import fmt_bytes

COLUMNS = ("name", "size", "modified", "mode", "owner", "note")


class FileBrowserScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("enter", "open", "open"),
        Binding("backspace", "up", "up"),
        Binding("d", "download", "download"),
        Binding("g", "goto", "go to path"),
        Binding("full_stop", "toggle_hidden", "hidden"),
        Binding("r", "reload", "reload"),
    ]

    def __init__(self, alias: str, path: str | None = None) -> None:
        super().__init__()
        self.alias = alias
        self.path = path or ""
        self.show_hidden = False
        self.listing: Listing | None = None
        self._names: list[str] = []

    def compose(self) -> ComposeResult:
        # The header starts with content so the first layout pass has something to measure.
        yield Static(f"{self.alias}:{self.path or ''}", id="host-header")
        yield DataTable(id="files", cursor_type="row", zebra_stripes=True)
        yield Input(placeholder="path…", id="goto", classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#files", DataTable)
        for column in COLUMNS:
            table.add_column(column, key=column)
        host = self.app.context.inventory.host(self.alias)
        if not self.path:
            self.path = f"/home/{host.user or 'ubuntu'}"
        table.focus()
        self.action_reload()

    # ------------------------------------------------------------- rendering

    def _paint(self) -> None:
        # Not named _render: Textual's Widget._render builds the widget's visual, and
        # overriding it makes the screen render as nothing.
        header = Text()
        header.append(f"{self.alias}:", style="bold")
        header.append(self.path)
        if self.listing and self.listing.used_sudo:
            header.append("  (sudo)", style="dim")
        self.query_one("#host-header", Static).update(header)

        table = self.query_one("#files", DataTable)
        table.clear()
        self._names = []
        if self.listing is None:
            return
        for entry in self.listing.entries:
            if not self.show_hidden and entry.name.startswith("."):
                continue
            name = Text(entry.name + ("/" if entry.is_dir else ""))
            if entry.denied:
                name = Text(entry.name + " 🔒", style="dim red")
            elif entry.is_dir:
                name.stylize("bold cyan")
            table.add_row(
                name,
                "" if entry.is_dir else fmt_bytes(entry.size),
                datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M"),
                entry.mode,
                f"{entry.owner}:{entry.group}",
                entry.denied_reason or "",
                key=entry.name,
            )
            self._names.append(entry.name)

    async def _load(self) -> None:
        context = self.app.context
        host = context.inventory.host(self.alias)
        try:
            self.listing = await list_dir(
                context.runner, host, self.path, deny_extra=context.config.files.deny_extra
            )
        except (OSError, NotADirectoryError) as exc:
            self.app.notify(str(exc), severity="error")
            return
        self._paint()

    # --------------------------------------------------------------- actions

    @property
    def selected(self):
        table = self.query_one("#files", DataTable)
        index = table.cursor_row
        if self.listing is None or not (0 <= index < len(self._names)):
            return None
        name = self._names[index]
        return next((e for e in self.listing.entries if e.name == name), None)

    def action_reload(self) -> None:
        self.run_worker(self._load(), group=f"files:{self.alias}", exclusive=True)

    def action_open(self) -> None:
        entry = self.selected
        if entry is None:
            return
        if entry.is_dir:
            self.path = posixpath.join(self.path, entry.name)
            self.action_reload()
        else:
            self.action_download()

    def action_up(self) -> None:
        parent = posixpath.dirname(self.path.rstrip("/"))
        if parent and parent != self.path:
            self.path = parent
            self.action_reload()

    def action_toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self._paint()

    def action_goto(self) -> None:
        field = self.query_one("#goto", Input)
        field.remove_class("hidden")
        field.value = self.path
        field.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "goto":
            self.path = event.value.strip() or self.path
            event.input.add_class("hidden")
            self.query_one("#files", DataTable).focus()
            self.action_reload()

    def action_download(self) -> None:
        entry = self.selected
        if entry is None or entry.is_dir:
            return
        if entry.denied:
            self.app.notify(f"refused: {entry.denied_reason}", severity="error")
            return
        remote = posixpath.join(self.path, entry.name)
        self.run_worker(self._download(remote), group=f"xfer:{remote}")

    async def _download(self, remote: str) -> None:
        context = self.app.context
        host = context.inventory.host(self.alias)
        target = download_dir_for(context.download_dir, host) / Path(remote).name
        self.app.notify(f"downloading {remote}…")
        try:
            result = await download(
                context.runner, host, remote, target, deny_extra=context.config.files.deny_extra
            )
        except DeniedPath as exc:
            context.audit.append("file.download", env=host.env, host=self.alias, target=remote, status="denied", note=exc.reason)
            self.app.notify(f"refused: {exc.reason}", severity="error")
            return
        context.audit.append(
            "file.download", env=host.env, host=self.alias, target=remote,
            status="ok" if result.ok else "failed", note=f"{result.method}, {result.bytes} bytes",
        )
        if result.ok:
            self.app.notify(f"saved {fmt_bytes(result.bytes)} to {result.destination}", timeout=10)
        else:
            self.app.notify(result.message, severity="error", timeout=10)
