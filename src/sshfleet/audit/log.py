"""Append-only local record of every mutating action.

One JSON object per line, 0600. Restart, upload, download, tunnel changes and shell opens are
recorded with the exact command, so "what did I touch on production today" is answerable; so are
the reads that could matter later — a file listed or previewed, a log search.

Each line carries ``prev``, the SHA-256 of the line before it, so a line changed, removed or put
in between shows up in ``sshfleet audit verify``. That is evidence, not protection: whoever can
write the file can write a whole new chain. Past :data:`MAX_BYTES` the file is set aside as
``audit.1.jsonl`` (the one before becomes ``.2``, and so on, :data:`KEEP` of them) and the chain
carries on into the new one.
"""

from __future__ import annotations

import collections
import contextlib
import csv
import fcntl
import fnmatch
import getpass
import hashlib
import io
import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

#: The columns of a CSV export, in the order a person reads them.
CSV_FIELDS = ("ts", "actor", "action", "env", "host", "target", "status", "duration_ms", "command", "note")
_SINCE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
#: Size of the current file past which it is set aside and a new one started.
MAX_BYTES = 10 * 1024 * 1024
#: Files kept besides the current one; the oldest goes first.
KEEP = 5
#: ``prev`` of the very first line.
GENESIS = "0" * 64


def link(line: bytes) -> str:
    """What the next line carries as ``prev``: the hash of this one, exactly as written."""
    return hashlib.sha256(line).hexdigest()


@dataclass(slots=True)
class Verified:
    """What ``audit verify`` found."""

    entries: int = 0
    #: Lines from before the chain existed, at the start of the trail, which it cannot vouch for.
    unlinked: int = 0
    breaks: list[str] = field(default_factory=list)
    #: The link the next line will carry. Noted somewhere else, it shows later whether the trail
    #: up to here was rewritten as a whole.
    head: str | None = None

    @property
    def intact(self) -> bool:
        return not self.breaks


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
    max_bytes: int = MAX_BYTES
    keep: int = KEEP

    def rotated(self, number: int) -> Path:
        return self.path.with_name(f"{self.path.stem}.{number}{self.path.suffix}")

    def files(self) -> list[Path]:
        """Every file of the trail, oldest first: the ones set aside, then the current one."""
        older = [self.rotated(n) for n in range(self.keep, 0, -1)]
        return [p for p in older if p.exists()] + ([self.path] if self.path.exists() else [])

    def append(self, action: str, **fields: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "actor": _actor(),
            "pid": os.getpid(),
            "action": action,
        }
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The CLI and the web server both write here: reading the last line and adding the next
        # one is a single step for whichever holds the lock.
        with self._locked():
            last, whole = self._last_line()
            entry["prev"] = link(last) if last is not None else GENESIS
            line = json.dumps(entry, ensure_ascii=False).encode() + b"\n"
            with contextlib.suppress(FileNotFoundError):
                if self.path.stat().st_size + len(line) > self.max_bytes:
                    self._rotate()
                    whole = True
            if not whole:
                line = b"\n" + line  # after a write cut short, start on a line of its own
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, line)
            finally:
                os.close(descriptor)
            os.chmod(self.path, 0o600)
        return entry

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = os.open(self.path.with_name(f".{self.path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _last_line(self) -> tuple[bytes | None, bool]:
        """The last line of the trail, and whether the file ends on a full line."""
        for path in reversed(self.files()):
            last, whole = _last_line_of(path)
            if last is not None:
                return last, (whole if path == self.path else True)
        return None, True

    def _rotate(self) -> None:
        self.rotated(self.keep).unlink(missing_ok=True)
        for number in range(self.keep - 1, 0, -1):
            if self.rotated(number).exists():
                os.replace(self.rotated(number), self.rotated(number + 1))
        os.replace(self.path, self.rotated(1))

    def verify(self) -> Verified:
        """Walk the chain from the oldest line kept to the newest."""
        result = Verified()
        previous: bytes | None = None
        chained = False
        for path in self.files():
            with path.open("rb") as handle:
                for number, raw in enumerate(handle, 1):
                    line = raw.rstrip(b"\n")
                    if not line.strip():
                        continue
                    result.entries += 1
                    where = f"{path.name} line {number}"
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        entry = None
                    prev = entry.get("prev") if isinstance(entry, dict) else None
                    if not isinstance(entry, dict):
                        result.breaks.append(f"{where} is not a record")
                    elif prev is None:
                        if chained:
                            result.breaks.append(
                                f"{where} carries no link to the line before it: written by an sshfleet older "
                                "than the chain, or by hand"
                            )
                        else:
                            result.unlinked += 1
                    elif previous is not None and prev != link(previous):
                        result.breaks.append(
                            f"{where} does not follow the line before it: a line was changed, removed or put in between there"
                        )
                    chained = chained or prev is not None
                    previous = line
        result.head = link(previous) if previous is not None else None
        return result

    def _lines(self) -> Iterator[str]:
        for path in self.files():
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.strip():
                        yield line

    def iter_entries(self, flt: AuditFilter | None = None) -> Iterator[dict[str, Any]]:
        """Every entry that matches, oldest first, read a line at a time."""
        for line in self._lines():
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
        last: collections.deque[str] = collections.deque(maxlen=max(limit, 0))
        total = 0
        for line in self._lines():
            last.append(line)
            total += 1
        out: list[dict[str, Any]] = []
        for line in last:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out, total


def _last_line_of(path: Path) -> tuple[bytes | None, bool]:
    """The last non-empty line of a file, read from the end, and whether a newline closes it."""
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return None, True
    with handle:
        end = handle.seek(0, os.SEEK_END)
        if end == 0:
            return None, True
        handle.seek(end - 1)
        whole = handle.read(1) == b"\n"
        tail = b""
        position = end
        while position > 0:
            step = min(65536, position)
            position -= step
            handle.seek(position)
            tail = handle.read(step) + tail
            body = tail.rstrip(b"\n")
            cut = body.rfind(b"\n")
            if cut != -1:
                return body[cut + 1 :], whole
        body = tail.rstrip(b"\n")
        return (body or None), whole


def _actor() -> str:
    """The account this runs as, by its uid: ``$USER`` is whatever the environment says."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):  # pragma: no cover - no passwd entry
        return _safe_user()


def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry
        return "unknown"
