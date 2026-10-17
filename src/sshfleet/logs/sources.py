"""Work out what to search, from the hosts and what they are running.

Nothing here is declared. A host's workloads come from the last snapshot the collector took; the
only thing the operator says about them is which to watch (`Host.workloads`). Starring a host puts
its **running** workload stdout into the default merged search; its files, its nginx logs and its
journal stay available to pick by hand, and a stopped workload's log stays readable, because its
last lines are usually the reason it stopped.
"""

from __future__ import annotations

from os.path import commonprefix

from ..containers.names import check_container_name
from ..inventory import Host, Inventory
from .model import LogFormat, LogSource, SourceKind

NGINX_ROLES = {"frontend", "gateway"}


def _format(name: str | None) -> LogFormat:
    try:
        return LogFormat(name or "auto")
    except ValueError:
        return LogFormat.AUTO


def label_for(host: Host, workload: str | None = None, siblings: list[str] | None = None) -> str:
    """A short tag for merged output: the host's label, plus what tells this workload apart.

    Deterministic and stable whatever order the runtime listed the workloads in: the siblings are
    sorted, their shared prefix is dropped, and identical remainders are numbered.
    """
    base = host.log_label or host.alias
    others = sorted(siblings or [])
    if not workload or len(others) < 2:
        return base
    shared = commonprefix(others)
    tail = workload[len(shared) :].strip("-_.") or workload
    same = [name for name in others if (name[len(shared) :].strip("-_.") or name) == tail]
    if len(same) > 1:
        tail = f"{tail}{same.index(workload) + 1}"
    return f"{base}:{tail[:6]}"


def workload_sources(host: Host, snapshot=None, *, running_only: bool = False) -> list[LogSource]:
    """One stdout source per workload this host watches, from what the collector last saw."""
    containers = list(getattr(snapshot, "containers", None) or [])
    if running_only:
        containers = [c for c in containers if c.running]
    names = sorted(c.name for c in containers if host.watches(c.name))
    return [
        LogSource(
            id=f"{host.env}/{host.alias}/{name}",
            kind=SourceKind.CONTAINER_STDOUT,
            env=host.env,
            label=label_for(host, name, names),
            host_alias=host.alias,
            service=host.log_label or host.alias,
            container=name,
            log_format=_format(host.log_format),
        )
        for name in names
    ]


def host_file_source(
    host: Host,
    path: str,
    log_format: LogFormat = LogFormat.AUTO,
    *,
    rotated_glob: str | None = None,
    container: str | None = None,
) -> LogSource:
    """A file on the host, or inside one of its containers when ``container`` is given."""
    return LogSource(
        id=f"{host.env}/{host.alias}{path}",
        kind=SourceKind.CONTAINER_FILE if container else SourceKind.HOST_FILE,
        env=host.env,
        label=label_for(host),
        host_alias=host.alias,
        service=host.log_label or host.alias,
        container=container,
        path=path,
        rotated_glob=rotated_glob,
        log_format=log_format,
    )


def extra_file_sources(host: Host, snapshot=None) -> list[LogSource]:
    """The files the operator listed for this host under ``hosts.<alias>.log_sources``.

    An entry marked ``in_container`` is read inside the first workload the host watches, which is
    how an application that logs to a file inside its image is reached.
    """
    inside = None
    if any(entry.get("in_container") for entry in host.log_sources):
        candidates = sorted(c.name for c in (getattr(snapshot, "containers", None) or []) if host.watches(c.name))
        inside = candidates[0] if candidates else None
    out: list[LogSource] = []
    for entry in host.log_sources:
        path = str(entry.get("path") or "")
        if not path or (entry.get("in_container") and inside is None):
            continue
        out.append(
            host_file_source(
                host,
                path,
                _format(entry.get("format")),
                rotated_glob=entry.get("rotated_glob") or None,
                container=inside if entry.get("in_container") else None,
            )
        )
    return out


