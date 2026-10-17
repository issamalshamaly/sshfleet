"""Typed configuration objects.

Parsing is tolerant: unknown keys are kept in ``extra`` so a newer config never crashes an older
binary, and every field has a default so a half-written file still loads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class HostRole(StrEnum):
    """What a host is for. The single definition; drives colouring and default log sources."""

    SERVICE = "service"
    GATEWAY = "gateway"
    FRONTEND = "frontend"
    DATA = "data"
    OPS = "ops"

    @classmethod
    def parse(cls, value: str | None) -> HostRole:
        """Unknown or missing means a plain service host, which is what most of them are."""
        try:
            return cls(str(value or "").lower())
        except ValueError:
            return cls.SERVICE


class ConfigError(Exception):
    """Raised for a config file that cannot be used (bad YAML, wrong shape, duplicate env)."""


def _expand(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


@dataclass(slots=True)
class ProjectCfg:
    """A named grouping of environments, and the outermost thing sshfleet knows about.

    It exists in its own right, so one is created before the environments that belong to it, and
    every environment belongs to exactly one. It carries one behaviour only: pausing it stops
    monitoring of every host in it. Nothing about how a host is reached comes from here.
    """

    name: str
    color: str = "gray"
    description: str = ""
    #: Monitoring stopped for every host of every environment in it, until it is resumed.
    paused: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProjectCfg:
        if not raw.get("name"):
            raise ConfigError("project entry is missing 'name'")
        return cls(
            name=str(raw["name"]),
            color=str(raw.get("color") or "gray"),
            description=str(raw.get("description") or ""),
            paused=bool(raw.get("paused")),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "color": self.color}
        if self.description:
            out["description"] = self.description
        if self.paused:  # written only while it is set
            out["paused"] = True
        return out


@dataclass(slots=True)
class EnvironmentCfg:
    name: str
    ssh_config_path: Path
    short: str = ""
    color: str = "cyan"
    key_path: Path | None = None
    alias_pattern: str = "auto"
    bastions: list[str] | str = "auto"
    #: The project this environment belongs to. Every environment has one; a file that predates
    #: projects, or one edited by hand, is repaired when it is loaded (see the loader).
    project: str = ""
    #: Monitoring stopped for every host in it, until it is resumed.
    paused: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EnvironmentCfg:
        if not raw.get("name"):
            raise ConfigError("environment entry is missing 'name'")
        if not raw.get("ssh_config_path"):
            raise ConfigError(f"environment {raw['name']!r} is missing 'ssh_config_path'")
        path = _expand(str(raw["ssh_config_path"]))
        assert path is not None
        return cls(
            name=str(raw["name"]),
            ssh_config_path=path,
            short=str(raw.get("short") or raw["name"][:4]),
            color=str(raw.get("color") or "cyan"),
            key_path=_expand(raw.get("key_path")),
            alias_pattern=str(raw.get("alias_pattern") or "auto"),
            bastions=raw.get("bastions", "auto"),
            project=str(raw.get("project") or ""),
            paused=bool(raw.get("paused")),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "short": self.short,
            "color": self.color,
            "ssh_config_path": str(self.ssh_config_path),
        }
        if self.key_path:
            out["key_path"] = str(self.key_path)
        if self.alias_pattern != "auto":
            out["alias_pattern"] = self.alias_pattern
        if self.bastions != "auto":
            out["bastions"] = list(self.bastions)
        if self.project:
            out["project"] = self.project
        if self.paused:  # written only while it is set
            out["paused"] = True
        return out


@dataclass(slots=True)
class ProbesCfg:
    #: Path probed on ports discovered from a container's published ports (the configured
    #: health endpoint of a service is always used when there is one).
    discovered_path: str = "/"


@dataclass(slots=True)
class FrontendsCfg:
    #: Where static sites live; each directory's mtime is reported as a deploy.
    web_root: str = "/var/www/html"


@dataclass(slots=True)
class HostOverride:
    display: str | None = None
    role: str | None = None
    runtime: str | None = None
    sudo: bool | None = None
    tags: list[str] = field(default_factory=list)
    #: Include this host's running workload stdout in the default log search.
    main: bool = False
    #: Short tag in merged log output; defaults to the alias with the environment prefix stripped.
    label: str | None = None
    #: Optional include filter over the workloads the runtime reports. Nothing more than a filter.
    workloads: str | None = None
    #: Format of workload stdout on this host; "auto" detects it per source.
    log_format: str = "auto"
    health: dict[str, Any] = field(default_factory=dict)
    web_root: str | None = None
    #: How many containers this host should have; None = learn from what is seen.
    expected_count: int | None = None
    #: Containers that were here once and are gone on purpose: never reported as vanished.
    ignored_containers: list[str] = field(default_factory=list)
    #: Seconds to wait for this host's connection, when the fleet-wide budget is not enough for
    #: it. A host whose login is slow — a wedged ``systemd-logind`` makes ``pam_systemd`` wait 25
    #: seconds before giving up — answers perfectly well, just late.
    connect_timeout_s: int | None = None
    #: Extra log files on this host: ``[{path, format?, rotated_glob?}]``.
    log_sources: list[dict[str, Any]] = field(default_factory=list)
    #: Monitoring stopped: sshfleet makes no connection to this host (or through it) until it is
    #: resumed. True or None, so the file only carries it while it is set.
    paused: bool | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HostOverride:
        return cls(
            display=raw.get("display"),
            role=raw.get("role"),
            runtime=raw.get("runtime"),
            sudo=raw.get("sudo"),
            tags=list(raw.get("tags") or []),
            main=bool(raw.get("main", False)),
            label=raw.get("label") or None,
            workloads=str(raw["workloads"]) if raw.get("workloads") else None,
            log_format=str(raw.get("log_format") or "auto"),
            health=_health(raw.get("health")),
            web_root=raw.get("web_root") or None,
            expected_count=int(raw["expected_count"]) if raw.get("expected_count") is not None else None,
            ignored_containers=[str(n) for n in (raw.get("ignored_containers") or [])],
            connect_timeout_s=int(raw["connect_timeout_s"]) if raw.get("connect_timeout_s") is not None else None,
            log_sources=[dict(s) for s in (raw.get("log_sources") or []) if isinstance(s, dict) and s.get("path")],
            paused=True if raw.get("paused") else None,
        )


def _health(raw: Any) -> dict[str, Any]:
    """The health endpoint, with its port made a number or refused.

    The port goes into a script on the host, inside ``bash -c "…/dev/tcp/127.0.0.1/PORT"`` when
    there is no curl, where anything but digits would be read as shell. The settings page always
    checked it; a hand-edited file or an imported bundle did not.
    """
    health = dict(raw or {})
    port = health.get("port")
    if port:
        if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
            raise ConfigError(f"health.port must be a port number from 1 to 65535, not {port!r}")
        health["port"] = int(port)
    return health


def _web_address(value: Any, tunnel: str) -> str | None:
    """A tunnel's "open in the browser" address: http or https, nothing a link could run."""
    if not value:
        return None
    text = str(value).strip()
    if not text.lower().startswith(("http://", "https://")):
        raise ConfigError(f"tunnel {tunnel!r}: open must be an http:// or https:// address, not {text!r}")
    return text


