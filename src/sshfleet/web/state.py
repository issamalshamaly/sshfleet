"""Shared state for the web server: the fleet, the tunnels, a refresh loop and an event bus.

Browsers subscribe to the event bus over a streaming response; the refresh loop publishes one
event per host as it lands, so the page updates row by row instead of on a timer.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..bundle import StalePlan, apply_import, plan_import
from ..collector import transitions
from ..config import ConfigError
from ..config import loader as config_loader
from ..context import Context
from ..fleet import FleetService
from ..history import History, StateRecord, apply_restore, plan_restore, restored_reason, tracked_files
from ..inventory import EnvScope, PausedBy, build_inventory
from ..tunnels import TunnelManager, TunnelWatcher

QUEUE_SIZE = 2000

#: How often the registered SSH config files and config.yaml are stat-ed for changes. Cheap
#: enough (a handful of stat calls) to feel immediate without watching with inotify.
WATCH_INTERVAL_S = 2.0


class Broadcaster:
    """Fan-out of JSON events to every connected browser. Slow clients drop old events."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            self._offer(queue, event)

    @staticmethod
    def _offer(queue: asyncio.Queue, item: Any) -> None:
        """Enqueue without blocking; a lagging client loses its oldest event, never the newest."""
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(item)

    @property
    def clients(self) -> int:
        return len(self._subscribers)

    def close(self) -> None:
        """Tell every open stream to finish; used at shutdown."""
        # The sentinel must land even when a client is lagging with a full queue, otherwise
        # that stream never learns the server is going away.
        for queue in list(self._subscribers):
            self._offer(queue, None)


@dataclass(slots=True)
class Job:
    """A long-running action (a restart) whose progress the browser streams."""

    id: str
    kind: str
    target: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    result: dict[str, Any] | None = None
    started: float = field(default_factory=time.time)


