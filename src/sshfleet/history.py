"""Every state the configuration has been in, so any of them can be looked at and gone back to.

A *state* is the tracked files at one moment: ``config.yaml`` plus each environment's SSH config
file and the files it includes. A new one is recorded whenever they change, whoever changed them —
the web page, a CLI command, or a hand edit noticed by the server's watcher or by the next CLI
run — and recording is skipped when nothing differs from the latest.

Storage is content-addressed, so the hundredth state of a fleet costs only the files that changed:

    history/blobs/<sha256>                       one copy of each distinct file content
    history/states/<UTC stamp>-<6 hex>.json      what each file held, why, and what changed

Directories are 0700 and files 0600: the SSH files name hosts and users, though never keys. A file
whose name or content looks like key material is listed as skipped and never copied — the same
rule as everywhere else in the tool. The CLI and the server can record at the same moment, so
writes happen under an ``flock``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import re
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .config.schema import Config
from .fsutil import atomic_write
from .inventory import included_files
from .inventory.sshconfig import holds_key_material, looks_like_key
from .inventory.sshedit import aliases_in
from .paths import Paths

ROLE_CONFIG = "config"
ROLE_SSH = "ssh"
#: Larger than any SSH config or config.yaml a fleet will have; past it a file is listed, not kept.
MAX_FILE_BYTES = 2 * 1024 * 1024
#: Retention: the newest KEEP_NEWEST states, and every state younger than KEEP_DAYS, but never more
#: than HARD_CAP. Pruning waits for PRUNE_SLACK extra states so it runs in batches, not per record.
KEEP_NEWEST = 200
KEEP_DAYS = 14
HARD_CAP = 2000
PRUNE_SLACK = 20

STATE_ID_RE = re.compile(r"^\d{8}T\d{9}Z-[0-9a-f]{6}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class HistoryError(Exception):
    """A state that does not exist, or a history that cannot be read."""


@dataclass(frozen=True, slots=True)
class TrackedFile:
    role: str
    path: Path
    env: str | None = None


@dataclass(slots=True)
class FileEntry:
    role: str
    path: str
    env: str | None
    sha256: str | None
    size: int = 0
    mode: int = 0o600
    missing: bool = False
    #: Why the content was not kept: "too large", "looks like a key", "holds key material".
    skipped: str | None = None

    @property
    def kept(self) -> bool:
        return self.sha256 is not None


@dataclass(slots=True)
class StateRecord:
    id: str
    at: str
    reason: str
    source: str
    files: list[FileEntry]
    valid: bool = True
    error: str | None = None
    restored_from: str | None = None
    sshfleet_version: str = __version__
    config_version: int | None = None
    #: What changed since the state before it, worked out when it was recorded.
    changes: dict[str, Any] = field(default_factory=dict)

    @property
    def code(self) -> str:
        """The six characters typed to confirm a restore of this state."""
        return self.id[-6:]

    @property
    def fingerprint(self) -> tuple:
        return _fingerprint(self.files)

    def file(self, path: str) -> FileEntry | None:
        return next((f for f in self.files if f.path == path), None)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["code"] = self.code
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateRecord:
        files = [FileEntry(**entry) for entry in data.get("files") or []]
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "files"}  # type: ignore[attr-defined]
        return cls(files=files, **known)


def _fingerprint(files: list[FileEntry]) -> tuple:
    return tuple(sorted((f.path, f.sha256, f.missing, f.skipped) for f in files))


def tracked_files(config: Config, paths: Paths) -> list[TrackedFile]:
    """config.yaml, then each environment's SSH file and what it includes, each path once.

    The server's watcher stats exactly this list, so what history keeps and what triggers a reload
    are the same files. An environment whose file is missing is still listed — as missing — so
    the state says so instead of quietly leaving it out.
    """
    out = [TrackedFile(ROLE_CONFIG, paths.config_file)]
    for env in config.environments:
        own = env.ssh_config_path.expanduser()
        with contextlib.suppress(OSError):
            for path in included_files(own) or [own]:
                out.append(TrackedFile(ROLE_SSH, path, env.name))
    seen: set[str] = set()
    unique: list[TrackedFile] = []
    for item in out:
        if str(item.path) not in seen:
            seen.add(str(item.path))
            unique.append(item)
    return unique


def _read(item: TrackedFile) -> tuple[FileEntry, bytes | None]:
    """What a file holds now, without keeping anything. Key material is never opened."""
    entry = FileEntry(role=item.role, path=str(item.path), env=item.env, sha256=None)
    path = item.path.expanduser()
    if item.role == ROLE_SSH and looks_like_key(path):
        entry.skipped = "looks like a key"
        return entry, None
    try:
        info = path.stat()
    except FileNotFoundError:
        entry.missing = True
        return entry, None
    except OSError as exc:
        entry.skipped = f"unreadable: {exc.strerror or exc}"
        return entry, None
    entry.size = info.st_size
    entry.mode = info.st_mode & 0o777
    if info.st_size > MAX_FILE_BYTES:
        entry.skipped = "too large"
        return entry, None
    try:
        data = path.read_bytes()
    except OSError as exc:
        entry.skipped = f"unreadable: {exc.strerror or exc}"
        return entry, None
    if holds_key_material(data):
        entry.skipped = "holds key material"
        return entry, None
    entry.sha256 = hashlib.sha256(data).hexdigest()
    return entry, data


def read_files(files: list[TrackedFile]) -> list[tuple[FileEntry, bytes | None]]:
    return [_read(item) for item in files]


def _now_stamp() -> tuple[str, str]:
    now = time.time()
    seconds = time.gmtime(now)
    millis = int(now * 1000) % 1000
    return time.strftime("%Y%m%dT%H%M%S", seconds) + f"{millis:03d}Z", time.strftime("%Y-%m-%dT%H:%M:%S", seconds) + f".{millis:03d}Z"


def _yaml(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    try:
        loaded = yaml.safe_load(data.decode(errors="replace"))
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def config_changes(old: dict[str, Any] | None, new: dict[str, Any] | None, limit: int = 24) -> list[str]:
    """Which parts of config.yaml differ, named the way a person would: ``hosts.web-1``,
    ``environments.staging``, ``thresholds``."""
    old, new = old or {}, new or {}
    out: list[str] = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        if key in {"environments", "projects"} and isinstance(a or [], list) and isinstance(b or [], list):
            by_a = {str(e.get("name")): e for e in a or [] if isinstance(e, dict)}
            by_b = {str(e.get("name")): e for e in b or [] if isinstance(e, dict)}
            out.extend(f"{key}.{name}" for name in sorted(set(by_a) | set(by_b)) if by_a.get(name) != by_b.get(name))
        elif key in {"hosts", "tunnels"} and isinstance(a or {}, dict) and isinstance(b or {}, dict):
            a, b = a or {}, b or {}
            out.extend(f"{key}.{name}" for name in sorted(set(a) | set(b)) if a.get(name) != b.get(name))
        else:
            out.append(key)
    return out[:limit] + ([f"… {len(out) - limit} more"] if len(out) > limit else [])


def _host_patterns(data: bytes | None) -> set[str]:
    if data is None:
        return set()
    return {p for p in aliases_in(data.decode(errors="replace")) if not any(c in p for c in "*?!")}


class History:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.blobs = root / "blobs"
        self.states = root / "states"

    # ------------------------------------------------------------------ storage

    def _ensure(self) -> None:
        for directory in (self.root, self.blobs, self.states):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure()
        lock = self.root / ".lock"
        with lock.open("a") as handle:
            lock.chmod(0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _ids(self) -> list[str]:
        """Every state id, oldest first. The stamp leads, so name order is time order."""
        if not self.states.is_dir():
            return []
        return sorted(p.stem for p in self.states.glob("*.json") if STATE_ID_RE.match(p.stem))

    def _load(self, state_id: str) -> StateRecord:
        try:
            return StateRecord.from_dict(json.loads((self.states / f"{state_id}.json").read_text()))
        except (OSError, ValueError, TypeError) as exc:
            raise HistoryError(f"state {state_id} cannot be read: {exc}") from exc

    def blob(self, sha: str) -> bytes:
        if not _SHA_RE.match(sha or ""):
            raise HistoryError(f"not a content hash: {sha!r}")
        try:
            return (self.blobs / sha).read_bytes()
        except OSError as exc:
            raise HistoryError(f"content {sha[:12]} is missing from history") from exc

    def content(self, entry: FileEntry) -> bytes | None:
        return self.blob(entry.sha256) if entry.sha256 else None

    # ------------------------------------------------------------------ reading

    def latest(self) -> StateRecord | None:
        ids = self._ids()
        return self._load(ids[-1]) if ids else None

    def get(self, state_id: str) -> StateRecord:
        if not STATE_ID_RE.match(state_id or ""):
            raise HistoryError(f"no such state: {state_id!r}")
        if not (self.states / f"{state_id}.json").exists():
            raise HistoryError(f"no such state: {state_id}")
        return self._load(state_id)

    def previous(self, state_id: str) -> StateRecord | None:
        ids = self._ids()
        earlier = [i for i in ids if i < state_id]
        return self._load(earlier[-1]) if earlier else None

    def list(self, limit: int = 50, before: str | None = None) -> tuple[list[StateRecord], int]:
        """The newest ``limit`` states (older than ``before`` when given), and how many exist."""
        ids = self._ids()
        pool = [i for i in ids if before is None or i < before]
        return [self._load(i) for i in reversed(pool[-limit:])] if limit > 0 else [], len(ids)

    # ------------------------------------------------------------------ writing

    def record(
        self,
        files: list[TrackedFile],
        *,
        reason: str,
        source: str,
        valid: bool = True,
        error: str | None = None,
        restored_from: str | None = None,
    ) -> tuple[StateRecord, bool]:
        """Record the files as they are now. Returns the state and whether it is a new one: when
        nothing differs from the latest state, that state comes back and nothing is written."""
        with self._locked():
            read = read_files(files)
            entries = [entry for entry, _ in read]
            latest = self.latest()
            if latest is not None and latest.fingerprint == _fingerprint(entries):
                return latest, False

            for entry, data in read:
                if entry.sha256 and data is not None and not (self.blobs / entry.sha256).exists():
                    atomic_write(self.blobs / entry.sha256, data, mode=0o600)

            stamp, at = _now_stamp()
            digest = hashlib.sha256(repr(_fingerprint(entries)).encode()).hexdigest()[:6]
            config_data = next((data for entry, data in read if entry.role == ROLE_CONFIG), None)
            parsed = _yaml(config_data)
            state = StateRecord(
                id=f"{stamp}-{digest}",
                at=at,
                reason=reason,
                source=source,
                files=entries,
                valid=valid,
                error=error,
                restored_from=restored_from,
                config_version=int(parsed["version"]) if parsed and str(parsed.get("version", "")).isdigit() else None,
                changes=self._changes(latest, read, parsed),
            )
            atomic_write(self.states / f"{state.id}.json", json.dumps(state.to_dict(), indent=1), mode=0o600)
            self._prune()
            return state, True

    def _changes(self, before: StateRecord | None, read: list[tuple[FileEntry, bytes | None]], parsed: dict[str, Any] | None) -> dict[str, Any]:
        if before is None:
            return {"first": True}
        files: list[dict[str, str]] = []
        hosts_before: set[str] = set()
        hosts_after: set[str] = set()
        config_before: dict[str, Any] | None = None
        now = {entry.path: (entry, data) for entry, data in read}
        for old in before.files:
            if old.role == ROLE_SSH:
                with contextlib.suppress(HistoryError):
                    hosts_before |= _host_patterns(self.content(old))
            elif old.role == ROLE_CONFIG:
                with contextlib.suppress(HistoryError):
                    config_before = _yaml(self.content(old))
        for path, (entry, data) in now.items():
            if entry.role == ROLE_SSH:
                hosts_after |= _host_patterns(data)
            old = before.file(path)
            if old is None:
                files.append({"path": path, "role": entry.role, "change": "added"})
            elif (old.sha256, old.missing, old.skipped) != (entry.sha256, entry.missing, entry.skipped):
                files.append({"path": path, "role": entry.role, "change": "changed"})
        for old in before.files:
            if old.path not in now:
                files.append({"path": old.path, "role": old.role, "change": "untracked"})
        return {
            "files": files,
            "hosts_added": sorted(hosts_after - hosts_before),
            "hosts_removed": sorted(hosts_before - hosts_after),
            "config": config_changes(config_before, parsed),
        }

    def _prune(self) -> int:
        """Drop states past retention, then any content no remaining state refers to."""
        ids = self._ids()
        cutoff = time.strftime("%Y%m%dT%H%M%S000Z", time.gmtime(time.time() - KEEP_DAYS * 86400))
        keep = set(ids[-KEEP_NEWEST:]) | {i for i in ids if i >= cutoff}
        keep = set(sorted(keep)[-HARD_CAP:])
        drop = [i for i in ids if i not in keep]
        if len(drop) < PRUNE_SLACK:
            return 0
        for state_id in drop:
            (self.states / f"{state_id}.json").unlink(missing_ok=True)
        referenced: set[str] = set()
        for state_id in keep:
            with contextlib.suppress(HistoryError):
                referenced |= {f.sha256 for f in self._load(state_id).files if f.sha256}
        for blob in self.blobs.iterdir():
            if _SHA_RE.match(blob.name) and blob.name not in referenced:
                blob.unlink(missing_ok=True)
        return len(drop)


def record_now(config: Config, paths: Paths, *, reason: str, source: str, **extra: Any) -> tuple[StateRecord, bool]:
    """Record the configuration as it is on disk now. The CLI's entry point."""
    return History(paths.history_dir).record(tracked_files(config, paths), reason=reason, source=source, **extra)
