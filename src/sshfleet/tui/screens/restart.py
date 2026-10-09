"""Confirming and watching a container restart.

Production asks for the container name to be typed. Nothing is reported as restarted until the
container has been seen running and answering twice in a row.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Log, ProgressBar, Static

from ...containers import RestartOutcome, VerifyStep, restart_and_verify, tail_logs
from ...containers.runtime import restart_command


class RestartConfirmScreen(ModalScreen[bool]):
    """Shows exactly what will run, and makes production deliberate."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, alias: str, container: str) -> None:
        super().__init__()
        self.alias = alias
        self.container = container

    def compose(self) -> ComposeResult:
        host = self.app.context.inventory.host(self.alias)
        environment = self.app.context.config.environment(host.env)
        self.is_production = "prod" in environment.name.lower()
        command = restart_command(host, self.container)
        snapshot = self.app.fleet.snapshot(self.alias)
        current = snapshot.container(self.container) if snapshot else None

        classes = "" if self.is_production else "safe"
        with Vertical(id="confirm-box", classes=classes):
            yield Static(Text.assemble("Environment  ", (environment.name.upper(), f"bold {environment.color}")))
            yield Static(f"Host         {host.alias}  ({host.hostname or ''}" + (f" via {host.proxy_jump}" if host.proxy_jump else "") + ")")
            state = f"{current.state}, up {current.status}" if current else "not currently listed"
            yield Static(f"Container    {self.container}   [{state}]")
            yield Static(Text(f"Command      {command.preview(str(self.app.context.mux.ssh_config))}", style="dim"))
            yield Static(Text("Restarts in place. sshfleet never stops, removes or prunes a container.", style="dim"))
            if self.is_production:
                yield Label("Type the container name to confirm:")
                yield Input(placeholder=self.container, id="confirm-input")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Restart", variant="error" if self.is_production else "primary", id="go")

    def on_mount(self) -> None:
        if self.is_production:
            self.query_one("#confirm-input", Input).focus()
        else:
            self.query_one("#go", Button).focus()

    def _confirmed(self) -> bool:
        if not self.is_production:
            return True
        return self.query_one("#confirm-input", Input).value.strip() == self.container

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go":
            if self._confirmed():
                self.dismiss(True)
            else:
                self.app.notify("The name does not match; nothing was done", severity="error")
        else:
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._confirmed():
            self.dismiss(True)
        else:
            self.app.notify("The name does not match; nothing was done", severity="error")

    def action_cancel(self) -> None:
        self.dismiss(False)


class RestartProgressScreen(ModalScreen[RestartOutcome | None]):
    """Runs the restart and shows the verification as it happens."""

    BINDINGS = [Binding("escape", "close", "close")]

    def __init__(self, alias: str, container: str) -> None:
        super().__init__()
        self.alias = alias
        self.container = container
        self.outcome: RestartOutcome | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box", classes="safe"):
            yield Static(Text.assemble(("Restarting ", "bold"), (self.container, "bold cyan"), f" on {self.alias}"))
            yield ProgressBar(total=90, show_eta=False, id="restart-bar")
            yield Log(id="restart-log", max_lines=200)
            with Horizontal(id="confirm-buttons"):
                yield Button("Close", variant="default", id="close")

    def on_mount(self) -> None:
        self.run_restart()

    def action_close(self) -> None:
        self.dismiss(self.outcome)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.outcome)

    def _log(self, step: VerifyStep) -> None:
        log = self.query_one("#restart-log", Log)
        log.write_line(f"[{step.elapsed:5.0f}s] {step.phase.value:<11} {step.message}")
        self.query_one("#restart-bar", ProgressBar).update(progress=min(step.elapsed, 90))

    async def _run(self) -> None:
        context = self.app.context
        host = context.inventory.host(self.alias)
        watched = host.watches(self.container)
        port = host.health_port if watched else None
        path = host.health_path if watched else None

        command = restart_command(host, self.container)
        context.audit.append(
            "container.restart", env=host.env, host=self.alias, target=self.container,
            command=command.render(), status="started",
        )

        async for event in restart_and_verify(context.runner, host, self.container, port=port, path=path):
            if isinstance(event, RestartOutcome):
                self.outcome = event
            else:
                self._log(event)

        outcome = self.outcome
        if outcome is None:
            return
        context.audit.append(
            "container.restart", env=host.env, host=self.alias, target=self.container,
            command=outcome.command, status="ok" if outcome.ok else "failed",
            duration_ms=int(outcome.elapsed * 1000),
            verify={"phase": outcome.phase.value, "state": outcome.state, "probe": outcome.probe_code},
        )

        log = self.query_one("#restart-log", Log)
        if outcome.ok:
            self.app.notify(f"{self.container} restarted and verified", severity="information")
        else:
            self.app.notify(f"{self.container}: {outcome.message}", severity="error", timeout=12)
            log.write_line("")
            log.write_line("last log lines:")
            for line in (await tail_logs(context.runner, host, self.container, 30)).splitlines()[-30:]:
                log.write_line("  " + line)
        self.app.refresh_one(self.alias)

    def run_restart(self) -> None:
        self.run_worker(self._run(), group=f"restart:{self.alias}:{self.container}", exclusive=True)
