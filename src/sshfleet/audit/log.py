"""Append-only local record of every mutating action.

One JSON object per line, 0600. Restart, upload, download, tunnel changes and shell opens are
recorded with the exact command, so "what did I touch on production today" is answerable.
"""

from __future__ import annotations

import csv
import fnmatch
import getpass
import io
import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

#: The columns of a CSV export, in the order a person reads them.
CSV_FIELDS = ("ts", "actor", "action", "env", "host", "target", "status", "duration_ms", "command", "note")
_SINCE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_since(text: str, *, now: datetime | None = None) -> str:
    """``30m``, ``12h``, ``7d``, ``2w`` ago, or a date or time (``2026-10-01``, ``2026-10-01T09:00``),
    as the timestamp form entries are stored in, so the two compare as text."""
    now = now or datetime.now(UTC)
    match = _SINCE.match(text or "")
    if match:
        moment = now - timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
    else:
        try:
            moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{text!r} is not a time: use 30m, 12h, 7d, 2w or a date like 2026-10-01") from exc
        if moment.tzinfo is None:
            moment = moment.astimezone()  # a bare date or time means this machine's local time
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class AuditFilter:
    """Which entries to read. ``envs`` names the environments in view; an entry that belongs to
    no environment (a settings change, a sound toggle) is fleet-wide and always matches."""

    since: str | None = None
    until: str | None = None
    actions: tuple[str, ...] = ()  # exact names or globs such as "container.*"
    envs: tuple[str, ...] | None = None
    host: str | None = None
    status: str | None = None

    def matches(self, entry: dict[str, Any]) -> bool:
        ts = str(entry.get("ts") or "")
        if self.since and ts < self.since:
            return False
        if self.until and ts > self.until:
            return False
        action = str(entry.get("action") or "")
        if self.actions and not any(fnmatch.fnmatchcase(action, pattern) for pattern in self.actions):
            return False
        if self.envs is not None and entry.get("env") and entry["env"] not in self.envs:
            return False
        if self.host and entry.get("host") != self.host:
            return False
        return not (self.status and entry.get("status") != self.status)


def _cell(value: Any) -> str:
    """A CSV cell a spreadsheet shows as text. A value that starts like a formula — a container
    named ``=HYPERLINK(...)`` — would otherwise be run when the file is opened."""
    if value is None:
        return ""
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, dict | list) else str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@", "\t", "\r"} else text


def write_csv(entries: Iterable[dict[str, Any]], handle: TextIO) -> int:
    writer = csv.writer(handle)
    writer.writerow(CSV_FIELDS)
    count = 0
    for entry in entries:
        writer.writerow([_cell(entry.get(field)) for field in CSV_FIELDS])
        count += 1
    return count


def write_jsonl(entries: Iterable[dict[str, Any]], handle: TextIO) -> int:
    count = 0
    for entry in entries:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        count += 1
    return count


def render(entries: Iterable[dict[str, Any]], fmt: str) -> tuple[str, int]:
    """The export as text: ``csv`` or ``jsonl``."""
    buffer = io.StringIO()
    count = write_jsonl(entries, buffer) if fmt == "jsonl" else write_csv(entries, buffer)
    return buffer.getvalue(), count


@dataclass(slots=True)
class AuditLog:
    path: Path

    def append(self, action: str, **fields: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "actor": os.environ.get("USER") or _safe_user(),
            "action": action,
        }
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.chmod(self.path, 0o600)
        return entry

    def iter_entries(self, flt: AuditFilter | None = None) -> Iterator[dict[str, Any]]:
        """Every entry that matches, oldest first, read a line at a time."""
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and (flt is None or flt.matches(entry)):
                    yield entry

    def query(self, flt: AuditFilter, limit: int) -> tuple[list[dict[str, Any]], int, list[str]]:
        """The newest ``limit`` matching entries (newest first), how many match, and every action
        name in the trail — for a filter that offers all of them, not only those on screen."""
        matching: list[dict[str, Any]] = []
        actions: set[str] = set()
        for entry in self.iter_entries():
            actions.add(str(entry.get("action") or ""))
            if flt.matches(entry):
                matching.append(entry)
        return (list(reversed(matching[-limit:])) if limit > 0 else []), len(matching), sorted(a for a in actions if a)

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.page(limit)[0]

    def page(self, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
        """The last ``limit`` entries, and how many there are in total.

        The total is what lets a reader know whether "load more" has anything left to give,
        without a second request or a guess at the file's size.
        """
        if not self.path.exists():
            return [], 0
        lines = [line for line in self.path.read_text(errors="replace").splitlines() if line.strip()]
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out, len(lines)


def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry
        return "unknown"
