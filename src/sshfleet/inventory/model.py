"""Inventory objects: one ``Host`` per SSH alias, grouped into environments.

A host is the unit. What runs on it is discovered from the runtime and never declared; the only
thing the operator may say about its workloads is which of them to look at
(:attr:`Host.workload_glob`) and which never to raise an alert about
(:attr:`Host.ignored_containers`).
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

#: How wide a short log tag may be, so merged output lines up.
LABEL_WIDTH = 8

#: The first name segment that carries a number, with the separator before it. Everything from
#: there on is which run this was, not what it does.
_RUN_SEGMENT = re.compile(r"[-_][^-_]*\d[^-_]*")


def ignore_glob_for(name: str) -> str:
    """A glob covering the family of generated names a container belongs to.

    A job runner names a container after its task and then appends the run:
    ``destination-postgres-write-43138-0-nxulk`` is one of a family whose stable part ends where
    the numbers begin. Cutting there keeps what the container does and drops which run it was.
    A name with no numbered segment belongs to no family and stands for itself.
    """
    match = _RUN_SEGMENT.search(name)
    if not match or match.start() == 0:
        return name
    return name[: match.start() + 1] + "*"


def strip_env_prefix(alias: str, prefix: str | None) -> str:
    """``prod-order`` with prefix ``prod-`` -> ``order``."""
    if prefix and alias.startswith(prefix):
        return alias[len(prefix) :]
    return alias


@dataclass(slots=True)
class Host:
    alias: str
    env: str
    hostname: str | None = None
    user: str | None = None
    port: int | None = None
    proxy_jump: str | None = None
    is_bastion: bool = False
    role: str = "service"
    runtime: str | None = None  # docker | podman | none | None (probe on the host)
    sudo: bool = True
    display: str | None = None
    log_label: str = ""  # short tag in merged log output
    main: bool = False  # its running workload stdout joins the default log search
    workload_glob: str | None = None  # optional include filter over the discovered workloads
    log_format: str = "auto"
    tags: list[str] = field(default_factory=list)
    health_port: int | None = None
    health_path: str | None = None
    probe_min_interval_s: int = 0
    web_root: str | None = None  # where this host's static sites live, if it serves any
    expected_count: int | None = None  # the only count contract; None means there is none
    #: Name globs whose containers raise no alerts: gone on purpose, or a job runner's churn.
    ignored_containers: list[str] = field(default_factory=list)
    #: Seconds to wait for this host's connection; None means the fleet-wide budget.
    connect_timeout_s: int | None = None
    log_sources: list[dict] = field(default_factory=list)
    #: Monitoring stopped by the operator: nothing connects to it. See Inventory.paused_by.
    paused: bool = False
    source: Path | None = None

    @property
    def label(self) -> str:
        return self.display or self.alias

    @property
    def bastion(self) -> str:
        """The connection that carries this host — itself when it has no ProxyJump."""
        return self.proxy_jump or self.alias

    def watches(self, name: str) -> bool:
        """Is this workload one of the ones we look at? No filter means all of them."""
        return True if not self.workload_glob else fnmatch.fnmatch(name, self.workload_glob)

    def ignores(self, name: str) -> bool:
        """Is this workload one we never alert on?

        Entries are globs, because the containers worth ignoring are usually the ones nobody
        named: a job runner such as Airbyte spawns a container per task and gives each one a
        fresh name (``destination-postgres-write-43138-0-nxulk``), so ``destination-*`` is the
        only form of the rule that still matches tomorrow. A plain name is a glob that matches
        itself, so lists written before this stayed correct.
        """
        return any(fnmatch.fnmatch(name, pattern) for pattern in self.ignored_containers)

    def visible(self, names) -> list[str]:
        """The discovered names this host watches, in a stable order."""
        return sorted(n for n in names if self.watches(n))

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.alias} ({self.env})"


@dataclass(slots=True)
class Environment:
    name: str
    short: str
    color: str
    #: The project this environment is filed under, or None.
    project: str | None = None
    hosts: list[Host] = field(default_factory=list)
    bastions: list[str] = field(default_factory=list)
    alias_prefix: str | None = None
    key_path: Path | None = None
    ssh_config_path: Path | None = None


#: Which environments an operation covers: one (by name or short label), several (one project's,
#: say), or every one when None.
EnvScope = str | Collection[str] | None


def in_scope(environment: Environment, env: EnvScope) -> bool:
    # An empty collection (a project with no environments yet) covers nothing, not everything.
    if env is None or env == "":
        return True
    names = (env,) if isinstance(env, str) else env
    return environment.name in names or environment.short in names


@dataclass(slots=True)
class Inventory:
    environments: list[Environment] = field(default_factory=list)
    by_alias: dict[str, Host] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)  # environments whose file could not be read

    def hosts(self, env: EnvScope = None) -> list[Host]:
        return [h for e in self.environments if in_scope(e, env) for h in e.hosts]

    def host(self, alias: str) -> Host:
        try:
            return self.by_alias[alias]
        except KeyError:
            raise KeyError(f"unknown host alias {alias!r}") from None

    def environment(self, name: str) -> Environment:
        for env in self.environments:
            if env.name == name or env.short == name:
                return env
        known = ", ".join(e.name for e in self.environments) or "none"
        raise KeyError(f"unknown environment {name!r} (known: {known})")

    def bastions(self, env: EnvScope = None) -> list[str]:
        out: list[str] = []
        for environment in self.environments:
            if not in_scope(environment, env):
                continue
            for alias in environment.bastions:
                if alias not in out:
                    out.append(alias)
        return out

    def behind(self, bastion: str) -> list[Host]:
        return [h for h in self.hosts() if h.proxy_jump == bastion]

    def starred(self, env: str | None = None) -> list[Host]:
        """Hosts whose workload logs are in the default search (a paused one sits out)."""
        return [h for h in self.hosts(env) if h.main and self.paused_by(h.alias) is None]

    def paused_by(self, alias: str) -> str | None:
        """The paused host that keeps sshfleet from connecting to ``alias``: the host itself, or a
        jump host on its way, since reaching it would open a connection through that one.
        None when nothing is paused on the way (or the alias is unknown)."""
        seen: set[str] = set()
        current = self.by_alias.get(alias)
        while current is not None and current.alias not in seen:
            if current.paused:
                return current.alias
            seen.add(current.alias)
            current = self.by_alias.get(current.proxy_jump) if current.proxy_jump else None
        return None

    def paused_reason(self, alias: str) -> str | None:
        """Why sshfleet must not connect to ``alias``, or None when it may."""
        by = self.paused_by(alias)
        if by is None:
            return None
        if by == alias:
            return "monitoring is paused — sshfleet does not connect to it"
        return f"it connects through {by}, whose monitoring is paused"
