"""Merge several services' output into one timeline.

Each source arrives in chronological order but the sources run in parallel, so a line can only
be released once every still-running source has passed its timestamp. The watermark is the
oldest "last seen" time across active sources; anything at or below it is safe to emit.

Sources whose lines carry no timestamp at all cannot take part and are appended at the end as
their own block rather than interleaved at the wrong place.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .model import LogLine


@dataclass(slots=True)
class TimelineMerger:
    """Order-preserving k-way merge over live streams."""

    sources: set[str] = field(default_factory=set)
    _heap: list[tuple[int, int, int, LogLine]] = field(default_factory=list)
    _watermark: dict[str, int] = field(default_factory=dict)
    _finished: set[str] = field(default_factory=set)
    _order: dict[str, int] = field(default_factory=dict)
    _counter: int = 0
    _last_emitted: int | None = None
    out_of_order: int = 0
    untimed: list[LogLine] = field(default_factory=list)

    def register(self, source_id: str) -> None:
        self.sources.add(source_id)
        self._order.setdefault(source_id, len(self._order))

    def finish(self, source_id: str) -> None:
        self._finished.add(source_id)
        self._watermark.pop(source_id, None)

    @property
    def _active(self) -> set[str]:
        return self.sources - self._finished

    def push(self, line: LogLine) -> None:
        if line.ts_ns is None:
            self.untimed.append(line)
            return
        self._counter += 1
        heapq.heappush(self._heap, (line.ts_ns, self._order.get(line.source_id, 0), self._counter, line))
        previous = self._watermark.get(line.source_id)
        if previous is not None and line.ts_ns < previous:
            self.out_of_order += 1
        self._watermark[line.source_id] = max(line.ts_ns, previous or line.ts_ns)

    def drain(self) -> list[LogLine]:
        """Everything that can be released without risking an out-of-order line."""
        active = self._active
        if not active:
            return self.flush()
        pending = [self._watermark.get(source) for source in active]
        if any(value is None for value in pending):
            return []  # a source has not produced anything yet; it may still be older
        limit = min(v for v in pending if v is not None)

        out: list[LogLine] = []
        while self._heap and self._heap[0][0] <= limit:
            _, _, _, line = heapq.heappop(self._heap)
            self._last_emitted = line.ts_ns
            out.append(line)
        return out

    def flush(self) -> list[LogLine]:
        """Release everything: called once all sources are done."""
        out: list[LogLine] = []
        while self._heap:
            _, _, _, line = heapq.heappop(self._heap)
            out.append(line)
        out.extend(self.untimed)
        self.untimed = []
        return out

    @property
    def buffered(self) -> int:
        return len(self._heap) + len(self.untimed)

    @property
    def waiting_on(self) -> list[str]:
        """Sources the merge is currently blocked on, for the status line."""
        active = self._active
        if not active or not self._heap:
            return []
        limit_source = min(
            (s for s in active if self._watermark.get(s) is not None),
            key=lambda s: self._watermark[s],
            default=None,
        )
        never_seen = [s for s in active if self._watermark.get(s) is None]
        return never_seen or ([limit_source] if limit_source else [])