@dataclass(slots=True)
class TunnelCfg:
    name: str
    env: str
    via: str
    remote: str  # "host:port"
    local: int
    open_url: str | None = None

    @property
    def remote_host(self) -> str:
        return self.remote.rsplit(":", 1)[0]

    @property
    def remote_port(self) -> int:
        return int(self.remote.rsplit(":", 1)[1])

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> TunnelCfg:
        missing = [k for k in ("env", "via", "remote", "local") if k not in raw]
        if missing:
            raise ConfigError(f"tunnel {name!r} is missing {', '.join(missing)}")
        return cls(
            name=name,
            env=str(raw["env"]),
            via=str(raw["via"]),
            remote=str(raw["remote"]),
            local=int(raw["local"]),
            open_url=_web_address(raw.get("open"), name),
        )


@dataclass(slots=True)
class SshCfg:
    control_dir: str = "auto"
    control_persist: str = "10m"
    connect_timeout_s: int = 8
    server_alive_interval_s: int = 15
    strict_host_key_checking: str = "accept-new"
    compression: bool = True
    include_user_config: bool = False


@dataclass(slots=True)
class RefreshCfg:
    # Two minutes keeps 48 hosts current without a constant trickle of ssh traffic; staleness is
    # scaled to it so a host is never called stale merely because the next sweep has not run.
    fast_s: int = 120
    stats_s: int = 240
    stale_after_s: int = 300
    collector_timeout_s: int = 25
    connect_timeout_s: int = 10  # opening the connection; a dead host fails here, not at the collector timeout
    age_tick_s: int = 5


