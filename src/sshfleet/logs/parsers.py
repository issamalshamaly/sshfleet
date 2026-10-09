"""Timestamp parsing and line sanitising.

Five timestamp shapes reach this code: the ``docker logs -t`` RFC3339Nano prefix, pino-pretty's
bracketed ``[2026-08-11 10:59:08.669 +0000]``, zap's ISO with offset, nginx's
``[13/Sep/2026:10:00:00 +0000]`` and journalctl's short-iso. Everything is converted to epoch
nanoseconds so merging several services stays exact at docker's nanosecond resolution.
"""

from __future__ import annotations

import calendar
import re
from functools import lru_cache

from .model import LogFormat

# CSI and OSC escape sequences, plus stray C0 control bytes.
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?\s*(Z|[+-]\d{2}:?\d{2})?"
)
PINO_HEADER_RE = re.compile(
    r"^\[(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d{3}) ([+-]\d{4})\]\s*([A-Z]+)(?:\s*\([^)]*\))?:"
)
ZAP_HEADER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{1,9}(?:Z|[+-]\d{2}:\d{2}))\t([A-Z]+)\t"
)
NGINX_ACCESS_RE = re.compile(
    r"\[(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})\]"
)
NGINX_ERROR_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2}) \[(\w+)\]")
JSON_TIME_RE = re.compile(r'"(?:ts|time|timestamp|@timestamp|@t)"\s*:\s*"([^"]+)"')
JSON_LEVEL_RE = re.compile(r'"level"\s*:\s*(?:"([a-zA-Z]+)"|(\d+))')

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
)}

#: pino numeric levels
PINO_LEVELS = {10: "TRACE", 20: "DEBUG", 30: "INFO", 40: "WARN", 50: "ERROR", 60: "FATAL"}


def sanitize(text: str) -> str:
    """Strip colour codes and control junk so the line is safe to render and to grep locally."""
    return CONTROL_RE.sub("⌧", ANSI_RE.sub("", text)).rstrip("\r\n")


@lru_cache(maxsize=4096)
def _day_epoch(year: int, month: int, day: int) -> int:
    return calendar.timegm((year, month, day, 0, 0, 0, 0, 0, 0))


def _to_ns(year: int, month: int, day: int, hour: int, minute: int, second: int, fraction: str, offset: str | None) -> int:
    epoch = _day_epoch(year, month, day) + hour * 3600 + minute * 60 + second
    if offset and offset not in {"Z", "z", "+0000", "+00:00"}:
        sign = 1 if offset[0] == "+" else -1
        digits = offset[1:].replace(":", "")
        epoch -= sign * (int(digits[:2]) * 3600 + int(digits[2:4] or 0) * 60)
    nanos = int((fraction or "").ljust(9, "0")[:9] or 0)
    return epoch * 1_000_000_000 + nanos


def parse_iso(text: str) -> int | None:
    """Parse the leading ISO timestamp of a line (docker ``-t`` prefix, zap, journal)."""
    match = ISO_RE.match(text)
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    return _to_ns(int(year), int(month), int(day), int(hour), int(minute), int(second), fraction or "", offset)


def split_docker_prefix(text: str) -> tuple[int | None, str]:
    """``2026-08-11T10:59:08.670022952Z rest`` -> ``(ts_ns, "rest")``."""
    match = ISO_RE.match(text)
    if not match or match.end() >= len(text) or text[match.end()] != " ":
        return None, text
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    if offset is None:
        return None, text  # a bare date at the start of a message is not a -t prefix
    ts = _to_ns(int(year), int(month), int(day), int(hour), int(minute), int(second), fraction or "", offset)
    return ts, text[match.end() + 1 :]


def parse_pino_header(text: str) -> tuple[int, str] | None:
    match = PINO_HEADER_RE.match(text)
    if not match:
        return None
    year, month, day, hour, minute, second, millis, offset, level = match.groups()
    ts = _to_ns(int(year), int(month), int(day), int(hour), int(minute), int(second), millis, offset)
    return ts, level


