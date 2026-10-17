"""Restart a container and prove it came back.

These services have no restart policy and no healthcheck, so "the command returned 0" is not
evidence. After restarting, the container's state and its HTTP endpoint are polled until both
agree twice in a row, or the attempt is declared failed and the last log lines are shown.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..inventory import Host
from ..ssh import RunResult, SshRunner
from . import runtime as rt
from .names import check_container_name

POLL_INTERVAL = 1.0
DEFAULT_VERIFY_TIMEOUT = 90.0
STABLE_POLLS = 2  # consecutive good polls before we call it verified
MIN_RUNNING_S = 5.0  # when there is no HTTP probe, "running this long" is the evidence


class Phase(StrEnum):
    CONFIRMED = "confirmed"
    RESTARTING = "restarting"
    POLLING = "polling"
    VERIFIED = "verified"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class VerifyStep:
    phase: Phase
    message: str
    elapsed: float = 0.0
    state: str | None = None
    probe_code: str | None = None
    detail: str = ""


@dataclass(slots=True)
class RestartOutcome:
    host: str
    container: str
    ok: bool
    phase: Phase
    command: str
    elapsed: float
    state: str | None = None
    probe_code: str | None = None
    message: str = ""
    steps: list[VerifyStep] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "container": self.container,
            "ok": self.ok,
            "phase": self.phase.value,
            "command": self.command,
            "elapsed": round(self.elapsed, 2),
            "state": self.state,
            "probe": self.probe_code,
            "message": self.message,
        }


def _probe_block(port: int | None, path: str | None) -> str:
    """Shell that sets ``$probe``. Statements, never a command substitution.

    ``probe=$( (cmd) )`` had to be written as a block: a substitution that opens with "(" is
    ``$((``, which bash reads as *arithmetic*. That made every probe fail with a syntax error,
    so a container that had come back perfectly well was polled until the verification timeout
    and then declared failed.
    """
    if not port:
        return 'probe="-"\n'
    url = f"http://127.0.0.1:{port}{path or '/'}"
    return (
        'probe=""\n'
        "if command -v curl >/dev/null 2>&1; then\n"
        # A failed curl still prints 000, so capture only: `|| echo 000` would append a second.
        f"    probe=$(curl -s -m 2 -o /dev/null -w '%{{http_code}}' {shlex.quote(url)} 2>/dev/null)\n"
        '    [ -z "$probe" ] && probe="000"\n'
        f'elif timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/{port}" 2>/dev/null; then\n'
        '    probe="open"\n'
        "else\n"
        '    probe="closed"\n'
        "fi\n"
    )


def status_script(host: Host, name: str, port: int | None, path: str | None) -> str:
    """One round-trip returning ``state<TAB>status<TAB>probe`` for a single container."""
    binary = rt.detect_command(host)
    prefix = "sudo -n " if host.sudo else ""
    quoted = shlex.quote(check_container_name(name))
    # The filter is one argument and is quoted as one: it used to go in bare.
    name_filter = shlex.quote(f"name=^{name}$")
    return (
        "# sshfleet:status\n"
        "export LC_ALL=C\n"
        f'RT={binary}\n'
        f'line=$({prefix}"$RT" ps -a --filter {name_filter} --format \'{{{{.State}}}}\t{{{{.Status}}}}\' 2>/dev/null | head -1)\n'
        f'[ -n "$line" ] || line=$({prefix}"$RT" inspect --format \'{{{{.State.Status}}}}\t{{{{.State.Status}}}}\' {quoted} 2>/dev/null)\n'
        f"{_probe_block(port, path)}"
        'printf "%s\\t%s\\n" "${line:-missing\tmissing}" "$probe"\n'
    )


def _parse_status(text: str) -> tuple[str, str, str]:
    fields = text.strip().split("\t")
    while len(fields) < 3:
        fields.append("")
    state, status, probe = fields[0].strip().lower(), fields[1].strip(), fields[2].strip()
    if state in {"", "missing"}:
        state = "missing"
    return state, status, probe


#: What the probe field can say when there is nothing useful to report.
NO_PROBE = {"", "-", "closed"}


def _probe_ok(code: str) -> bool:
    return code.startswith(("2", "3")) or code == "open"


async def restart(
    runner: SshRunner,
    host: Host,
    name: str,
    *,
    stop_timeout: int = 10,
    timeout: float = 30.0,
) -> RunResult:
    """Issue the restart itself. Verification is a separate, cancellable step."""
    command = rt.restart_command(host, name, stop_timeout)
    return await runner.run(host, command.render(), timeout=timeout, retries=0)


async def verify(
    runner: SshRunner,
    host: Host,
    name: str,
    *,
    port: int | None = None,
    path: str | None = None,
    timeout: float = DEFAULT_VERIFY_TIMEOUT,
) -> AsyncIterator[VerifyStep]:
    """Poll until the container is demonstrably back, or give up and say why."""
    script = status_script(host, name, port, path)
    started = time.monotonic()
    good = 0
    running_since: float | None = None
    last_state = "unknown"
    last_probe = "-"

    while True:
        elapsed = time.monotonic() - started
        if elapsed > timeout:
            yield VerifyStep(
                Phase.FAILED,
                f"still not healthy after {int(timeout)}s (state {last_state}, probe {last_probe})",
                elapsed,
                last_state,
                last_probe,
            )
            return

        result = await runner.run_script(host, script, timeout=15, retries=0)
        elapsed = time.monotonic() - started
        if not result.ok:
            yield VerifyStep(Phase.POLLING, f"probe failed: {result.error().message if result.error() else 'error'}", elapsed)
            await asyncio.sleep(POLL_INTERVAL)
            continue

        state, status, probe = _parse_status(result.out)
        last_state, last_probe = state, probe

        if state in {"exited", "dead", "missing"}:
            yield VerifyStep(Phase.FAILED, f"container is {state} after restart", elapsed, state, probe, status)
            return

        if state == "running":
            running_since = running_since if running_since is not None else elapsed
            # A probe that cannot answer at all (no curl, nothing listening yet) must not be the
            # only way to pass: fall back to "it has stayed up", the same rule used when no
            # health endpoint is configured.
            usable = bool(port) and probe not in NO_PROBE
            steady = (elapsed - running_since) >= MIN_RUNNING_S
            healthy = _probe_ok(probe) if usable else steady
            good = good + 1 if healthy else 0
            if usable:
                detail = f"running, probe {probe}"
            elif port:
                detail = f"running for {elapsed - running_since:.0f}s (no answer on port {port} yet)"
            else:
                detail = f"running for {elapsed - running_since:.0f}s"
            if good >= STABLE_POLLS:
                yield VerifyStep(Phase.VERIFIED, f"verified: {detail}", elapsed, state, probe, status)
                return
            yield VerifyStep(Phase.POLLING, detail, elapsed, state, probe, status)
        else:
            good = 0
            running_since = None
            yield VerifyStep(Phase.POLLING, f"state {state}", elapsed, state, probe, status)

        await asyncio.sleep(POLL_INTERVAL)


async def restart_and_verify(
    runner: SshRunner,
    host: Host,
    name: str,
    *,
    port: int | None = None,
    path: str | None = None,
    stop_timeout: int = 10,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
) -> AsyncIterator[VerifyStep | RestartOutcome]:
    """Restart, then stream verification steps, ending with a :class:`RestartOutcome`."""
    command = rt.restart_command(host, name, stop_timeout)
    started = time.monotonic()
    steps: list[VerifyStep] = []

    yield VerifyStep(Phase.RESTARTING, f"running {command.render()}", 0.0)
    result = await restart(runner, host, name, stop_timeout=stop_timeout)
    if not result.ok:
        error = result.error()
        message = error.message if error else "restart failed"
        step = VerifyStep(Phase.FAILED, message, time.monotonic() - started)
        steps.append(step)
        yield step
        yield RestartOutcome(
            host=host.alias, container=name, ok=False, phase=Phase.FAILED,
            command=command.render(), elapsed=time.monotonic() - started, message=message, steps=steps,
        )
        return

    final: VerifyStep | None = None
    async for step in verify(runner, host, name, port=port, path=path, timeout=verify_timeout):
        steps.append(step)
        yield step
        if step.phase in {Phase.VERIFIED, Phase.FAILED}:
            final = step
            break

    ok = final is not None and final.phase is Phase.VERIFIED
    yield RestartOutcome(
        host=host.alias,
        container=name,
        ok=ok,
        phase=final.phase if final else Phase.FAILED,
        command=command.render(),
        elapsed=time.monotonic() - started,
        state=final.state if final else None,
        probe_code=final.probe_code if final else None,
        message=final.message if final else "verification did not complete",
        steps=steps,
    )


async def tail_logs(runner: SshRunner, host: Host, name: str, lines: int = 80) -> str:
    """Last log lines, for showing why a restart failed."""
    command = rt.logs_command(host, name, lines)
    result = await runner.run(host, command.render() + " 2>&1", timeout=20, retries=0)
    return result.out or result.err


# ------------------------------------------------------------------- remove

#: States in which a container may be removed. Anything else is refused on the host itself.
REMOVABLE_STATES = frozenset({"exited", "created", "dead"})


@dataclass(slots=True)
class RemoveOutcome:
    ok: bool
    state: str
    message: str
    command: str = ""
    saved_log: str | None = None
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "state": self.state,
            "message": self.message,
            "command": self.command,
            "saved_log": self.saved_log,
            "elapsed": round(self.elapsed, 2),
        }


async def remove(
    runner: SshRunner,
    host: Host,
    name: str,
    *,
    save_logs_to: Path | None = None,
    tail: int = 2000,
) -> RemoveOutcome:
    """Remove a container that is not running, keeping its last log lines first.

    The state is read on the host immediately before the command, not from the last snapshot,
    so a container that came back to life since the page was drawn is left alone. Removing a
    container deletes its json-file log with it, which is why the tail is saved by default.
    """
    started = time.monotonic()
    probe = await runner.run(host, rt.inspect_command(host, name).render() + " 2>&1", timeout=20, retries=0)
    if not probe.ok or "\t" not in probe.out:
        return RemoveOutcome(False, "missing", f"{name} is not on this host", elapsed=time.monotonic() - started)
    fields = probe.out.strip().splitlines()[0].split("\t")
    state = fields[5].strip().lower() if len(fields) > 5 else "unknown"
    if state not in REMOVABLE_STATES:
        return RemoveOutcome(
            False, state, f"{name} is {state}; only a stopped container can be removed", elapsed=time.monotonic() - started
        )

    saved: str | None = None
    if save_logs_to is not None:
        text = await tail_logs(runner, host, name, tail)
        save_logs_to.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        save_logs_to.write_text(text)
        save_logs_to.chmod(0o600)
        saved = str(save_logs_to)

    command = rt.remove_command(host, name)
    result = await runner.run(host, command.render() + " 2>&1", timeout=60, retries=0)
    message = f"{name} removed" if result.ok else (result.out or result.err or "rm failed").strip().splitlines()[-1][:200]
    return RemoveOutcome(result.ok, state, message, command.render(), saved, time.monotonic() - started)
