"""Fleet refresh orchestration.

Refresh order is deliberate: bastions first (everything else rides on them), then the main
services, then the rest. Hosts behind a bastion that failed are marked without being contacted,
so one dead bastion costs one timeout instead of seventeen.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field

from .collector import HostSnapshot, collect, unreachable
from .collector import alerts as alerts_module
from .collector.model import Alert
from .context import Context
from .inventory import EnvScope, Host
from .ssh.errors import ErrorKind, SshError


@dataclass(slots=True)
class RefreshProgress:
    done: int
    total: int
    alias: str | None = None


@dataclass(slots=True)
class FleetState:
    snapshots: dict[str, HostSnapshot] = field(default_factory=dict)
    bastion_ok: dict[str, bool] = field(default_factory=dict)
    last_refresh: float = 0.0
    refreshing: bool = False

    def alerts(self, env: str | None = None) -> list[Alert]:
        out: list[Alert] = []
        for snapshot in self.snapshots.values():
            if env and snapshot.env != env:
                continue
            out.extend(snapshot.alerts)
        order = {"crit": 0, "warn": 1, "info": 2}
        return sorted(out, key=lambda a: (order.get(a.severity.value, 3), a.host))


class FleetService:
    """Owns the current view of the fleet and knows how to refresh it."""

    def __init__(self, context: Context) -> None:
        self.context = context
        self.state = FleetState()
        self._sort_cache: dict[str, int] = {}
        #: Hosts whose served domains have been read since this process started. A cached
        #: snapshot makes every host look familiar, so "have we asked yet" is the question, not
        #: "have we seen this host before".
        self._sites_read: set[str] = set()

    # ------------------------------------------------------------ selection

    def hosts(self, env: EnvScope = None) -> list[Host]:
        return self.context.inventory.hosts(env)

    def ordered(self, hosts: Sequence[Host]) -> list[Host]:
        """Bastions first — everything rides on them — then the hosts you starred, then the rest."""

        def rank(host: Host) -> tuple[int, str]:
            if host.is_bastion:
                return (0, host.alias)
            if host.main:
                return (1, host.alias)
            return (2, host.alias)

        return sorted(hosts, key=rank)

    # ------------------------------------------------------------- refresh

    async def refresh(
        self,
        env: EnvScope = None,
        *,
        on_update: Callable[[HostSnapshot], None] | None = None,
        on_progress: Callable[[RefreshProgress], None] | None = None,
    ) -> FleetState:
        """Refresh every host, streaming each snapshot to ``on_update`` as it lands."""
        async for _ in self.iter_refresh(env, on_update=on_update, on_progress=on_progress):
            pass
        return self.state

    async def iter_refresh(
        self,
        env: EnvScope = None,
        *,
        on_update: Callable[[HostSnapshot], None] | None = None,
        on_progress: Callable[[RefreshProgress], None] | None = None,
    ) -> AsyncIterator[HostSnapshot]:
        context = self.context
        in_scope = self.hosts(env)
        # A paused host is not contacted, and neither is one reached through it.
        for host in in_scope:
            if context.inventory.paused_by(host.alias) is not None:
                self.forget(host.alias)
        hosts = self.ordered([h for h in in_scope if context.inventory.paused_by(h.alias) is None])
        if not hosts:
            return

        self.state.refreshing = True
        try:
            bastion_results = await context.mux.warm_bastions(env)
            for alias, result in bastion_results.items():
                self.state.bastion_ok[alias] = result.ok

            down = {alias for alias, result in bastion_results.items() if not result.ok}
            reachable_hosts: list[Host] = []
            skipped: list[HostSnapshot] = []
            for host in hosts:
                if host.proxy_jump and host.proxy_jump in down:
                    message = bastion_results[host.proxy_jump].message or "bastion unreachable"
                    skipped.append(unreachable(host, "bastion_down", f"via {host.proxy_jump}: {message}"))
                else:
                    reachable_hosts.append(host)

            total = len(hosts)
            done = 0

            for snapshot in skipped:
                self._store(snapshot)
                done += 1
                if on_update:
                    on_update(snapshot)
                if on_progress:
                    on_progress(RefreshProgress(done, total, snapshot.alias))
                yield snapshot

            tasks = {
                asyncio.create_task(self._collect_guarded(host)): host for host in reachable_hosts
            }
            try:
                for finished in asyncio.as_completed(tasks):
                    snapshot = await finished
                    self._store(snapshot)
                    done += 1
                    if on_update:
                        on_update(snapshot)
                    if on_progress:
                        on_progress(RefreshProgress(done, total, snapshot.alias))
                    yield snapshot
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()

            self.state.last_refresh = time.time()
        finally:
            self.state.refreshing = False

    async def refresh_host(self, alias: str) -> HostSnapshot:
        """One host, asked for by hand — so it also re-reads what this host serves."""
        host = self.context.inventory.host(alias)
        reason = self.context.inventory.paused_reason(alias)
        if reason is not None:
            self.forget(alias)
            raise SshError(ErrorKind.PAUSED, alias, reason)
        snapshot = await self._collect_one(host, discover_sites=True)
        self._store(snapshot)
        return snapshot

    async def _collect_guarded(self, host: Host) -> HostSnapshot:
        """One host's check inside a sweep. A crash in it becomes that host's error: left to
        propagate, it ended the sweep and every host still in flight was cancelled with it."""
        try:
            return await self._collect_one(host)
        except Exception as exc:
            snapshot = unreachable(host, "unknown", f"check failed: {exc}")
            snapshot.alerts = alerts_module.evaluate(
                snapshot, host, self.context.config.thresholds, self.state.snapshots.get(host.alias)
            )
            return snapshot

    async def _collect_one(self, host: Host, *, discover_sites: bool = False) -> HostSnapshot:
        context = self.context
        previous = self.state.snapshots.get(host.alias)

        # Open the connection first, on a short deadline. A host whose sshd is gone sits in a
        # half-open TCP connect through the bastion; without this it would burn the whole
        # collector timeout and set the pace for the entire refresh.
        if host.alias not in context.mux.alive:
            warmed = await context.mux.warm(
                host.alias, timeout=host.connect_timeout_s or context.config.refresh.connect_timeout_s
            )
            if not warmed.ok:
                snapshot = unreachable(host, (warmed.kind.value if warmed.kind else "unknown"), warmed.message)
                snapshot.elapsed = warmed.elapsed
                snapshot.alerts = alerts_module.evaluate(snapshot, host, context.config.thresholds, previous)
                return snapshot

        # The domains a host serves change when someone edits a config file, not every two
        # minutes. Read them once per run, and whenever a check is asked for by hand; in between
        # the last answer is carried forward.
        reading_sites = discover_sites or host.alias not in self._sites_read
        snapshot = await collect(context.runner, host, context.config, previous, discover_sites=reading_sites)
        if reading_sites and snapshot.reachable:
            self._sites_read.add(host.alias)
        snapshot.alerts = alerts_module.evaluate(snapshot, host, context.config.thresholds, previous)
        return snapshot

    def _store(self, snapshot: HostSnapshot) -> None:
        # A check still in flight when its host was removed (or paused) lands after the reload that
        # forgot the host. Kept, it would bring back a server that is no longer in the fleet,
        # alerts and all, until the next restart.
        inventory = self.context.inventory
        if snapshot.alias not in inventory.by_alias or inventory.paused_by(snapshot.alias) is not None:
            return
        # A host that did not answer told us nothing about its containers. Its snapshot keeps what
        # was known — which workloads are established, which left and since when — so the check
        # after it can still say what has gone, instead of starting over from nothing.
        previous = self.state.snapshots.get(snapshot.alias)
        if not snapshot.reachable and previous is not None:
            snapshot.established = list(previous.established)
            snapshot.vanished_since = dict(previous.vanished_since)
        self.state.snapshots[snapshot.alias] = snapshot

    def forget(self, alias: str) -> None:
        """Drop what is known about a host — its snapshot, so its alerts go with it."""
        self.state.snapshots.pop(alias, None)
        self.state.bastion_ok.pop(alias, None)
        self._sites_read.discard(alias)

    # ----------------------------------------------------------- persistence

    def _state_file(self, env: str):
        return self.context.paths.fleet_state_dir / f"{env}.json"

    def save(self) -> None:
        """Cache the last known state so the next launch paints instantly."""
        by_env: dict[str, list[dict]] = {}
        known = self.context.inventory.by_alias
        for alias, snapshot in self.state.snapshots.items():
            if alias not in known:
                continue  # removed from the ssh config since; do not carry it forward
            by_env.setdefault(snapshot.env, []).append(snapshot.to_dict())
        self.context.paths.fleet_state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for env, payload in by_env.items():
            self._state_file(env).write_text(json.dumps({"saved_at": time.time(), "hosts": payload}))

    def load_cached(self) -> int:
        """Restore the cached snapshots (marked stale by their age, not by a flag)."""
        loaded = 0
        for environment in self.context.inventory.environments:
            path = self._state_file(environment.name)
            if not path.exists():
                continue
            try:
                document = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            for entry in document.get("hosts", []):
                alias = entry.get("alias")
                if not alias or alias in self.state.snapshots:
                    continue
                if alias not in self.context.inventory.by_alias:
                    # A host deleted from the ssh config while the tool was not running. Without
                    # this it would come back from the cache at every start and keep alerting
                    # about a server that no longer exists.
                    continue
                if self.context.inventory.paused_by(alias) is not None:
                    continue  # paused: its last state would only bring its old alerts back
                snapshot = _snapshot_from_dict(entry)
                if snapshot is not None:
                    self.state.snapshots[alias] = snapshot
                    loaded += 1
        return loaded

    # ------------------------------------------------------------- queries

    def snapshot(self, alias: str) -> HostSnapshot | None:
        return self.state.snapshots.get(alias)

    def age(self, alias: str) -> float | None:
        snapshot = self.state.snapshots.get(alias)
        return None if snapshot is None else time.time() - snapshot.at

    def is_stale(self, alias: str) -> bool:
        age = self.age(alias)
        return age is None or age > self.context.config.refresh.stale_after_s


def _snapshot_from_dict(entry: dict) -> HostSnapshot | None:
    """Rebuild the parts of a snapshot the fleet table needs; details are re-fetched."""
    from .collector.model import ContainerInfo, DiskInfo, ProbeResult, Severity, SiteInfo

    try:
        snapshot = HostSnapshot(alias=entry["alias"], env=entry.get("env", ""))
    except KeyError:
        return None
    snapshot.at = float(entry.get("at") or 0)
    snapshot.reachable = bool(entry.get("reachable"))
    error = entry.get("error") or None
    if error:
        snapshot.error_kind = error.get("kind")
        snapshot.error_message = error.get("message", "")
    snapshot.hostname = entry.get("hostname", "")
    snapshot.kernel = entry.get("kernel", "")
    snapshot.uptime_s = int(entry.get("uptime_s") or 0)
    load = entry.get("load") or [0, 0, 0]
    snapshot.load = (float(load[0]), float(load[1]), float(load[2]))
    snapshot.cpus = int(entry.get("cpus") or 1)
    memory = entry.get("mem") or {}
    snapshot.mem_total = int(memory.get("total") or 0)
    snapshot.mem_avail = int(memory.get("available") or 0)
    snapshot.disks = [DiskInfo(d["mount"], int(d["size"]), int(d["used"]), int(d["pct"])) for d in entry.get("disks") or []]
    snapshot.sites = [
        SiteInfo(
            name=str(x.get("name") or ""),
            port=int(x.get("port") or 443),
            ssl=bool(x.get("ssl")),
            source=str(x.get("source") or ""),
        )
        for x in entry.get("sites") or []
        if x.get("name")
    ]
    snapshot.established = [str(n) for n in entry.get("established") or []]
    # A cache written before the memory learned to age out has no timestamps. Starting its memory
    # fresh costs nothing: a workload that really is gone is noticed again on the next sweep.
    snapshot.vanished_since = {str(k): float(v) for k, v in (entry.get("vanished_since") or {}).items()}
    snapshot.runtime = entry.get("runtime", "none")
    snapshot.sudo_ok = bool(entry.get("sudo_ok", True))
    snapshot.containers = [
        ContainerInfo(
            name=c["name"],
            image=c.get("image", ""),
            state=c.get("state", "unknown"),
            status=c.get("status", ""),
            restart_count=int(c.get("restart_count") or 0),
            exit_code=c.get("exit_code"),
            network_mode=c.get("network_mode", ""),
            ports=c.get("ports", ""),
            log_bytes=c.get("log_bytes"),
            ignored=bool(c.get("ignored")),
        )
        for c in entry.get("containers") or []
    ]
    snapshot.missing_containers = list(entry.get("missing_containers") or [])
    snapshot.vanished_containers = list(entry.get("vanished_containers") or [])
    snapshot.expected_count = int(entry["expected_count"]) if entry.get("expected_count") is not None else None
    snapshot.listening = [int(p) for p in entry.get("listening") or []]
    snapshot.probes = [
        ProbeResult(int(p["port"]), p.get("path", "/"), p.get("code", ""), float(p.get("at") or 0))
        for p in entry.get("probes") or []
    ]
    snapshot.frontends = list(entry.get("frontends") or [])
    snapshot.nginx = entry.get("nginx", "")
    snapshot.alerts = [
        Alert(code=a["code"], severity=Severity(a.get("severity", "info")), message=a.get("message", ""), host=snapshot.alias, container=a.get("container"))
        for a in entry.get("alerts") or []
    ]
    return snapshot
