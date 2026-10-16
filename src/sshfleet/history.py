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

import calendar
import contextlib
import difflib
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
from .config import migrate
from .config.loader import parse_text
from .config.schema import CONFIG_VERSION, Config, ConfigError
from .fsutil import atomic_write
from .inventory import included_files
from .inventory.sshconfig import holds_key_material, looks_like_key
from .inventory.sshedit import aliases_in, replace_file
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

STATE_ID_RE = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{6}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class HistoryError(Exception):
    """A state that does not exist, or a history that cannot be read."""


class RestoreRefused(Exception):
    """Putting this state back would leave a configuration that does not load; nothing was written."""


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


def _stamp_after(previous_id: str | None) -> tuple[str, str]:
    """A state's stamp (for its id) and its time (for people), always later than the state before
    it. Ids sort by their stamp, so two states recorded inside one clock tick — a burst of edits,
    a clock that steps back — must still come out in the order they were recorded. Called under
    the history lock, which makes that hold across processes too."""
    now_us = time.time_ns() // 1000
    if previous_id:
        stamp = previous_id[:21]
        previous_us = calendar.timegm(time.strptime(stamp[:15], "%Y%m%dT%H%M%S")) * 1_000_000 + int(stamp[15:21])
        now_us = max(now_us, previous_us + 1)
    seconds, micros = divmod(now_us, 1_000_000)
    when = time.gmtime(seconds)
    return (
        time.strftime("%Y%m%dT%H%M%S", when) + f"{micros:06d}Z",
        time.strftime("%Y-%m-%dT%H:%M:%S", when) + f".{micros // 1000:03d}Z",
    )


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

    def resolve(self, ref: str) -> StateRecord:
        """A state named by its id, the six characters of its code, or an unambiguous id prefix."""
        ref = (ref or "").strip()
        if STATE_ID_RE.match(ref):
            return self.get(ref)
        matches = [i for i in self._ids() if i.endswith(f"-{ref}") or (len(ref) >= 8 and i.startswith(ref))]
        if not matches:
            raise HistoryError(f"no state matches {ref!r}")
        if len(matches) > 1:
            raise HistoryError(f"{ref!r} matches {len(matches)} states; give the full id (one of {', '.join(matches[-3:])}…)")
        return self._load(matches[0])

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

            stamp, at = _stamp_after(latest.id if latest else None)
            # The stamp goes into the code too: a state that returns to earlier content (a restore,
            # an undo) still gets a code of its own, so six characters always name one state.
            digest = hashlib.sha256(f"{stamp}{_fingerprint(entries)!r}".encode()).hexdigest()[:6]
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
        cutoff = time.strftime("%Y%m%dT%H%M%S000000Z", time.gmtime(time.time() - KEEP_DAYS * 86400))
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


# ---------------------------------------------------------------------------- comparing


@dataclass(slots=True)
class FileDiff:
    path: str
    role: str
    #: "changed", "same", "added" (only in the newer side), "removed" (only in the older side),
    #: or "unavailable" (its content was not kept on one side: missing, skipped, too large).
    change: str
    diff: str = ""
    note: str | None = None


def _text(data: bytes | None) -> list[str]:
    return [] if data is None else data.decode(errors="replace").splitlines(keepends=True)


def _unified(path: str, old: bytes | None, new: bytes | None) -> str:
    name = Path(path).name
    return "".join(difflib.unified_diff(_text(old), _text(new), fromfile=f"a/{name}", tofile=f"b/{name}"))


def _why_unavailable(entry: FileEntry) -> str:
    return "missing when this state was recorded" if entry.missing else (entry.skipped or "not kept")


def compare(
    older: list[tuple[FileEntry, bytes | None]], newer: list[tuple[FileEntry, bytes | None]]
) -> list[FileDiff]:
    """File by file, what turns ``older`` into ``newer``."""
    old_by_path = {entry.path: (entry, data) for entry, data in older}
    out: list[FileDiff] = []
    for entry, data in newer:
        before = old_by_path.pop(entry.path, None)
        if before is None:
            out.append(FileDiff(entry.path, entry.role, "added", _unified(entry.path, None, data)))
            continue
        old_entry, old_data = before
        if not entry.kept or not old_entry.kept:
            if (old_entry.sha256, old_entry.missing, old_entry.skipped) == (entry.sha256, entry.missing, entry.skipped):
                out.append(FileDiff(entry.path, entry.role, "same"))
            else:
                side = entry if not entry.kept else old_entry
                out.append(FileDiff(entry.path, entry.role, "unavailable", note=_why_unavailable(side)))
        elif old_entry.sha256 == entry.sha256:
            out.append(FileDiff(entry.path, entry.role, "same"))
        else:
            out.append(FileDiff(entry.path, entry.role, "changed", _unified(entry.path, old_data, data)))
    for old_entry, old_data in old_by_path.values():
        out.append(FileDiff(old_entry.path, old_entry.role, "removed", _unified(old_entry.path, old_data, None)))
    return out


def with_content(history: History, state: StateRecord) -> list[tuple[FileEntry, bytes | None]]:
    out: list[tuple[FileEntry, bytes | None]] = []
    for entry in state.files:
        try:
            out.append((entry, history.content(entry)))
        except HistoryError:
            out.append((FileEntry(entry.role, entry.path, entry.env, None, entry.size, entry.mode, skipped="content lost"), None))
    return out


# ---------------------------------------------------------------------------- restoring


@dataclass(slots=True)
class FileWrite:
    path: str
    role: str
    content: bytes = field(repr=False)


