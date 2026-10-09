"""Turn the collector's JSON into a :class:`HostSnapshot`.

Docker and podman disagree about almost every field name in ``ps --format json``, so the
normalisation happens here rather than in the remote script.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..config.schema import VANISHED_MEMORY_S
from ..inventory import Host
from .model import ContainerInfo, DiskInfo, HostSnapshot, ProbeResult, SiteInfo


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ports_to_text(value: Any) -> str:
    """docker gives a string; podman gives a list of mapping objects."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for entry in value:
            if isinstance(entry, dict):
                host_ip = entry.get("host_ip") or ""
                host_port = entry.get("host_port") or entry.get("hostPort") or ""
                container_port = entry.get("container_port") or entry.get("containerPort") or ""
                protocol = entry.get("protocol") or "tcp"
                prefix = f"{host_ip}:" if host_ip else ""
                parts.append(f"{prefix}{host_port}->{container_port}/{protocol}")
            else:
                parts.append(str(entry))
        return ", ".join(parts)
    return ""


def _name_of(entry: dict[str, Any]) -> str:
    """docker: ``Names`` is a string. podman: ``Names`` is a list."""
    names = entry.get("Names") or entry.get("names") or entry.get("Name") or ""
    if isinstance(names, list):
        return str(names[0]).lstrip("/") if names else ""
    return str(names).split(",")[0].lstrip("/")


def _state_of(entry: dict[str, Any]) -> tuple[str, str]:
    """Return ``(state, status)``; podman's ``State`` is already the bare word."""
    status = str(entry.get("Status") or entry.get("status") or "")
    state = entry.get("State") or entry.get("state") or ""
    if isinstance(state, dict):  # very old podman
        state = state.get("Status", "")
    state = str(state).lower()
    if not state or state not in {"running", "exited", "created", "paused", "restarting", "dead", "removing", "stopped"}:
        lowered = status.lower()
        if lowered.startswith("up"):
            state = "running"
        elif lowered.startswith("exited"):
            state = "exited"
        elif lowered.startswith("created"):
            state = "created"
        elif lowered.startswith("restarting"):
            state = "restarting"
        else:
            state = state or "unknown"
    if state == "stopped":
        state = "exited"
    return state, status


def _parse_inspect(text: str) -> dict[str, dict[str, Any]]:
    """TSV from ``inspect --format`` keyed by container name."""
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 8:
            continue
        name = fields[0].lstrip("/")
        out[name] = {
            "image": fields[1],
            "image_id": fields[2],
            "created": fields[3],
            "started_at": fields[4],
            "exit_code": _int(fields[5], 0),
            "restart_count": _int(fields[6], 0),
            "network_mode": fields[7],
            "log_path": fields[8] if len(fields) > 8 else "",
        }
    return out


def _parse_sites(text: str) -> list[SiteInfo]:
    """``name<TAB>port<TAB>ssl<TAB>source`` lines from the collector.

    A domain usually appears twice — once on 443 and once on the port 80 block that redirects to
    it. Only one of those is a place to send a browser, so https wins and the redirect is dropped.
    """
    best: dict[str, SiteInfo] = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 3 or not fields[0].strip():
            continue
        site = SiteInfo(
            name=fields[0].strip(),
            port=_int(fields[1], 443),
            ssl=fields[2].strip() == "1",
            source=fields[3].strip() if len(fields) > 3 else "",
        )
        current = best.get(site.name)
        if current is None or (site.ssl and not current.ssl):
            best[site.name] = site
    return sorted(best.values(), key=lambda s: s.name)


