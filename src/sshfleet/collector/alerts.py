"""Derive alerts from a snapshot.

The rules encode what goes wrong on a fleet of hand-deployed containers: without a restart
policy one missing from ``ps`` is an outage; disks fill because docker's json-file logs are
unbounded by default; and a rising restart count means something is crash-looping.

None of that holds for containers a scheduler creates and throws away — starting, exiting 0 and
vanishing is their whole life — so a workload matching one of the host's ignore globs is passed
over here entirely. It is still collected and still listed; it just never speaks.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..config import Thresholds
from ..inventory import Host
from .model import Alert, HostSnapshot, Severity


def _run_age_s(started_at: str) -> float | None:
    """Seconds since the container's current run began, from docker's RFC3339 timestamp."""
    if not started_at:
        return None
    text = started_at.replace("Z", "+00:00")
    if "." in text:  # docker prints nanoseconds; fromisoformat wants microseconds at most
        head, _, tail = text.partition(".")
        fraction, sign, offset = tail.partition("+") if "+" in tail else (tail, "", "")
        text = f"{head}.{fraction[:6]}" + (f"{sign}{offset}" if sign else "")
    try:
        started = datetime.fromisoformat(text)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - started).total_seconds(), 0.0)


def _fmt_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _fmt_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def evaluate(
    snapshot: HostSnapshot,
    host: Host,
    thresholds: Thresholds,
    previous: HostSnapshot | None = None,
) -> list[Alert]:
    alerts: list[Alert] = []

    def add(code: str, severity: Severity, message: str, container: str | None = None) -> None:
        alerts.append(Alert(code=code, severity=severity, message=message, host=snapshot.alias, container=container))

    if not snapshot.reachable:
        code = "BASTION_DOWN" if snapshot.error_kind == "bastion_down" else "UNREACH"
        add(code, Severity.CRIT, snapshot.error_message or "unreachable")
        return alerts

    disk = snapshot.worst_disk
    if disk:
        if disk.pct >= thresholds.disk_crit_pct:
            add("DISK", Severity.CRIT, f"{disk.mount} {disk.pct}% full ({_fmt_bytes(disk.free)} free)")
        elif disk.pct >= thresholds.disk_warn_pct:
            add("DISK", Severity.WARN, f"{disk.mount} {disk.pct}% full ({_fmt_bytes(disk.free)} free)")

    if snapshot.mem_total:
        used = snapshot.mem_used_pct
        if used >= thresholds.mem_crit_pct:
            add("MEM", Severity.CRIT, f"memory {used}% used")
        elif used >= thresholds.mem_warn_pct:
            add("MEM", Severity.WARN, f"memory {used}% used")

    if snapshot.load_per_cpu >= thresholds.load_per_cpu_warn:
        add("LOAD", Severity.WARN, f"load {snapshot.load[0]:.2f} on {snapshot.cpus} cpu")

    if not snapshot.sudo_ok:
        add("SUDO", Severity.WARN, "sudo -n failed; container state unknown")
    elif snapshot.runtime == "none" and snapshot.expected_count:
        add("NORUNTIME", Severity.CRIT, "no docker or podman on this host")
    elif snapshot.runtime_error:
        add("RUNTIME", Severity.CRIT, snapshot.runtime_error.splitlines()[0][:120])

    gone = snapshot.vanished_containers
    # Named one by one while that is still readable; beyond a handful the count itself is the
    # news and the names go in one line. One alert or the other, never both for the same loss.
    if len(gone) > 3:
        add("ABSENT", Severity.CRIT, f"{len(gone)} workloads were here and are gone: {', '.join(gone[:3])}, …")
    else:
        for name in gone:
            add("ABSENT", Severity.CRIT, f"{name} was here and is gone", container=name)
    # Counted against what the host is actually held to: a job runner that leaves a dozen finished
    # containers behind must not make a host with a missing service look complete.
    tracked = snapshot.tracked_containers
    # Only when the list could be read: with sudo or the runtime failing it is empty for that
    # reason alone, and the SUDO or RUNTIME alert above already says so.
    if snapshot.saw_containers and snapshot.expected_count is not None and len(tracked) < snapshot.expected_count:
        add("ABSENT", Severity.CRIT, f"{len(tracked)} of {snapshot.expected_count} workloads present")

    for container in tracked:
        if container.state == "restarting":
            add("RESTARTING", Severity.CRIT, f"{container.name} is restarting", container=container.name)
        elif not container.running and container.state != "paused":
            # Any container that is not running, whatever it is called. A crash is an outage; a
            # clean exit of something that was running a moment ago is worth a look; a clean exit
            # that predates our first sight of the host (an init or migration job) is just noted.
            exit_note = f" (exit {container.exit_code})" if container.exit_code not in (None, 0) else ""
            was_running = previous is not None and (before := previous.container(container.name)) is not None and before.running
            if container.exit_code not in (None, 0) or container.state == "dead":
                severity = Severity.CRIT
            elif was_running:
                severity = Severity.WARN
            else:
                severity = Severity.INFO
            add("STOPPED", severity, f"{container.name} is {container.state}{exit_note}", container=container.name)

        if container.restart_count >= thresholds.restart_count_warn:
            # Docker's own counter, cumulative since the container was created and only ever
            # raised by its restart policy — sshfleet never restarts anything on its own. What
            # says whether this is happening *now* is how long the current run has lasted, so a
            # container that has been up for a day is reported without crying wolf.
            age = _run_age_s(container.started_at)
            recent = age is None or age < 86400
            suffix = f", up {_fmt_age(age)}" if age is not None else ""
            add(
                "RESTARTS",
                Severity.WARN if recent else Severity.INFO,
                f"{container.name} restarted {container.restart_count}x by its restart policy{suffix}",
                container=container.name,
            )

        if container.log_bytes and container.log_bytes >= thresholds.container_log_warn_bytes:
            add("LOGSIZE", Severity.WARN, f"{container.name} log file is {_fmt_bytes(container.log_bytes)}", container=container.name)

    # Only the configured endpoint is worth an alert: a discovered port failing usually means
    # the tool guessed the path, not that the service is unwell.
    if host.health_port:
        configured = next((p for p in snapshot.probes if p.port == host.health_port), None)
        if configured is not None and not configured.ok and snapshot.running_containers:
            # The probe may have been skipped for several refreshes on a host whose health
            # endpoint is rate-limited, so the alert says how old the reading is.
            age = snapshot.at - configured.at if configured.at else 0.0
            stale = f", last checked {_fmt_age(age)} ago" if age > 90 else ""
            add("PROBE", Severity.WARN, f"health probe failed: {configured.port}{configured.path}={configured.code}{stale}")

    if previous is not None and previous.reachable:
        for container in tracked:
            before = previous.container(container.name)
            if before and container.restart_count > before.restart_count:
                add(
                    "RESTARTED",
                    Severity.WARN,
                    f"{container.name} restarted since the last check",
                    container=container.name,
                )

    return alerts


def transitions(previous: list[Alert], current: list[Alert]) -> tuple[list[Alert], list[Alert]]:
    """Alerts that appeared and alerts that cleared — the only ones worth a toast."""
    before = {a.key: a for a in previous}
    after = {a.key: a for a in current}
    appeared = [a for key, a in after.items() if key not in before]
    cleared = [a for key, a in before.items() if key not in after]
    return appeared, cleared