@dataclass(slots=True)
class RestorePlan:
    target: StateRecord
    writes: list[FileWrite]
    unchanged: list[str]
    #: Tracked now but not in the target state, typically a newer environment's file. Never deleted:
    #: the target's config.yaml stops naming it, exactly as removing that environment would.
    left_in_place: list[str]
    #: In the target state but without its content (missing then, skipped, lost): left as it is now.
    unavailable: list[dict[str, str]]
    #: The target's config.yaml predates the current layout and was upgraded on the way back.
    migrated: bool
    diffs: list[FileDiff]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.id,
            "writes": [w.path for w in self.writes],
            "unchanged": self.unchanged,
            "left_in_place": self.left_in_place,
            "unavailable": self.unavailable,
            "migrated": self.migrated,
            "diffs": [asdict(d) for d in self.diffs],
        }


def plan_restore(history: History, ref: str, files_now: list[TrackedFile], *, only: set[str] | None = None) -> RestorePlan:
    """What putting a state back would write, checked to load before anything is written.

    Only files whose content differs are written, and nothing is ever deleted. ``only`` limits the
    restore to some of the target's files (by path); the config that results must still load.
    """
    target = history.resolve(ref)
    now = read_files(files_now)
    now_by_path = {entry.path: (entry, data) for entry, data in now}
    target_files = with_content(history, target)
    chosen = [(e, d) for e, d in target_files if only is None or e.path in only]
    if only is not None and not chosen:
        raise RestoreRefused(f"none of {', '.join(sorted(only))} is part of state {target.id}")

    writes: list[FileWrite] = []
    unchanged: list[str] = []
    unavailable: list[dict[str, str]] = []
    migrated = False
    for entry, data in chosen:
        current = now_by_path.get(entry.path)
        if not entry.kept or data is None:
            unavailable.append({"path": entry.path, "why": _why_unavailable(entry)})
            continue
        if entry.role == ROLE_CONFIG:
            data, migrated = _config_to_write(data, target_files, target)
        if current is not None and current[0].sha256 == hashlib.sha256(data).hexdigest():
            unchanged.append(entry.path)
        else:
            writes.append(FileWrite(entry.path, entry.role, data))

    # The configuration this leaves must load: the restored config.yaml, or the current one when
    # it is not part of what is being put back.
    config_after = next((w.content for w in writes if w.role == ROLE_CONFIG), None)
    if config_after is None:
        current_config = next((data for entry, data in now if entry.role == ROLE_CONFIG), None)
        config_after = current_config
    if config_after is not None:
        try:
            parse_text(config_after.decode(errors="replace"), source=f"config.yaml of state {target.id}")
        except ConfigError as exc:
            raise RestoreRefused(f"state {target.id} cannot be restored: {exc}") from exc

    target_paths = {entry.path for entry in target.files}
    left_in_place = [path for path in now_by_path if path not in target_paths] if only is None else []
    newer = [(FileEntry(w.role, w.path, None, hashlib.sha256(w.content).hexdigest()), w.content) for w in writes]
    older = [now_by_path[w.path] if w.path in now_by_path else (FileEntry(w.role, w.path, None, None, missing=True), None) for w in writes]
    return RestorePlan(
        target=target,
        writes=writes,
        unchanged=unchanged,
        left_in_place=left_in_place,
        unavailable=unavailable,
        migrated=migrated,
        diffs=compare(older, newer),
    )


def _config_to_write(data: bytes, files: list[tuple[FileEntry, bytes | None]], target: StateRecord) -> tuple[bytes, bool]:
    """The target's config.yaml as it can be written today: byte for byte when it is already in the
    current layout, upgraded otherwise — with the aliases read from the target's own SSH files,
    since the files on disk now may not be the ones that config was written against."""
    raw = _yaml(data)
    if raw is None or migrate.file_version(raw) >= CONFIG_VERSION:
        return data, False
    aliases: dict[str, list[str]] = {}
    for entry, content in files:
        if entry.role == ROLE_SSH and entry.env and content is not None:
            aliases.setdefault(entry.env, []).extend(sorted(_host_patterns(content)))
    try:
        upgraded = migrate.upgrade_mapping(raw, aliases)
    except (ConfigError, ValueError, TypeError, KeyError) as exc:
        raise RestoreRefused(f"state {target.id} holds a config.yaml too old to upgrade: {exc}") from exc
    return yaml.safe_dump(upgraded, sort_keys=False, allow_unicode=True, width=100).encode(), True


def restored_reason(target: StateRecord) -> str:
    """How a restore is labelled in history: which state, and what that state was."""
    reason = target.reason if len(target.reason) <= 70 else target.reason[:69] + "…"
    return f"restored {target.code}: {reason}"


def apply_restore(plan: RestorePlan, paths: Paths) -> list[str]:
    """Write what the plan says: SSH files first (each keeps its own backup), config.yaml last, so
    a reader that sees the new config.yaml finds the files it names already in place."""
    written: list[str] = []
    for write in sorted(plan.writes, key=lambda w: w.role == ROLE_CONFIG):
        path = Path(write.path)
        if write.role == ROLE_CONFIG:
            if path.exists():
                atomic_write(path.with_suffix(path.suffix + ".bak"), path.read_bytes(), mode=0o600)
            atomic_write(path, write.content, mode=0o600)
        else:
            replace_file(path, write.content, paths.ssh_backups)
        written.append(write.path)
    return written


def record_now(config: Config, paths: Paths, *, reason: str, source: str, **extra: Any) -> tuple[StateRecord, bool]:
    """Record the configuration as it is on disk now. The CLI's entry point."""
    return History(paths.history_dir).record(tracked_files(config, paths), reason=reason, source=source, **extra)