@dataclass(slots=True)
class ConcurrencyCfg:
    global_: int = 16
    per_bastion: int = 8
    heavy_logs_per_bastion: int = 6
    heavy_logs_global: int = 10


#: How long a workload that left ``ps -a`` is still reported as gone. The name memory is a
#: heuristic for noticing a change, not a contract — ``expected_count`` is the contract. Six hours
#: is long enough to see it and short enough that a finished job stops being an alarm.
VANISHED_MEMORY_S = 21600


@dataclass(slots=True)
class Thresholds:
    disk_warn_pct: int = 80
    disk_crit_pct: int = 90
    mem_warn_pct: int = 85
    mem_crit_pct: int = 95
    load_per_cpu_warn: float = 1.5
    container_log_warn_bytes: int = 2147483648
    restart_count_warn: int = 1
    vanished_memory_s: int = VANISHED_MEMORY_S


@dataclass(slots=True)
class FilesCfg:
    download_dir: str = "auto"
    deny_extra: list[str] = field(default_factory=list)
    preview_max_bytes: int = 65536


@dataclass(slots=True)
class LogsCfg:
    default_before: int = 60
    default_after: int = 0
    default_window: str = "30m"
    tail_lines: int = 50000
    max_bytes: int = 20971520
    max_line_bytes: int = 16384
    max_file_bytes: int = 209715200
    display_tz: str = "local"  # an IANA name, or "local" for the machine's own zone
    timeout_s: int = 600
    follow_max_sources: int = 12
    #: Extra directories (besides the built-in safe list) a log source may read from.
    allowed_paths: list[str] = field(default_factory=list)
    seq: dict[str, Any] = field(default_factory=dict)
    elastic: dict[str, Any] = field(default_factory=dict)


#: Config file layout version. 3 = there are no services; a host carries its own properties.
CONFIG_VERSION = 3


@dataclass(slots=True)
class Config:
    version: int = CONFIG_VERSION
    projects: list[ProjectCfg] = field(default_factory=list)
    environments: list[EnvironmentCfg] = field(default_factory=list)
    probes: ProbesCfg = field(default_factory=ProbesCfg)
    frontends: FrontendsCfg = field(default_factory=FrontendsCfg)
    hosts: dict[str, HostOverride] = field(default_factory=dict)
    refresh: RefreshCfg = field(default_factory=RefreshCfg)
    concurrency: ConcurrencyCfg = field(default_factory=ConcurrencyCfg)
    thresholds: Thresholds = field(default_factory=Thresholds)
    ssh: SshCfg = field(default_factory=SshCfg)
    tunnels: dict[str, TunnelCfg] = field(default_factory=dict)
    files: FilesCfg = field(default_factory=FilesCfg)
    ui: dict[str, Any] = field(default_factory=dict)
    logs: LogsCfg = field(default_factory=LogsCfg)
    extra: dict[str, Any] = field(default_factory=dict)

    def environment(self, name: str) -> EnvironmentCfg:
        for env in self.environments:
            if env.name == name or env.short == name:
                return env
        known = ", ".join(e.name for e in self.environments) or "none configured"
        raise ConfigError(f"unknown environment {name!r} (known: {known})")

    def project(self, name: str) -> ProjectCfg | None:
        """The declared record, or None when no project goes by that name."""
        return next((p for p in self.projects if p.name == name), None)

    def environments_of(self, project: str) -> list[EnvironmentCfg]:
        return [e for e in self.environments if e.project == project]

    def host_override(self, alias: str) -> HostOverride:
        return self.hosts.get(alias, HostOverride())
