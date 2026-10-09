"""Searching several services' logs in one window, merged into one timeline."""

from __future__ import annotations

import time
from zoneinfo import ZoneInfo

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Checkbox, DataTable, Footer, Input, RichLog, Select

from ... import tz as tz_module
from ...logs import (
    Bundle,
    LogLine,
    LogSource,
    SearchCoordinator,
    SearchDone,
    SearchRequest,
    SourceDone,
    SourceError,
    TimeWindow,
    bundle_dir,
)
from ..widgets import StatusBar

PALETTE = [
    "bold cyan", "bold magenta", "bold green", "bold yellow", "bold blue", "bold red",
    "bright_cyan", "bright_magenta", "bright_green", "bright_yellow", "bright_blue", "bright_white",
]
WINDOWS = [("5m", "5m"), ("15m", "15m"), ("1h", "1h"), ("6h", "6h"), ("24h", "24h")]
FLUSH_INTERVAL = 0.08
COUNT_COLUMNS = ("service", "host", "matches", "lines", "bytes", "state")


class LogSearchScreen(Screen):
    """Query bar, merged results, and a per-service count panel."""

    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("ctrl+r", "run", "re-run"),
        Binding("slash", "focus_pattern", "pattern"),
        Binding("d", "download", "bundle"),
        Binding("c", "toggle_counts", "counts"),
        Binding("w", "toggle_wrap", "wrap"),
        Binding("t", "toggle_tz", "tz"),
        Binding("m", "starred", "starred hosts"),
        Binding("ctrl+c", "cancel", "cancel", show=False),
    ]

    def __init__(self, sources: list[LogSource], env: str, pattern: str = "") -> None:
        super().__init__()
        self.sources = sources
        self.env = env
        self.initial_pattern = pattern
        self.styles_by_label: dict[str, str] = {}
        self.buffer: list[LogLine] = []
        self._pending: list[LogLine] = []
        self._last_flush = 0.0
        self.summary: SearchDone | None = None
        self.wrap = False
        self.use_utc = False
        self._running = False
        self._counts: dict[str, tuple[int, int, int, str]] = {}

    # --------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status-bar")
        with Horizontal(id="query-bar"):
            yield Input(placeholder="pattern (regex)…", id="pattern", value=self.initial_pattern)
            yield Input(placeholder="B", value="60", id="before", classes="narrow")
            yield Select(WINDOWS, value="1h", id="window", allow_blank=False)
            yield Checkbox("case", value=False, id="case")
        with Horizontal():
            yield RichLog(id="results", max_lines=20000, wrap=False, highlight=False, markup=False, auto_scroll=True)
            yield DataTable(id="counts", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#counts", DataTable)
        for column in COUNT_COLUMNS:
            table.add_column(column, key=column)
        self._assign_styles()
        self.set_interval(FLUSH_INTERVAL, self._flush)
        self.query_one("#pattern", Input).focus()
        self.render_sources()
        if self.initial_pattern:
            self.action_run()

    def _assign_styles(self) -> None:
        labels = sorted({s.label for s in self.sources})
        self.styles_by_label = {label: PALETTE[index % len(PALETTE)] for index, label in enumerate(labels)}

    def render_sources(self) -> None:
        table = self.query_one("#counts", DataTable)
        table.clear()
        for source in self.sources:
            table.add_row(
                Text(source.label, style=self.styles_by_label.get(source.label, "")),
                source.host_alias or "",
                "", "", "",
                Text("queued", style="dim"),
                key=source.id,
            )

    # ---------------------------------------------------------------- search

    @property
    def zone(self) -> ZoneInfo:
        return tz_module.zone_for("UTC" if self.use_utc else self.app.context.config.logs.display_tz)

    def _request(self) -> SearchRequest:
        logs_cfg = self.app.context.config.logs
        pattern = self.query_one("#pattern", Input).value
        try:
            before = int(self.query_one("#before", Input).value or 0)
        except ValueError:
            before = logs_cfg.default_before
        window = self.query_one("#window", Select).value or "1h"
        case = self.query_one("#case", Checkbox).value
        return SearchRequest(
            sources=tuple(self.sources),
            allowed_paths=tuple(logs_cfg.allowed_paths),
            pattern=pattern,
            ignore_case=not case,
            before=before,
            window=TimeWindow(spec=str(window), tz=logs_cfg.display_tz),
            tail_lines=logs_cfg.tail_lines,
            max_bytes=logs_cfg.max_bytes,
            max_line_bytes=logs_cfg.max_line_bytes,
            max_file_bytes=logs_cfg.max_file_bytes,
            timeout_s=logs_cfg.timeout_s,
            display_tz=logs_cfg.display_tz,
        )

    def action_run(self) -> None:
        self.buffer.clear()
        self._pending.clear()
        self._counts.clear()
        self.summary = None
        self.query_one("#results", RichLog).clear()
        self.render_sources()
        self._running = True
        self.run_worker(self._search(self._request()), group="logsearch", exclusive=True)

    def action_cancel(self) -> None:
        if self._running:
            self.workers.cancel_group(self, "logsearch")
            self._running = False
            self.app.notify("search cancelled", severity="warning")

    async def _search(self, request: SearchRequest, bundle: Bundle | None = None) -> None:
        context = self.app.context
        coordinator = SearchCoordinator(context.runner, context.inventory, context.config)
        status = self.query_one("#status-bar", StatusBar)
        status.note = "searching…"
        try:
            async for event in coordinator.run(request):
                if isinstance(event, LogLine):
                    self.buffer.append(event)
                    self._pending.append(event)
                    if bundle:
                        bundle.add(event)
                elif isinstance(event, SourceDone):
                    state = "truncated" if event.truncated else ("partial" if not event.coverage_ok else "done")
                    self._counts[event.source_id] = (event.matches, event.lines, event.bytes, state)
                    self._update_count(event.source_id)
                elif isinstance(event, SourceError):
                    self._counts[event.source_id] = (0, 0, 0, f"{event.kind}")
                    self._update_count(event.source_id, error=event.message)
                elif isinstance(event, SearchDone):
                    self.summary = event
        finally:
            self._running = False
            self._flush(force=True)
            self._update_status()
            if bundle and self.summary:
                directory = bundle.close(self.summary)
                context.audit.append(
                    "logs.download", env=self.env, target=str(directory), status="ok",
                    note=f"{self.summary.matches} matches",
                )
                self.app.notify(f"bundle written to {directory}", timeout=10)

    # -------------------------------------------------------------- painting

    def _format(self, line: LogLine) -> Text:
        text = Text()
        text.append(f"{line.label:<9}", style=self.styles_by_label.get(line.label, "white"))
        when = line.when
        stamp = when.astimezone(self.zone).strftime("%H:%M:%S.%f")[:-3] if when else "--:--:--.---"
        text.append(f" {stamp} ", style="dim")
        text.append(line.text, style="" if line.is_match else "dim")
        return text

    def _flush(self, force: bool = False) -> None:
        if not self._pending:
            return
        now = time.monotonic()
        if not force and now - self._last_flush < FLUSH_INTERVAL:
            return
        self._last_flush = now
        results = self.query_one("#results", RichLog)
        batch, self._pending = self._pending[:500], self._pending[500:]
        for line in batch:
            results.write(self._format(line))
        self._update_status()

    def _update_count(self, source_id: str, error: str = "") -> None:
        table = self.query_one("#counts", DataTable)
        matches, lines, size, state = self._counts.get(source_id, (0, 0, 0, ""))
        style = "red" if error else ("yellow" if state in {"truncated", "partial"} else "green")
        try:
            table.update_cell(source_id, "matches", str(matches), update_width=False)
            table.update_cell(source_id, "lines", str(lines), update_width=False)
            table.update_cell(source_id, "bytes", f"{size / 1024:.0f}K", update_width=False)
            table.update_cell(source_id, "state", Text(state, style=style), update_width=False)
        except KeyError:
            return

    def _update_status(self) -> None:
        status = self.query_one("#status-bar", StatusBar)
        matches = sum(count[0] for count in self._counts.values())
        parts = [f"{len(self.buffer)} lines", f"{matches} matches"]
        if self.summary:
            parts.append(f"{self.summary.elapsed:.1f}s")
            parts.append(self.summary.window.describe("UTC" if self.use_utc else self.app.context.config.logs.display_tz))
            if self.summary.truncated_sources:
                parts.append("truncated: " + ", ".join(self.summary.truncated_sources))
        elif self._running:
            parts.append("searching…")
        status.note = " · ".join(parts)

    # --------------------------------------------------------------- actions

    def action_focus_pattern(self) -> None:
        self.query_one("#pattern", Input).focus()

    def action_back(self) -> None:
        self.action_cancel()
        self.app.pop_screen()

    def action_toggle_counts(self) -> None:
        panel = self.query_one("#counts", DataTable)
        panel.display = not panel.display

    def action_toggle_wrap(self) -> None:
        self.wrap = not self.wrap
        results = self.query_one("#results", RichLog)
        results.wrap = self.wrap
        self._repaint()

    def action_toggle_tz(self) -> None:
        self.use_utc = not self.use_utc
        self._repaint()
        self._update_status()

    def action_starred(self) -> None:
        from ...logs import main_sources

        self.sources = main_sources(self.app.context.inventory, self.app.context.config, self.env)
        self._assign_styles()
        self.render_sources()
        self.app.notify(f"searching {len(self.sources)} main services")

    def action_download(self) -> None:
        if self._running:
            self.app.notify("wait for the search to finish", severity="warning")
            return
        request = self._request()
        directory = bundle_dir(self.app.context.download_dir, self.env, request.pattern or "all")
        bundle = Bundle(directory=directory, request=request, env=self.env)
        self.buffer.clear()
        self._pending.clear()
        self._counts.clear()
        self.query_one("#results", RichLog).clear()
        self.render_sources()
        self._running = True
        self.run_worker(self._search(request, bundle), group="logsearch", exclusive=True)

    def _repaint(self) -> None:
        results = self.query_one("#results", RichLog)
        results.clear()
        for line in self.buffer[-20000:]:
            results.write(self._format(line))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_run()
