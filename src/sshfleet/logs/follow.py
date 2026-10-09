"""Follow several containers at once.

Every source gets a bounded queue. When one floods, its oldest lines are dropped and counted
rather than blocking: the remote side must keep draining or ``docker logs -f`` stalls, and a
stalled follow is worse than a gap you can see.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

from ..inventory import Inventory
from ..ssh import SshRunner
from ..ssh import proc as proc_module
from . import commands, parsers
from .grouper import EntryGrouper
from .model import LogLine, LogSource, SourceError

QUEUE_PER_SOURCE = 2000
PUMP_INTERVAL = 0.08
BATCH = 500
RECONNECT_DELAY = 2.0
MISSING_RETRY = 30.0


async def _read_marker(stream: asyncio.StreamReader | None) -> str:
    """Drain stderr and return the error marker the pipeline printed, if any."""
    if stream is None:
        return ""
    text = (await stream.read()).decode(errors="replace")
    if commands.ERROR_MARKER in text:
        return text.split(commands.ERROR_MARKER, 1)[1].strip().split("\n")[0]
    return ""


class FollowSession:
    """Streams several containers into one merged view until cancelled."""

    def __init__(
        self,
        runner: SshRunner,
        inventory: Inventory,
        sources: list[LogSource],
        *,
        pattern: str = "",
        since: str = "1m",
    ) -> None:
        self.runner = runner
        self.inventory = inventory
        self.sources = sources
        self.pattern = pattern
        self.since = since
        self.queues: dict[str, asyncio.Queue] = {s.id: asyncio.Queue(maxsize=QUEUE_PER_SOURCE) for s in sources}
        self.dropped: dict[str, int] = dict.fromkeys(self.queues, 0)
        self.paused = False
        self._last_ts: dict[str, int] = {}

    def _put(self, source_id: str, item) -> None:
        queue = self.queues[source_id]
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
                self.dropped[source_id] += 1
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(item)

    async def _drive(self, source: LogSource) -> None:
        grouper = EntryGrouper(source.log_format)
        since = self.since
        while True:
            try:
                host = self.inventory.host(source.host_alias or "")
            except KeyError:
                self._put(source.id, SourceError(source.id, source.label, "unknown", "no such host"))
                return

            pipeline = commands.follow_pipeline(host, source, self.pattern, since=since)
            try:
                proc, slot = await self.runner.spawn(host, "LC_ALL=C bash -s", heavy=False)
            except Exception as exc:
                self._put(source.id, SourceError(source.id, source.label, "ssh", str(exc)))
                await asyncio.sleep(RECONNECT_DELAY)
                continue

            saw_error = ""
            try:
                assert proc.stdout is not None
                await proc_module.feed_stdin(proc, pipeline.script.encode())

                stderr_task = asyncio.create_task(_read_marker(proc.stderr))
                buffer = b""
                seq = 0
                while True:
                    chunk = await proc.stdout.read(16384)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        text = parsers.sanitize(raw.decode("utf-8", errors="replace"))
                        if not text.strip():
                            continue
                        ts, body = parsers.split_docker_prefix(text)
                        line = grouper.feed(body if ts is not None else text, ts)
                        line.source_id = source.id
                        line.label = source.label
                        line.seq = seq
                        seq += 1
                        if line.ts_ns:
                            self._last_ts[source.id] = line.ts_ns
                        self._put(source.id, line)
                with contextlib.suppress(Exception):
                    saw_error = await asyncio.wait_for(stderr_task, timeout=2)
            except asyncio.CancelledError:
                raise
            finally:
                await proc_module.terminate(proc)
                await slot.__aexit__(None, None, None)

            if "missing_container" in saw_error:
                self._put(source.id, SourceError(source.id, source.label, "missing_container", "container is not running; retrying"))
                await asyncio.sleep(MISSING_RETRY)
            else:
                # The stream ended: the container was redeployed, or the link dropped. Resume
                # from the last line we saw so nothing is replayed twice.
                last = self._last_ts.get(source.id)
                if last:
                    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last / 1_000_000_000))
                await asyncio.sleep(RECONNECT_DELAY)

    async def run(self) -> AsyncIterator[LogLine | SourceError]:
        tasks = [asyncio.create_task(self._drive(source)) for source in self.sources]
        try:
            while True:
                await asyncio.sleep(PUMP_INTERVAL)
                if self.paused:
                    continue
                batch: list = []
                for queue in self.queues.values():
                    for _ in range(BATCH // max(len(self.queues), 1)):
                        try:
                            batch.append(queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                if not batch:
                    continue
                batch.sort(key=lambda item: getattr(item, "ts_ns", None) or 0)
                for item in batch:
                    yield item
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def follow(
    runner: SshRunner,
    inventory: Inventory,
    sources: list[LogSource],
    *,
    pattern: str = "",
    since: str = "1m",
) -> AsyncIterator[LogLine | SourceError]:
    session = FollowSession(runner, inventory, sources, pattern=pattern, since=since)
    async for event in session.run():
        yield event
