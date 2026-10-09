"""Snapshot objects produced by one collector pass."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRIT = "crit"


@dataclass(slots=True)
class DiskInfo:
    mount: str
    size: int
    used: int
    pct: int

    @property
    def free(self) -> int:
        return max(self.size - self.used, 0)


@dataclass(slots=True)
class ProbeResult:
    port: int
    path: str
    code: str
    at: float = 0.0  # when this probe actually ran; older than the snapshot when it was carried forward

    @property
    def ok(self) -> bool:
        return self.code.startswith("2") or self.code.startswith("3") or self.code == "open"


@dataclass(slots=True)
class ContainerInfo:
    name: str
    image: str = ""
    image_id: str = ""
    state: str = "unknown"  # running | exited | created | paused | restarting | dead
    status: str = ""  # human string from ps ("Up 3 days")
    created: str = ""
    started_at: str = ""
    exit_code: int | None = None
    restart_count: int = 0
    network_mode: str = ""
    ports: str = ""
    log_path: str = ""
    log_bytes: int | None = None
    cpu_pct: float | None = None
    mem_bytes: int | None = None
    #: Matched one of the host's ignore globs: still listed, never alerted on.
    ignored: bool = False

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def host_network(self) -> bool:
        return self.network_mode == "host"


@dataclass(slots=True)
class SiteInfo:
    """A domain this host serves, read out of a web server's own configuration."""

    name: str
    port: int = 443
    ssl: bool = True
    #: Which web server named it: nginx, apache, caddy, traefik, haproxy.
    source: str = ""

    @property
    def url(self) -> str:
        scheme = "https" if self.ssl else "http"
        default = 443 if self.ssl else 80
        return f"{scheme}://{self.name}" + ("" if self.port == default else f":{self.port}")


@dataclass(slots=True)
class Alert:
    code: str
    severity: Severity
    message: str
    host: str
    container: str | None = None

    @property
    def key(self) -> str:
        return f"{self.host}:{self.container or '-'}:{self.code}"


@dataclass(slots=True)
class HostSnapshot:
    """Everything one collector round-trip produced, plus what the tool derived from it."""

    alias: str
    env: str
    at: float = 0.0  # local monotonic-ish wall clock (time.time) when the snapshot completed
    reachable: bool = False
    error_kind: str | None = None
    error_message: str = ""
    hostname: str = ""
    kernel: str = ""
    remote_epoch: int | None = None
    uptime_s: int = 0
    load: tuple[float, float, float] = (0.0, 0.0, 0.0)
    cpus: int = 1
    mem_total: int = 0
    mem_avail: int = 0
    disks: list[DiskInfo] = field(default_factory=list)
    runtime: str = "none"
    sudo_ok: bool = True
    runtime_error: str = ""
    containers: list[ContainerInfo] = field(default_factory=list)
    listening: list[int] = field(default_factory=list)
    probes: list[ProbeResult] = field(default_factory=list)
    frontends: list[dict[str, Any]] = field(default_factory=list)
    nginx: str = ""
    #: Domains this host serves. Discovered on demand, carried forward in between.
    sites: list[SiteInfo] = field(default_factory=list)
    missing_containers: list[str] = field(default_factory=list)
    #: Containers seen on this host before and gone entirely now (``ps -a`` no longer lists them).
    vanished_containers: list[str] = field(default_factory=list)
    #: Workloads running now that were also running last sweep. Only these are remembered: one
    #: that comes and goes inside a single refresh interval is a job, not something that left.
    established: list[str] = field(default_factory=list)
    #: When each vanished workload was first noticed missing, so the memory can age out instead
    #: of keeping every short-lived job's name forever.
    vanished_since: dict[str, float] = field(default_factory=dict)
    #: A fixed number the operator set for this host, or None.
    expected_count: int | None = None
    alerts: list[Alert] = field(default_factory=list)
    elapsed: float = 0.0
    raw: str = ""

    # ------------------------------------------------------------- derived

    @property
    def mem_used_pct(self) -> int:
        if not self.mem_total:
            return 0
        return round((self.mem_total - self.mem_avail) * 100 / self.mem_total)

    @property
    def load_per_cpu(self) -> float:
        return self.load[0] / max(self.cpus, 1)

    @property
    def worst_disk(self) -> DiskInfo | None:
        return max(self.disks, key=lambda d: d.pct) if self.disks else None

    @property
    def running_containers(self) -> list[ContainerInfo]:
        return [c for c in self.containers if c.running]

    @property
    def tracked_containers(self) -> list[ContainerInfo]:
        """The workloads this host is held to. A job runner's throwaway containers are not among
        them, so they neither raise alerts nor pad the count the host is measured against."""
        return [c for c in self.containers if not c.ignored]

    @property
    def expected_total(self) -> int:
        """How many workloads should be here: the set number, or what is present plus what is gone."""
        if self.expected_count is not None:
            return self.expected_count
        return len(self.tracked_containers) + len(self.vanished_containers)

    @property
    def clock_skew_s(self) -> float | None:
        if self.remote_epoch is None or not self.at:
            return None
        return self.remote_epoch - self.at

    @property
    def severity(self) -> Severity | None:
        if not self.alerts:
            return None
        if any(a.severity is Severity.CRIT for a in self.alerts):
            return Severity.CRIT
        if any(a.severity is Severity.WARN for a in self.alerts):
            return Severity.WARN
        return Severity.INFO

    def container(self, name: str) -> ContainerInfo | None:
        return next((c for c in self.containers if c.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "env": self.env,
            "at": self.at,
            "reachable": self.reachable,
            "error": {"kind": self.error_kind, "message": self.error_message} if self.error_kind else None,
            "hostname": self.hostname,
            "kernel": self.kernel,
            "uptime_s": self.uptime_s,
            "load": list(self.load),
            "cpus": self.cpus,
            "mem": {"total": self.mem_total, "available": self.mem_avail, "used_pct": self.mem_used_pct},
            "disks": [{"mount": d.mount, "size": d.size, "used": d.used, "pct": d.pct} for d in self.disks],
            "runtime": self.runtime,
            "sudo_ok": self.sudo_ok,
            "containers": [
                {
                    "name": c.name,
                    "image": c.image,
                    "state": c.state,
                    "status": c.status,
                    "restart_count": c.restart_count,
                    "exit_code": c.exit_code,
                    "network_mode": c.network_mode,
                    "ports": c.ports,
                    "log_bytes": c.log_bytes,
                    "ignored": c.ignored,
                }
                for c in self.containers
            ],
            "missing_containers": self.missing_containers,
            "vanished_containers": self.vanished_containers,
            "established": self.established,
            "vanished_since": self.vanished_since,
            "expected_count": self.expected_count,
            "listening": self.listening,
            "probes": [{"port": p.port, "path": p.path, "code": p.code, "ok": p.ok, "at": p.at} for p in self.probes],
            "frontends": self.frontends,
            "nginx": self.nginx,
            "sites": [{"name": s.name, "port": s.port, "ssl": s.ssl, "source": s.source, "url": s.url} for s in self.sites],
            "alerts": [{"code": a.code, "severity": a.severity.value, "message": a.message, "container": a.container} for a in self.alerts],
            "elapsed": round(self.elapsed, 3),
        }