def parse_zap_header(text: str) -> tuple[int, str] | None:
    match = ZAP_HEADER_RE.match(text)
    if not match:
        return None
    ts = parse_iso(match.group(1))
    return (ts, match.group(2)) if ts is not None else None


def parse_nginx_access(text: str) -> int | None:
    match = NGINX_ACCESS_RE.search(text)
    if not match:
        return None
    day, month, year, hour, minute, second, offset = match.groups()
    if month not in MONTHS:
        return None
    return _to_ns(int(year), MONTHS[month], int(day), int(hour), int(minute), int(second), "", offset)


def parse_nginx_error(text: str) -> tuple[int, str] | None:
    match = NGINX_ERROR_RE.match(text)
    if not match:
        return None
    year, month, day, hour, minute, second, level = match.groups()
    # nginx error.log has no offset; the host's clock is assumed to be UTC.
    return _to_ns(int(year), int(month), int(day), int(hour), int(minute), int(second), "", None), level.upper()


def parse_json_line(text: str) -> tuple[int | None, str | None]:
    """pino NDJSON and zap JSON: pull the time and level without parsing the whole document."""
    ts = None
    match = JSON_TIME_RE.search(text)
    if match:
        ts = parse_iso(match.group(1))
    level = None
    level_match = JSON_LEVEL_RE.search(text)
    if level_match:
        if level_match.group(1):
            level = level_match.group(1).upper()
        else:
            level = PINO_LEVELS.get(int(level_match.group(2)), level_match.group(2))
    return ts, level


def detect_format(lines: list[str]) -> LogFormat:
    """Majority vote over the first lines of a source."""
    votes: dict[LogFormat, int] = {}
    for line in lines[:50]:
        _, body = split_docker_prefix(line)
        body = sanitize(body)
        if not body.strip():
            continue
        if body.lstrip().startswith("{") and JSON_TIME_RE.search(body):
            fmt = LogFormat.ZAP_JSON if '"caller"' in body or '"ts"' in body else LogFormat.PINO_JSON
        elif PINO_HEADER_RE.match(body):
            fmt = LogFormat.PINO_PRETTY
        elif ZAP_HEADER_RE.match(body):
            fmt = LogFormat.ZAP_CONSOLE
        elif NGINX_ERROR_RE.match(body):
            fmt = LogFormat.NGINX_ERROR
        elif NGINX_ACCESS_RE.search(body):
            fmt = LogFormat.NGINX_ACCESS
        else:
            continue
        votes[fmt] = votes.get(fmt, 0) + 1
    if not votes:
        return LogFormat.PLAIN
    return max(votes, key=lambda key: votes[key])


def parse_body(body: str, fmt: LogFormat) -> tuple[int | None, str | None, bool]:
    """Return ``(ts_ns, level, is_header)`` for one already-sanitised line."""
    if fmt in {LogFormat.PINO_JSON, LogFormat.ZAP_JSON}:
        ts, level = parse_json_line(body)
        return ts, level, True
    if fmt is LogFormat.PINO_PRETTY:
        parsed = parse_pino_header(body)
        return (parsed[0], parsed[1], True) if parsed else (None, None, False)
    if fmt is LogFormat.ZAP_CONSOLE:
        parsed = parse_zap_header(body)
        return (parsed[0], parsed[1], True) if parsed else (None, None, False)
    if fmt is LogFormat.NGINX_ACCESS:
        ts = parse_nginx_access(body)
        return ts, None, ts is not None
    if fmt is LogFormat.NGINX_ERROR:
        parsed = parse_nginx_error(body)
        return (parsed[0], parsed[1], True) if parsed else (None, None, False)
    if fmt is LogFormat.JOURNAL:
        ts = parse_iso(body)
        return ts, None, ts is not None
    ts = parse_iso(body)
    return ts, None, ts is not None
