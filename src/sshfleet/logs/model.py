"""Types for log search: sources, time windows, requests, and streamed results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from ..tz import zone_for


class SourceKind(StrEnum):
    CONTAINER_STDOUT = "container_stdout"
    CONTAINER_FILE = "container_file"
    HOST_FILE = "host_file"
    JOURNAL = "journal"
    SEQ = "seq"
    ELASTIC = "elastic"


class LogFormat(StrEnum):
    AUTO = "auto"
    PINO_PRETTY = "pino_pretty"
    PINO_JSON = "pino_json"
    ZAP_CONSOLE = "zap_console"
    ZAP_JSON = "zap_json"
    NGINX_ACCESS = "nginx_access"
    NGINX_ERROR = "nginx_error"
    JOURNAL = "journal_short_iso"
    PLAIN = "plain"


@dataclass(frozen=True, slots=True)
class LogSource:
    """One place to read log lines from."""

    id: str
    kind: SourceKind
    env: str
    label: str  # short tag shown beside every line
    host_alias: str | None = None
    service: str | None = None
    container: str | None = None
    path: str | None = None
    rotated_glob: str | None = None
    unit: str | None = None
    log_format: LogFormat = LogFormat.AUTO

    def __str__(self) -> str:
        return self.id


@dataclass(frozen=True, slots=True)
class ResolvedWindow:
    since: datetime
    until: datetime
    label: str
    resolved_at: datetime

    @property
    def since_rfc3339(self) -> str:
        """Docker interprets an offset-less timestamp in the *client's* zone, so always send Z."""
        return self.since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def until_rfc3339(self) -> str:
        return self.until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def until_compare(self) -> str:
        """The form the remote awk stage compares against a ``docker logs -t`` prefix."""
        return self.until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")

    @property
    def since_compare(self) -> str:
        return self.since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")

    def describe(self, tz: str = "UTC") -> str:
        zone = zone_for(tz)
        start = self.since.astimezone(zone)
        end = self.until.astimezone(zone)
        same_day = start.date() == end.date()
        end_text = end.strftime("%H:%M") if same_day else end.strftime("%Y-%m-%d %H:%M")
        return f"{start.strftime('%Y-%m-%d %H:%M')}–{end_text} {start.strftime('%z') or 'UTC'}"


_RELATIVE = re.compile(r"^(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)$", re.I)
_UNITS = {"s": 1, "sec": 1, "secs": 1, "m": 60, "min": 60, "mins": 60, "h": 3600, "hr": 3600, "hrs": 3600, "d": 86400, "day": 86400, "days": 86400}


def parse_duration(text: str) -> timedelta:
    match = _RELATIVE.match(text.strip())
    if not match:
        raise ValueError(f"not a duration: {text!r} (try 30m, 2h, 1d)")
    return timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2).lower()])


def parse_instant(text: str, tz: ZoneInfo, now: datetime) -> datetime:
    """Accept ``10:00``, ``yesterday 22:30``, ``2026-09-13 10:00``, ISO with offset, ``now``."""
    value = text.strip()
    lowered = value.lower()
    if lowered == "now":
        return now
    if lowered.startswith("yesterday"):
        rest = value[len("yesterday") :].strip() or "00:00"
        base = (now.astimezone(tz) - timedelta(days=1)).date()
        hour, _, minute = rest.partition(":")
        return datetime(base.year, base.month, base.day, int(hour), int(minute or 0), tzinfo=tz)
    if lowered.startswith("today"):
        rest = value[len("today") :].strip() or "00:00"
        base = now.astimezone(tz).date()
        hour, _, minute = rest.partition(":")
        return datetime(base.year, base.month, base.day, int(hour), int(minute or 0), tzinfo=tz)

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    except ValueError:
        pass

    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", value):
        today = now.astimezone(tz)
        parts = [int(p) for p in value.split(":")]
        candidate = today.replace(hour=parts[0], minute=parts[1], second=parts[2] if len(parts) > 2 else 0, microsecond=0)
        # A time later than now means the operator meant yesterday.
        return candidate - timedelta(days=1) if candidate > today else candidate

    raise ValueError(f"cannot read a time from {value!r}")


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """Relative (``30m``) or absolute (``10:00..11:30``); resolved once per search."""

    spec: str = "30m"
    tz: str = "UTC"

    def resolve(self, now: datetime | None = None) -> ResolvedWindow:
        current = now or datetime.now(UTC)
        zone = zone_for(self.tz)
        text = self.spec.strip()

        if ".." in text:
            left, _, right = text.partition("..")
            since = parse_instant(left, zone, current)
            until = parse_instant(right, zone, current) if right.strip() else current
            return ResolvedWindow(since, until, text, current)

        try:
            delta = parse_duration(text)
        except ValueError:
            since = parse_instant(text, zone, current)
            return ResolvedWindow(since, current, text, current)
        return ResolvedWindow(current - delta, current, f"last {text}", current)


@dataclass(slots=True)
class SearchRequest:
    sources: tuple[LogSource, ...]
    pattern: str = ""
    mode: Literal["regex", "fixed"] = "regex"
    ignore_case: bool = True
    before: int = 60
    after: int = 0
    window: TimeWindow = field(default_factory=TimeWindow)
    tail_lines: int = 50_000
    max_bytes: int = 20 * 2**20
    max_line_bytes: int = 16 * 1024
    max_file_bytes: int = 200 * 2**20
    max_matches: int | None = None
    strip_ansi: bool = True
    timeout_s: int = 600
    display_tz: str = "UTC"
    allowed_paths: tuple[str, ...] = ()  # extra directories log files may be read from
    raw_dump: bool = False

    @property
    def has_pattern(self) -> bool:
        return bool(self.pattern.strip())


# ------------------------------------------------------------------- results


@dataclass(slots=True)
class LogLine:
    source_id: str
    label: str
    seq: int
    text: str
    ts_ns: int | None = None
    is_match: bool = True
    group_id: int = 0
    entry_id: int = 0
    is_continuation: bool = False
    level: str | None = None
    truncated: bool = False

    @property
    def when(self) -> datetime | None:
        return datetime.fromtimestamp(self.ts_ns / 1_000_000_000, UTC) if self.ts_ns else None


@dataclass(slots=True)
class SourceProgress:
    source_id: str
    lines: int
    bytes: int
    matches: int
    elapsed: float


@dataclass(slots=True)
class SourceDone:
    source_id: str
    label: str
    lines: int
    bytes: int
    matches: int
    elapsed: float
    command: str
    truncated: bool = False
    truncated_reason: str = ""
    coverage_ok: bool = True
    oldest_ts_ns: int | None = None
    newest_ts_ns: int | None = None
    exit_codes: list[int] = field(default_factory=list)


@dataclass(slots=True)
class SourceError:
    source_id: str
    label: str
    kind: str  # missing_container | missing_file | no_runtime | permission | too_large | ssh | timeout | bad_regex | remote_tool | denied | unknown
    message: str
    command: str = ""
    stderr_tail: str = ""


@dataclass(slots=True)
class SearchDone:
    window: ResolvedWindow
    lines: int
    matches: int
    bytes: int
    elapsed: float
    per_source: dict[str, SourceDone | SourceError] = field(default_factory=dict)
    out_of_order: int = 0

    @property
    def truncated_sources(self) -> list[str]:
        return [s.label for s in self.per_source.values() if isinstance(s, SourceDone) and s.truncated]

    @property
    def failed_sources(self) -> list[str]:
        return [s.label for s in self.per_source.values() if isinstance(s, SourceError)]


Event = LogLine | SourceProgress | SourceDone | SourceError | SearchDone
