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
from ..ssh import ErrorKind, SshError, SshRunner
from ..ssh import proc as proc_module
from . import commands, parsers
from .grouper import EntryGrouper
from .model import LogLine, LogSource, SourceError

QUEUE_PER_SOURCE = 2000
PUMP_INTERVAL = 0.08
BATCH = 500
RECONNECT_DELAY = 2.0
#: The longest wait between two attempts, however many have failed.
MAX_BACKOFF = 30.0
#: Attempts in a row that may fail (no connection, or a stream that ends before a single line)
#: before a source is given up on.
MAX_FAILURES = 6
MISSING_RETRY = 30.0
#: How long a container may stay away before it is given up on.
MISSING_GIVE_UP_S = 600.0


async def _read_stderr(stream: asyncio.StreamReader | None) -> tuple[str, str]:
    """Drain stderr: the error marker the pipeline printed, if any, and else its last line."""
    if stream is None:
        return "", ""
    text = (await stream.read()).decode(errors="replace")
    if commands.ERROR_MARKER in text:
        return text.split(commands.ERROR_MARKER, 1)[1].strip().split("\n")[0], ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "", (lines[-1][:300] if lines else "")


def _backoff(failures: int) -> float:
    return min(RECONNECT_DELAY * 2 ** max(failures - 1, 0), MAX_BACKOFF)


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
        """Follow one source until cancelled, or until it is clear it will not come back.

        It used to retry for ever: every two seconds against a paused host, every thirty against a
        container that was gone, each time with an error of its own on screen.
        """
        grouper = EntryGrouper(source.log_format)
        since = self.since
        failures = 0
        missing_since: float | None = None
        shown = ""

        def report(kind: str, message: str) -> None:
            nonlocal shown
            if message != shown:  # once, not on every attempt
                self._put(source.id, SourceError(source.id, source.label, kind, message))
                shown = message

        def give_up(kind: str, message: str) -> None:
            self._put(source.id, SourceError(source.id, source.label, kind, f"{message}; stopped following it"))

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
                if isinstance(exc, SshError) and exc.kind is ErrorKind.PAUSED:
                    give_up("paused", f"{host.alias} is paused ({exc.message})")
                    return
                failures += 1
                if failures >= MAX_FAILURES:
                    give_up("ssh", f"{failures} attempts in a row failed: {exc}")
                    return
                report("ssh", f"{exc}; trying again")
                await asyncio.sleep(_backoff(failures))
                continue

            marker, problem = "", ""
            seq = 0
            try:
                assert proc.stdout is not None
                await proc_module.feed_stdin(proc, pipeline.script.encode())

                stderr_task = asyncio.create_task(_read_stderr(proc.stderr))
                buffer = b""
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
                    marker, problem = await asyncio.wait_for(stderr_task, timeout=2)
            except asyncio.CancelledError:
                raise
            finally:
                await proc_module.terminate(proc)
                await slot.__aexit__(None, None, None)

            if "missing_container" in marker:
                now = time.monotonic()
                missing_since = missing_since if missing_since is not None else now
                if now - missing_since >= MISSING_GIVE_UP_S:
                    give_up("missing_container", f"the container has not been running for {int(MISSING_GIVE_UP_S // 60)} minutes")
                    return
                report("missing_container", f"container is not running; checking again every {int(MISSING_RETRY)} s")
                await asyncio.sleep(MISSING_RETRY)
                continue
            missing_since = None

            if seq:
                # It streamed, so whatever went wrong before is over.
                failures, shown = 0, ""
            else:
                failures += 1
                if failures >= MAX_FAILURES:
                    give_up("ssh", problem or f"the stream ended {failures} times in a row without a line (has the container stopped?)")
                    return
                if problem:
                    report("ssh", f"{problem}; trying again")
            # The stream ended: the container was redeployed, or the link dropped. Resume from the
            # last line we saw so nothing is replayed twice.
            last = self._last_ts.get(source.id)
            if last:
                since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last / 1_000_000_000))
            await asyncio.sleep(_backoff(failures))

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