class WebState:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.fleet = FleetService(context)
        self.tunnels = TunnelManager(context.config, context.mux, context.paths.tunnels_file)
        self.events = Broadcaster()
        self.tunnel_watch = TunnelWatcher(self.tunnels, self.events.publish, context.audit.append)
        self.token = secrets.token_urlsafe(24)
        self.jobs: dict[str, Job] = {}
        self._refresh_lock = asyncio.Lock()
        # One writer of the configuration at a time. Held by the task inside it, see config_lock.
        self._config_lock = asyncio.Lock()
        self._config_owner: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None
        self._previous_alerts: dict[str, list] = {}
        self.started_at = time.time()
        self._background: set[asyncio.Task] = set()
        self._watch_task: asyncio.Task | None = None
        self._tunnel_task: asyncio.Task | None = None
        self._fingerprints: dict[str, tuple[float, int]] = {}
        self._wake = asyncio.Event()
        #: Set when the server starts shutting down; see begin_shutdown.
        self.closing = False
        self._stream_waits: set[asyncio.Timeout] = set()
        self.last_reload: float = 0.0
        self.last_reload_reason: str = "startup"
        self.history = History(context.paths.history_dir)

    def spawn(self, coro) -> asyncio.Task:
        """Run a coroutine in the background, keeping a reference until it finishes."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self.fleet.load_cached()
        await self.record_state("server started", source="start")
        self._fingerprints = self._fingerprint()
        self._loop_task = asyncio.create_task(self._refresh_loop(), name="fleet-refresh-loop")
        self._watch_task = asyncio.create_task(self._watch_loop(), name="config-watch-loop")
        self._tunnel_task = asyncio.create_task(self.tunnel_watch.run(), name="tunnel-watch-loop")

    async def stop(self) -> None:
        self.events.close()
        for task in (self._loop_task, self._watch_task, self._tunnel_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    def begin_shutdown(self) -> None:
        """End every open stream now, before the server waits for open responses to finish.

        uvicorn runs the app's own shutdown (``stop``) only once every response has finished,
        and the page holds /api/events open for as long as it is open. A stream told to end
        from ``stop`` is told too late: each Ctrl+C sat out the whole grace period, and then
        uvicorn cancelled the stream and printed its traceback.
        """
        self.closing = True
        now = asyncio.get_running_loop().time()
        for wait in list(self._stream_waits):
            wait.reschedule(now)

    async def until_shutdown(self, stream: AsyncGenerator[bytes, None]) -> AsyncIterator[bytes]:
        """``stream``, ended between two chunks once the server starts shutting down.

        Only the wait for the next chunk is cut short, never a send, so the browser still gets a
        complete response, and the source is cancelled the same way a browser leaving cancels it.
        """
        try:
            while not self.closing:
                try:
                    async with asyncio.timeout(None) as wait:
                        self._stream_waits.add(wait)
                        try:
                            chunk = await anext(stream, None)
                        finally:
                            self._stream_waits.discard(wait)
                except TimeoutError:
                    if wait.expired():
                        return
                    raise  # the source's own timeout, not ours
                if chunk is None:
                    return
                yield chunk
        finally:
            await stream.aclose()

    async def _refresh_loop(self) -> None:
        # First pass right away so the page fills in; then on the configured interval, which the
        # settings page can change while this is running — the sleep waits on an event too, so a
        # new interval (or "manual only") takes effect at once instead of after the old one.
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - keep the loop alive
                self.events.publish({"type": "error", "message": f"refresh failed: {exc}"})
            await self._sleep_until_due()

    async def _sleep_until_due(self) -> None:
        self._wake.clear()
        interval = self.context.config.refresh.fast_s
        if interval <= 0:  # automatic refresh is off; wait for a settings change or a manual run
            await self._wake.wait()
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=interval)

    def wake(self) -> None:
        """Cut the current wait short (a changed interval, or a fresh inventory)."""
        self._wake.set()

    # ----------------------------------------------------------------- refresh

    async def refresh(self, env: EnvScope = None, alias: str | None = None) -> None:
        if alias:
            if self.context.inventory.paused_by(alias) is not None:
                self.fleet.forget(alias)  # nothing is asked of it; the page shows it paused
            else:
                await self.fleet.refresh_host(alias)
            self.events.publish({"type": "host", "host": self.host_payload(alias)})
            self._announce(alias)
            return

        if self._refresh_lock.locked():
            # One refresh at a time. Wait for the running one to finish so the caller sees
            # fresh data, but do not start a second sweep on its heels.
            async with self._refresh_lock:
                return
        async with self._refresh_lock:
            self.events.publish({"type": "refresh_started", "at": time.time()})

            def on_update(snapshot) -> None:
                self.events.publish({"type": "host", "host": self.host_payload(snapshot.alias)})
                self._announce(snapshot.alias)

            def on_progress(progress) -> None:
                self.events.publish({"type": "progress", "done": progress.done, "total": progress.total})

            async for _ in self.fleet.iter_refresh(env, on_update=on_update, on_progress=on_progress):
                pass
            self.fleet.save()
            self.events.publish(
                {
                    "type": "refresh_done",
                    "at": time.time(),
                    "bastions": self.fleet.state.bastion_ok,
                    "mux_alive": len(self.context.mux.alive),
                }
            )

    # ------------------------------------------------------------------ watching

    def watched_files(self) -> list[Path]:
        """config.yaml plus every SSH config file the inventory is built from — the same files
        history keeps, so what triggers a reload and what gets recorded never drift apart."""
        return [item.path for item in tracked_files(self.context.config, self.context.paths)]

    async def record_state(
        self,
        reason: str,
        *,
        source: str,
        valid: bool = True,
        error: str | None = None,
        restored_from: str | None = None,
    ) -> StateRecord | None:
        """Keep the configuration as it is on disk now in history (nothing is written when it has
        not changed). Never fails the change that called it: a history that cannot be written is
        reported and the change stands."""
        try:
            files = tracked_files(self.context.config, self.context.paths)
            state, _ = await asyncio.to_thread(
                self.history.record, files, reason=reason, source=source, valid=valid, error=error, restored_from=restored_from
            )
            return state
        except Exception as exc:
            self.events.publish({"type": "error", "message": f"history: could not record the configuration — {exc}"})
            return None

    def _fingerprint(self) -> dict[str, tuple[float, int]]:
        out: dict[str, tuple[float, int]] = {}
        for path in self.watched_files():
            try:
                info = path.stat()
                out[str(path)] = (info.st_mtime, info.st_size)
            except OSError:
                out[str(path)] = (0.0, 0)
        return out

    async def _watch_loop(self) -> None:
        """Notice edits made outside the tool, so nobody has to restart it.

        An editor often writes in stages, so a change has to look the same twice before the
        inventory is rebuilt.
        """
        while True:
            await asyncio.sleep(WATCH_INTERVAL_S)
            # A restore or an import writes several files: wait until it is done and has reloaded
            # itself, rather than catch it half-way and reload a mix of old and new.
            if self._config_lock.locked():
                continue
            try:
                current = self._fingerprint()
                if current == self._fingerprints:
                    continue
                await asyncio.sleep(0.4)
                settled = self._fingerprint()
                if settled != current:
                    continue  # still being written; look again next tick
                changed = [
                    Path(name).name
                    for name, value in settled.items()
                    if self._fingerprints.get(name) != value
                ]
                await self.reload(reason=f"{', '.join(changed[:3]) or 'config'} changed on disk", source="watcher")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - never let the watcher die
                self.events.publish({"type": "error", "message": f"config watch failed: {exc}"})

    @contextlib.asynccontextmanager
    async def config_lock(self) -> AsyncIterator[None]:
        """Hold the configuration still: no reload, restore or import runs while it is held.

        Two reloads interleaving at the await in the middle of one swapped in an inventory and
        then reported the other's changes. Re-entrant for the task holding it — a restore holds it
        across its writes and the reload that follows them — but not for tasks that task starts,
        which take their turn like anyone else.
        """
        task = asyncio.current_task()
        if task is not None and task is self._config_owner:
            yield
            return
        async with self._config_lock:
            self._config_owner = task
            try:
                yield
            finally:
                self._config_owner = None

    async def reload(
        self, *, reason: str = "manual", reload_config: bool = True, source: str = "web", restored_from: str | None = None
    ) -> dict[str, Any]:
        """Re-read config.yaml and the SSH config files and swap in the new inventory.

        The Context object is mutated rather than replaced: the fleet service, tunnels and every
        open request hold a reference to it. Whatever was read is then kept in history, the
        broken edits included — those most of all, since they are the ones to go back from.
        """
        async with self.config_lock():
            return await self._reload(reason=reason, reload_config=reload_config, source=source, restored_from=restored_from)

    async def _reload(
        self, *, reason: str, reload_config: bool, source: str = "web", restored_from: str | None = None
    ) -> dict[str, Any]:
        context = self.context
        before = {h.alias: (h.hostname, h.user, h.port, h.proxy_jump, h.env) for h in context.inventory.hosts()}
        paused_before = _paused(context.inventory)

        try:
            if reload_config:
                context.config = config_loader.load(context.paths.config_file)
                context.mux.config = context.config
            inventory = build_inventory(context.config)
        except (ConfigError, FileNotFoundError, OSError) as exc:
            self._fingerprints = self._fingerprint()
            message = str(exc)
            self.events.publish({"type": "error", "message": f"reload failed: {message}"})
            state = await self.record_state(reason, source=source, valid=False, error=message, restored_from=restored_from)
            return {"ok": False, "error": message, "state": state.id if state else None}

        context.inventory = inventory
        context.mux.inventory = inventory
        # Keep the startup warnings current: a file that has gone missing is reported, and one
        # that has come back stops being.
        context.warnings = [w for w in context.warnings if not w.startswith("environment ")] + list(inventory.warnings)
        for warning in inventory.warnings:
            self.events.publish({"type": "error", "message": warning})
        # Everything holding a reference to the old Config has to be handed the new one, or it
        # keeps answering from the settings that were replaced.
        self.tunnels.config = context.config
        if context.config.environments:
            context.mux.write_ssh_config()
        if context._runner is not None:
            context._runner.inventory = inventory

        after = {h.alias: (h.hostname, h.user, h.port, h.proxy_jump, h.env) for h in inventory.hosts()}
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(a for a in set(before) & set(after) if before[a] != after[a])

        for alias in removed:
            self.fleet.state.snapshots.pop(alias, None)
            self._previous_alerts.pop(alias, None)

        # Pausing takes effect here, whether it came from the page or from a hand edit of
        # config.yaml: what is open to the host is closed now, not at its next check. A host that
        # has only just appeared (added to a paused environment) has nothing open to close.
        paused_after = _paused(inventory)
        newly_paused = sorted((paused_after - paused_before) & set(before))
        resumed = sorted((paused_before - paused_after) & set(after))
        if newly_paused:
            await self._disconnect(newly_paused)
        for alias in resumed:
            context.audit.append("host.resume", env=inventory.host(alias).env, host=alias, status="ok")

        self._fingerprints = self._fingerprint()
        self.last_reload = time.time()
        self.last_reload_reason = reason
        state = await self.record_state(reason, source=source, restored_from=restored_from)
        previous = self.history.previous(state.id) if state else None
        summary = {
            "ok": True,
            "reason": reason,
            "added": added,
            "removed": removed,
            "changed": changed,
            "hosts": len(after),
            "warnings": list(inventory.warnings),
            "environments": [e.name for e in inventory.environments],
            "at": self.last_reload,
            # The state this reload left the configuration in, and the one before it: going back
            # to `previous_state` undoes whatever caused the reload.
            "state": state.id if state else None,
            "previous_state": previous.id if previous else None,
        }
        self.events.publish({"type": "inventory", **summary})
        for alias in sorted(set(added + changed + resumed)):
            self.spawn(self.refresh(alias=alias))
        self.wake()
        return summary

    @property
    def config_busy(self) -> bool:
        """A restore or an import is writing the configuration right now."""
        return self._config_lock.locked()

    async def restore(self, ref: str, *, only: set[str] | None = None) -> dict[str, Any]:
        """Put an earlier state of the configuration back, as one step nothing else can interleave
        with: checked first (see history.plan_restore), then the state before it recorded — so the
        restore itself can be undone — then the files written, then reloaded and recorded again.
        Raises HistoryError for an unknown state, RestoreRefused for one that would not load."""
        async with self.config_lock():
            files_now = tracked_files(self.context.config, self.context.paths)
            plan = await asyncio.to_thread(plan_restore, self.history, ref, files_now, only=only)
            target = plan.target
            before = await self.record_state(f"before restoring {target.code}", source="restore")
            written: list[str] = []
            summary: dict[str, Any] | None = None
            if plan.writes:
                written = await asyncio.to_thread(apply_restore, plan, self.context.paths)
                summary = await self.reload(
                    reason=restored_reason(target),
                    source="restore",
                    restored_from=target.id,
                )
            after = self.history.latest()
            self.context.audit.append(
                "config.restore",
                target=target.id,
                status="ok" if summary is None or summary.get("ok") else "failed",
                note=f"{len(written)} file(s) written" + (f"; undo by restoring {before.code}" if before and written else ""),
            )
            return {
                "ok": summary is None or bool(summary.get("ok")),
                "target": target.id,
                "before": before.id if before else None,
                "after": after.id if after else None,
                "written": written,
                "unchanged": plan.unchanged,
                "left_in_place": plan.left_in_place,
                "unavailable": plan.unavailable,
                "migrated": plan.migrated,
                "reload": summary,
            }

    async def catch_up(self) -> None:
        """Reload now if a watched file changed and the watcher has not got to it yet, so that a
        plan made from the running config is made from what is on disk."""
        if self._fingerprint() != self._fingerprints:
            await self.reload(reason="changed on disk", source="watcher")

    async def import_bundle(self, bundle: dict[str, Any], *, digest: str, **options: Any) -> dict[str, Any]:
        """Apply an import the operator has looked at: planned again under the config lock and
        refused (StalePlan) unless it is exactly the plan they saw. The state before is recorded
        first, so the import can be undone from history like any other change."""
        async with self.config_lock():
            await self.catch_up()
            plan = await asyncio.to_thread(plan_import, bundle, config=self.context.config, paths=self.context.paths, **options)
            if plan.digest != digest:
                raise StalePlan("the settings or the SSH files changed since the dry run; look at the import again")
            if not plan.changed:
                return {"ok": True, "written": [], "before": None, "after": None, "plan": plan.to_dict(), "reload": None}
            before = await self.record_state("before import", source="import")
            written = await asyncio.to_thread(apply_import, plan, self.context.paths)
            origin = bundle.get("manifest", {}).get("created_on") or "a bundle"
            summary = await self.reload(reason=f"imported from {origin}", source="import")
            after = self.history.latest()
            added = sum(1 for c in plan.changes if c.action == "add")
            updated = sum(1 for c in plan.changes if c.action == "update")
            self.context.audit.append(
                "config.import",
                target=origin,
                status="ok" if summary.get("ok") else "failed",
                note=f"{plan.mode}; {added} added, {updated} updated, {len(written)} file(s) written" + (f"; undo by restoring {before.code}" if before else ""),
            )
            return {
                "ok": bool(summary.get("ok")),
                "written": written,
                "before": before.id if before else None,
                "after": after.id if after else None,
                "plan": plan.to_dict(),
                "reload": summary,
            }

    async def _disconnect(self, aliases: list[str]) -> None:
        """Close everything open to hosts whose monitoring was just paused, and forget them.

        ``ssh -O exit`` speaks only to the local control socket — it opens no connection — and
        ends the master with every shell, log stream and forward riding on it. Tunnel records
        go first, so the watcher does not take those forwards for dropped and reopen them.
        """
        context = self.context
        self.tunnels.forget_via(set(aliases))
        for alias in aliases:
            await context.mux.stop(alias)
            self.fleet.forget(alias)
            for alert in self._previous_alerts.pop(alias, []):
                self.events.publish({"type": "alert", "state": "cleared", "alert": alert_payload(alert)})
            context.audit.append(
                "host.pause",
                env=context.inventory.host(alias).env,
                host=alias,
                status="ok",
                note=_pause_note(context.inventory.paused_by(alias), alias),
            )
        self.fleet.save()

    def _announce(self, alias: str) -> None:
        snapshot = self.fleet.snapshot(alias)
        if snapshot is None:
            return
        appeared, cleared = transitions(self._previous_alerts.get(alias, []), snapshot.alerts)
        self._previous_alerts[alias] = list(snapshot.alerts)
        for alert in appeared:
            self.events.publish({"type": "alert", "state": "appeared", "alert": alert_payload(alert)})
        for alert in cleared:
            self.events.publish({"type": "alert", "state": "cleared", "alert": alert_payload(alert)})

    # ------------------------------------------------------------------ views

    def host_payload(self, alias: str) -> dict[str, Any]:
        host = self.context.inventory.host(alias)
        snapshot = self.fleet.snapshot(alias)
        age = self.fleet.age(alias)
        return {
            "alias": host.alias,
            "env": host.env,
            "label": host.label,
            "hostname": host.hostname,
            "user": host.user,
            "via": host.proxy_jump,
            "is_bastion": host.is_bastion,
            "main": host.main,
            "role": host.role,
            "runtime": host.runtime,
            "sudo": host.sudo,
            "tags": host.tags,
            "expected_count": host.expected_count,
            "workloads": host.workload_glob,
            "ignored_containers": list(host.ignored_containers),
            "health_port": host.health_port,
            "health_path": host.health_path,
            "snapshot": snapshot.to_dict() if snapshot else None,
            "stale": self.fleet.is_stale(alias),
            "age_s": round(age, 1) if age is not None else None,
            # Its own ssh master is open, so views can count connections for what they show.
            "connected": alias in self.context.mux.alive,
            # What keeps this one out of reach: its project, its environment, itself, or a jump
            # host on its way — {"kind", "name"}, or None while it is watched.
            "paused_by": _paused_by_payload(self.context.inventory, alias),
        }

    def fleet_payload(self, env: str | None = None) -> dict[str, Any]:
        hosts = [self.host_payload(h.alias) for h in self.fleet.hosts(env)]
        return {
            "hosts": hosts,
            "refreshing": self.fleet.state.refreshing,
            "last_refresh": self.fleet.state.last_refresh,
            "bastions": self.fleet.state.bastion_ok,
            "mux_alive": len(self.context.mux.alive),
            "mux_total": len(self.context.inventory.hosts()),
            "alerts": [alert_payload(a) for a in self.fleet.state.alerts(env)],
        }

    def environments_payload(self) -> list[dict[str, Any]]:
        out = []
        for environment in self.context.inventory.environments:
            cfg = self.context.config.environment(environment.name)
            out.append(
                {
                    "name": environment.name,
                    "short": environment.short,
                    "color": cfg.color,
                    "project": cfg.project,
                    "hosts": len(environment.hosts),
                    "bastions": environment.bastions,
                    "production": "prod" in environment.name.lower(),
                    "paused": cfg.paused,  # its own flag; its project's is on the project
                }
            )
        return out

    # ------------------------------------------------------------------- jobs

    def new_job(self, kind: str, target: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, target=target)
        self.jobs[job.id] = job
        # Keep the map small: drop finished jobs older than an hour.
        cutoff = time.time() - 3600
        for stale_id in [j.id for j in self.jobs.values() if j.done and j.started < cutoff]:
            self.jobs.pop(stale_id, None)
        return job


def _paused(inventory) -> set[str]:
    """Every alias sshfleet may not connect to: paused itself, in a paused environment or
    project, or behind a paused jump host."""
    return {h.alias for h in inventory.hosts() if inventory.paused_by(h.alias) is not None}


def _paused_by_payload(inventory, alias: str) -> dict[str, str] | None:
    by = inventory.paused_by(alias)
    return by.to_dict() if by is not None else None


def _pause_note(by: PausedBy | None, alias: str) -> str:
    if by is None or (by.kind == "host" and by.name == alias):
        return "connection closed"
    if by.kind == "host":
        return f"connection closed; reached through paused {by.name}"
    return f"connection closed; {by.kind} {by.name} paused"


def alert_payload(alert) -> dict[str, Any]:
    return {
        "code": alert.code,
        "severity": alert.severity.value,
        "message": alert.message,
        "host": alert.host,
        "container": alert.container,
        "key": alert.key,
    }