def nginx_sources(host: Host) -> list[LogSource]:
    label = label_for(host)
    return [
        LogSource(
            id=f"{host.env}/{host.alias}/nginx-error",
            kind=SourceKind.HOST_FILE,
            env=host.env,
            label=f"{label}/ngx",
            host_alias=host.alias,
            service=host.log_label or host.alias,
            path="/var/log/nginx/error.log",
            rotated_glob="error.log.*",
            log_format=LogFormat.NGINX_ERROR,
        ),
        LogSource(
            id=f"{host.env}/{host.alias}/nginx-access",
            kind=SourceKind.HOST_FILE,
            env=host.env,
            label=f"{label}/ngx",
            host_alias=host.alias,
            service=host.log_label or host.alias,
            path="/var/log/nginx/access.log",
            rotated_glob="access.log.*",
            log_format=LogFormat.NGINX_ACCESS,
        ),
    ]


def journal_source(host: Host, unit: str = "docker.service") -> LogSource:
    return LogSource(
        id=f"{host.env}/{host.alias}/journal:{unit}",
        kind=SourceKind.JOURNAL,
        env=host.env,
        label=f"{label_for(host)}/sys",
        host_alias=host.alias,
        service=host.log_label or host.alias,
        unit=unit,
        log_format=LogFormat.JOURNAL,
    )


def container_source_for(host: Host, container: str) -> LogSource:
    """A source for one named workload, whatever its state.

    The name arrives from a request, a command line or a config file, and it ends up in a remote
    command: anything that is not a container name (or a ``*`` wildcard for one) stops here.
    """
    check_container_name(container, glob=True)
    return LogSource(
        id=f"{host.env}/{host.alias}/{container}",
        kind=SourceKind.CONTAINER_STDOUT,
        env=host.env,
        label=label_for(host),
        host_alias=host.alias,
        service=host.log_label or host.alias,
        container=container,
        log_format=_format(host.log_format),
    )


def host_sources(host: Host, snapshot=None) -> list[LogSource]:
    """Everything worth reading on one host: its workloads (running or not), files, nginx, journal."""
    out = workload_sources(host, snapshot)
    out.extend(extra_file_sources(host, snapshot))
    if host.role in NGINX_ROLES or (snapshot is not None and snapshot.nginx == "active"):
        out.extend(nginx_sources(host))
    out.append(journal_source(host))
    seen: set[str] = set()
    unique = []
    for source in out:
        if source.id not in seen:
            seen.add(source.id)
            unique.append(source)
    return unique


def known_source_for(host: Host, wanted: str, snapshot=None) -> LogSource:
    """The source behind a picked id or path, with everything the picker showed still on it.

    What a picker hands back is a choice from a list this module produced, so it has to be
    *looked up* rather than rebuilt from its path. Rebuilding loses the four things that decide
    how a source is read: a journal has no path at all, an ``in_container`` file would be read
    from the host instead of inside the container, the format that drives the remote time filter
    would fall back to ``auto``, and the rotated glob that reaches yesterday's file would go.

    A path nobody listed is still honoured, because typing one is how an ad-hoc file is searched.
    """
    for source in host_sources(host, snapshot):
        if wanted and wanted in (source.id, source.path):
            return source
    if wanted.startswith("/"):
        return host_file_source(host, wanted)
    raise KeyError(f"{host.alias}: no log source {wanted!r}")


def main_sources(inventory: Inventory, env: str, snapshots=None, *, hosts: list[str] | None = None) -> list[LogSource]:
    """The default search: the starred hosts' running workload stdout, and nothing else."""
    wanted = set(hosts) if hosts else None
    out: list[LogSource] = []
    for host in inventory.hosts(env):
        if wanted is None and not host.main:
            continue
        if wanted is not None and host.alias not in wanted:
            continue
        snapshot = snapshots(host.alias) if callable(snapshots) else None
        out.extend(workload_sources(host, snapshot, running_only=True))
    return out


__all__ = [
    "NGINX_ROLES",
    "container_source_for",
    "extra_file_sources",
    "host_file_source",
    "host_sources",
    "journal_source",
    "known_source_for",
    "label_for",
    "main_sources",
    "nginx_sources",
    "workload_sources",
]


def describe_sources(sources: list[LogSource]) -> str:
    """For the audit trail: how many sources, and on which hosts."""
    hosts = sorted({s.host_alias for s in sources if s.host_alias})
    named = ", ".join(hosts[:8]) + (f" and {len(hosts) - 8} more" if len(hosts) > 8 else "")
    return f"{len(sources)} source(s)" + (f" on {named}" if named else "")