def parse_collect(
    raw: str,
    host: Host,
    *,
    elapsed: float = 0.0,
    previous: HostSnapshot | None = None,
    memory_s: float = VANISHED_MEMORY_S,
) -> HostSnapshot:
    """Parse one collector document. Raises ValueError when it is not usable JSON.

    ``memory_s`` is how long a workload that left is still reported as gone.
    """
    snapshot = HostSnapshot(alias=host.alias, env=host.env, at=time.time(), elapsed=elapsed, raw=raw)

    text = raw.strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("collector produced no JSON")
    document = json.loads(text[start:])

    snapshot.reachable = True
    snapshot.hostname = str(document.get("host") or "")
    snapshot.kernel = str(document.get("kernel") or "")
    snapshot.remote_epoch = _int(document.get("now"), 0) or None
    snapshot.uptime_s = _int(document.get("uptime_s"))
    load = document.get("load") or [0, 0, 0]
    snapshot.load = (_float(load[0]), _float(load[1] if len(load) > 1 else 0), _float(load[2] if len(load) > 2 else 0))
    snapshot.cpus = max(_int(document.get("cpus"), 1), 1)
    snapshot.mem_total = _int(document.get("mem_total"))
    snapshot.mem_avail = _int(document.get("mem_avail"))
    snapshot.disks = [
        DiskInfo(mount=str(d.get("mount")), size=_int(d.get("size")), used=_int(d.get("used")), pct=_int(d.get("pct")))
        for d in document.get("disks") or []
    ]
    snapshot.runtime = str(document.get("runtime") or "none")
    snapshot.sudo_ok = bool(document.get("sudo_ok", 1))
    snapshot.runtime_error = str(document.get("runtime_error") or "")
    snapshot.listening = [_int(p) for p in document.get("listening") or []]
    snapshot.probes = [
        ProbeResult(port=_int(p.get("port")), path=str(p.get("path") or "/"), code=str(p.get("code") or ""), at=snapshot.at)
        for p in document.get("probes") or []
    ]
    snapshot.frontends = list(document.get("frontends") or [])
    snapshot.nginx = str(document.get("nginx") or "")

    inspected = _parse_inspect(str(document.get("inspect") or ""))
    containers: list[ContainerInfo] = []
    for entry in document.get("containers") or []:
        if not isinstance(entry, dict):
            continue
        name = _name_of(entry)
        if not name:
            continue
        state, status = _state_of(entry)
        details = inspected.get(name, {})
        containers.append(
            ContainerInfo(
                name=name,
                image=str(details.get("image") or entry.get("Image") or entry.get("image") or ""),
                image_id=str(details.get("image_id") or entry.get("ImageID") or ""),
                state=state,
                status=status,
                created=str(details.get("created") or entry.get("CreatedAt") or entry.get("Created") or ""),
                started_at=str(details.get("started_at") or ""),
                exit_code=details.get("exit_code"),
                restart_count=_int(details.get("restart_count")),
                network_mode=str(details.get("network_mode") or entry.get("Networks") or ""),
                ports=_ports_to_text(entry.get("Ports") or entry.get("ports")),
                log_path=str(details.get("log_path") or ""),
                ignored=host.ignores(name),
            )
        )
    snapshot.sites = _parse_sites(str(document.get("sites") or ""))
    snapshot.containers = sorted(containers, key=lambda c: c.name)

    # The filter decides what this host is watched for, everywhere alike: what is listed, what is
    # counted, what is remembered. ``ps -a`` reports stopped workloads too, and a stopped workload
    # is still present — that it is not running is a separate, status problem.
    snapshot.containers = [c for c in snapshot.containers if host.watches(c.name)]
    present = {c.name for c in snapshot.containers}
    snapshot.expected_count = host.expected_count

    # Memory, in two parts, because a fleet that runs one-shot jobs would otherwise drown the
    # real losses in the names of finished jobs. No naming convention is involved in either part,
    # so this works for any fleet.
    #
    # First: only an **established** workload is remembered — one running now that was running on
    # the previous sweep too. Something that appears and is gone again inside one refresh interval
    # was a job, and a job that ends is not a loss. A job that outlives a refresh — Airbyte's sync
    # containers run for minutes — would slip through, which is what the ignore globs are for: a
    # workload the operator has named as churn is never remembered in the first place.
    running_now = {c.name for c in snapshot.containers if c.running and not c.ignored}
    was_running = {c.name for c in previous.containers if c.running} if previous is not None else set()
    snapshot.established = sorted(running_now & was_running)

    # Second: the memory ages out. A workload that left is reported while the news is fresh; after
    # that its absence is the host's history, not an alarm that never clears. ``expected_count`` is
    # the durable contract for "this host should have N workloads" — this is only the heuristic.
    remembered = set(previous.established) if previous is not None and previous.reachable else set()
    carried = dict(previous.vanished_since) if previous is not None and previous.reachable else {}

    since: dict[str, float] = {}
    for name in (remembered | set(carried)) - present:
        if not host.watches(name) or host.ignores(name):
            continue
        first_missed = carried.get(name, snapshot.at)
        if snapshot.at - first_missed <= max(memory_s, 0):
            since[name] = first_missed
    snapshot.vanished_since = since
    snapshot.vanished_containers = sorted(since)
    return snapshot
