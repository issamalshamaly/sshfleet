"""Keep a log entry's continuation lines attached to its header.

pino-pretty prints a header line and then indents the whole object underneath it::

    [2026-08-11 10:59:08.669 +0000] WARN: upstream call failed
        requestId: "4f1c2a9e0b7d"
        error: {
          "type": "TIMEOUT"
        }

grep matches the *indented* line as often as the header, which is exactly why searching these
logs by hand needs ``-B 60``. Grouping lets the tool show whole entries instead, and tells the
reader when the header was left outside the context window.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import parsers
from .model import LogFormat, LogLine

CLOSERS = set("]}),\t ")


@dataclass(slots=True)
class EntryGrouper:
    """Feeds lines in order and decides which start a new entry."""

    log_format: LogFormat = LogFormat.AUTO
    entry_id: int = 0
    group_id: int = 0
    depth: int = 0
    open_entry: bool = False
    entry_ts: int | None = None
    entry_level: str | None = None
    last_ts: int | None = None
    header_seen: bool = False
    _sniff: list[str] = field(default_factory=list)

    def boundary(self) -> None:
        """A ``--`` separator from grep: the next line starts a new context group."""
        self.group_id += 1
        self.open_entry = False
        self.depth = 0

    def _detect(self, text: str) -> None:
        if self.log_format is not LogFormat.AUTO:
            return
        self._sniff.append(text)
        if len(self._sniff) >= 12:
            self.log_format = parsers.detect_format(self._sniff)

    @staticmethod
    def _is_continuation(text: str, depth: int, fmt: LogFormat) -> bool:
        if not text:
            return False
        if text[0] in " \t":
            return True
        if depth > 0:
            return True
        # zap prints panics and stack traces at column 0 under the entry that caused them,
        # so anything that is not a new tab-separated header continues the current entry.
        if fmt is LogFormat.ZAP_CONSOLE:
            return True
        stripped = text.strip()
        return bool(stripped) and all(ch in CLOSERS for ch in stripped)

    @staticmethod
    def _depth_delta(text: str) -> int:
        opens = text.count("{") + text.count("[") + text.count("(")
        closes = text.count("}") + text.count("]") + text.count(")")
        return opens - closes

    def feed(self, text: str, prefix_ts: int | None = None) -> LogLine:
        """Classify one sanitised line and return it as a :class:`LogLine`."""
        self._detect(text)
        fmt = self.log_format if self.log_format is not LogFormat.AUTO else parsers.detect_format(self._sniff or [text])

        body_ts, level, is_header = parsers.parse_body(text, fmt)
        continuation = False

        if is_header:
            self.entry_id += 1
            self.open_entry = True
            self.depth = max(self._depth_delta(text), 0)
            self.entry_ts = prefix_ts if prefix_ts is not None else body_ts
            self.entry_level = level
            self.header_seen = True
        elif self.open_entry and self._is_continuation(text, self.depth, fmt):
            continuation = True
            self.depth = max(self.depth + self._depth_delta(text), 0)
        else:
            # A plain line (a console.log, a stack frame with no open entry) is its own entry.
            self.entry_id += 1
            self.open_entry = True
            self.depth = max(self._depth_delta(text), 0)
            self.entry_ts = prefix_ts if prefix_ts is not None else body_ts
            self.entry_level = level

        ts = prefix_ts if prefix_ts is not None else (self.entry_ts if continuation else body_ts)
        if ts is None:
            ts = self.last_ts  # inherit so an untimed line still merges near its neighbours
        else:
            self.last_ts = ts

        return LogLine(
            source_id="",
            label="",
            seq=0,
            text=text,
            ts_ns=ts,
            group_id=self.group_id,
            entry_id=self.entry_id,
            is_continuation=continuation,
            level=self.entry_level if continuation else level,
        )

    @property
    def first_line_was_orphaned(self) -> bool:
        """True when the first line looked like a continuation — its header was out of range."""
        return not self.header_seen and self.entry_id > 0
