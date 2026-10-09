"""Run a search across several sources at once and stream the merged result."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime

from ..config import Config
from ..inventory import Inventory
from ..ssh import SshRunner
from .merge import TimelineMerger
from .model import (
    Event,
    LogLine,
    LogSource,
    SearchDone,
    SearchRequest,
    SourceDone,
    SourceError,
    SourceProgress,
)
from .runner import SourceRunner

QUEUE_SIZE = 10_000


class SearchCoordinator:
    """Fans out to every source, merges the results, and reports what each one did."""

    def __init__(self, runner: SshRunner, inventory: Inventory, config: Config) -> None:
        self.runner = runner
        self.inventory = inventory
        self.config = config

    async def run(self, request: SearchRequest, *, merge: bool = True, now: datetime | None = None) -> AsyncIterator[Event]:
        window = request.window.resolve(now)
        started = time.monotonic()
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        merger = TimelineMerger()
        per_source: dict[str, SourceDone | SourceError] = {}
        totals = {"lines": 0, "matches": 0, "bytes": 0}

        connect_timeout = self.config.refresh.connect_timeout_s

        async def drive(source: LogSource) -> None:
            try:
                host = self.inventory.host(source.host_alias or "")
            except KeyError:
                await queue.put(SourceError(source.id, source.label, "unknown", f"no such host {source.host_alias}"))
                return

            # Open the connection on a short deadline first. A host whose sshd is not answering
            # would otherwise hold the search open for the full remote timeout.
            if host.alias not in self.runner.mux.alive:
                warmed = await self.runner.mux.warm(host.alias, timeout=connect_timeout)
                if not warmed.ok:
                    await queue.put(
                        SourceError(source.id, source.label, "ssh", f"{host.alias}: {warmed.message}")
                    )
                    return

            try:
                source_runner = SourceRunner(self.runner, host, source, request, window)
            except Exception as exc:  # command policy refused, bad path, …
                await queue.put(SourceError(source.id, source.label, "denied", str(exc)))
                return
            try:
                async for event in source_runner.run():
                    await queue.put(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = str(exc) or f"{type(exc).__name__} while reading {source.id}"
                await queue.put(SourceError(source.id, source.label, "ssh", detail))

        tasks = []
        for source in request.sources:
            merger.register(source.id)
            tasks.append(asyncio.create_task(drive(source)))

        if not tasks:
            yield SearchDone(window, 0, 0, 0, 0.0, {})
            return

        async def watch() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)
            await queue.put(None)

        watcher = asyncio.create_task(watch())

        try:
            while True:
                event = await queue.get()
                if event is None:
                    break

                if isinstance(event, LogLine):
                    totals["lines"] += 1
                    if event.is_match:
                        totals["matches"] += 1
                    if merge:
                        merger.push(event)
                        for line in merger.drain():
                            yield line
                    else:
                        yield event
                elif isinstance(event, SourceProgress):
                    yield event
                elif isinstance(event, (SourceDone, SourceError)):
                    per_source[event.source_id] = event
                    if isinstance(event, SourceDone):
                        totals["bytes"] += event.bytes
                    merger.finish(event.source_id)
                    if merge:
                        for line in merger.drain():
                            yield line
                    yield event

            if merge:
                for line in merger.flush():
                    yield line
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            watcher.cancel()
            await asyncio.gather(*tasks, watcher, return_exceptions=True)

        yield SearchDone(
            window=window,
            lines=totals["lines"],
            matches=totals["matches"],
            bytes=totals["bytes"],
            elapsed=time.monotonic() - started,
            per_source=per_source,
            out_of_order=merger.out_of_order,
        )
