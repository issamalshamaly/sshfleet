"""Send the collector script to a host and parse what comes back."""

from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path

from ..config import Config
from ..inventory import Host
from ..ssh import ErrorKind, SshRunner
from .model import HostSnapshot
from .parse import parse_collect

SCRIPTS = Path(__file__).parent / "scripts"


@lru_cache(maxsize=4)
def script(name: str) -> str:
    return (SCRIPTS / name).read_text()


#: Ports that speak their own protocol; an HTTP probe against them is meaningless.
NON_HTTP_PORTS = frozenset({22, 25, 53, 5432, 6379, 27017, 5672, 15672, 9300, 11211, 3306})


def probe_specs(host: Host, previous: HostSnapshot | None, config: Config) -> list[str]:
    """Which ``port:path`` pairs the remote script should curl.

    The configured endpoint comes first and is the one alerts are based on. After it come ports
    published by workloads this host watches and that are running — a stopped one answers nothing,
    and a filtered-out one is none of our business. Databases and brokers are never probed,
    because a red probe on Redis says nothing. The ignore list plays no part here: it silences a
    disappearance, it does not hide a running workload.
    """
    specs: list[str] = []
    seen: set[int] = set()

    def add(port: int | None, path: str | None) -> None:
        if not port:
            return
        port = int(port)  # it is written into the script's arguments
        if port in seen or port in NON_HTTP_PORTS:
            return
        seen.add(port)
        specs.append(f"{port}:{path or '/'}")

    add(host.health_port, host.health_path)

    if previous is not None:
        for container in previous.running_containers:
            if container.host_network or not host.watches(container.name):
                continue
            for mapping in container.ports.split(","):
                mapping = mapping.strip()
                if "->" not in mapping:
                    continue
                port_text = mapping.split("->", 1)[0].rsplit(":", 1)[-1]
                try:
                    add(int(port_text), config.probes.discovered_path)
                except ValueError:
                    continue
    return specs


def should_probe(host: Host, previous: HostSnapshot | None) -> bool:
    """The wallet's /health logs a noisy block per call, so it is rate-limited."""
    if host.probe_min_interval_s <= 0 or previous is None:
        return True
    return (time.time() - previous.at) >= host.probe_min_interval_s


async def collect(
    runner: SshRunner,
    host: Host,
    config: Config,
    previous: HostSnapshot | None = None,
    discover_sites: bool = False,
) -> HostSnapshot:
    """One round-trip. Never raises: an unreachable host comes back as an unreachable snapshot.

    ``discover_sites`` reads the web servers' configs for the domains this host serves. It costs
    about 15ms of remote work and the answer changes only when someone edits those files, so it
    is asked for at startup and on a manual check rather than on every sweep.
    """
    probing = should_probe(host, previous)
    specs = probe_specs(host, previous, config) if probing else []
    runtime = host.runtime or "auto"
    use_sudo = "1" if host.sudo else "0"

    started = time.time()
    result = await runner.run_script(
        host,
        script("collect.sh"),
        [
            " ".join(specs),
            runtime,
            use_sudo,
            host.web_root or config.frontends.web_root,
            "1" if discover_sites else "0",
        ],
        timeout=config.refresh.collector_timeout_s,
    )
    elapsed = time.time() - started

    if not result.ok:
        snapshot = HostSnapshot(alias=host.alias, env=host.env, at=time.time(), elapsed=elapsed)
        snapshot.reachable = False
        snapshot.error_kind = result.kind.value if result.kind else ErrorKind.UNKNOWN.value
        error = result.error()
        snapshot.error_message = error.message if error else "failed"
        return snapshot

    try:
        snapshot = parse_collect(
            result.out, host, elapsed=elapsed, previous=previous,
            memory_s=config.thresholds.vanished_memory_s,
        )
        if not discover_sites and previous is not None and not snapshot.sites:
            snapshot.sites = list(previous.sites)  # not asked for this time; the last answer stands
        if not probing and previous is not None:
            # The probe was skipped to keep a chatty endpoint quiet, so show the last answer
            # rather than an empty cell. Each result keeps its own timestamp, so the UI can say
            # how old it is instead of passing a stale reading off as current.
            snapshot.probes = list(previous.probes)
        return snapshot
    except (ValueError, KeyError) as exc:
        snapshot = HostSnapshot(alias=host.alias, env=host.env, at=time.time(), elapsed=elapsed, raw=result.out)
        snapshot.reachable = False
        snapshot.error_kind = "bad_output"
        snapshot.error_message = f"collector output could not be parsed: {exc}"
        return snapshot


def unreachable(host: Host, kind: str, message: str) -> HostSnapshot:
    """A snapshot for a host we deliberately did not contact (its bastion is down)."""
    snapshot = HostSnapshot(alias=host.alias, env=host.env, at=time.time())
    snapshot.reachable = False
    snapshot.error_kind = kind
    snapshot.error_message = message
    return snapshot
