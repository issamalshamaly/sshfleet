"""Edit host stanzas inside the operator's own SSH config files.

Only the lines of the one ``Host`` stanza being changed are ever touched: comments, ordering,
blank lines, indentation and every other stanza survive byte for byte. The files stay plain
OpenSSH, so ``ssh <alias>`` keeps working outside the tool.

Every write leaves a timestamped backup **in the tool's state directory**, never beside the
original: ``Include ~/.ssh/config.d/*`` would read a stray ``.bak`` as real configuration and
duplicate every host in it. The temporary file used for the atomic replace is dot-prefixed for
the same reason — ``glob(3)`` does not match a leading dot with ``*``.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

#: Keywords this module manages. Anything else in a stanza is left exactly as it is.
CANONICAL = {
    "hostname": "HostName",
    "user": "User",
    "port": "Port",
    "proxyjump": "ProxyJump",
    "identityfile": "IdentityFile",
}
ORDER = ("hostname", "user", "port", "proxyjump", "identityfile")

ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
ADDRESS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,253}$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,31}$")
_SPLIT = re.compile(r"\s*(?:=|\s)\s*")
_BACKUP_KEEP = 20


class SshEditError(Exception):
    """The edit is invalid, or the file does not look the way the caller expected."""


@dataclass(slots=True)
class HostEdit:
    """Wanted state of one stanza.

    ``None`` leaves a keyword untouched, ``""`` removes its line. Values are validated before
    anything is written: a value that could break the file, or smuggle a second keyword onto a
    line, is refused rather than quoted.
    """

    hostname: str | None = None
    user: str | None = None
    port: str | None = None
    proxy_jump: str | None = None
    identity_file: str | None = None

    def items(self) -> list[tuple[str, str]]:
        pairs = [
            ("hostname", self.hostname),
            ("user", self.user),
            ("port", self.port),
            ("proxyjump", self.proxy_jump),
            ("identityfile", self.identity_file),
        ]
        return [(keyword, value) for keyword, value in pairs if value is not None]

    def is_empty(self) -> bool:
        return not self.items()


# --------------------------------------------------------------------------- validation


def validate_alias(alias: str) -> str:
    alias = alias.strip()
    if not ALIAS_RE.match(alias):
        raise SshEditError(
            f"{alias!r} is not a usable host alias — letters, digits, dot, dash and underscore only"
        )
    if any(ch in alias for ch in "*?!"):  # pragma: no cover - excluded by the pattern
        raise SshEditError("a managed host cannot be a wildcard pattern")
    return alias


def validate_edit(edit: HostEdit) -> HostEdit:
    """Check every supplied value; returns the edit with values stripped."""
    for keyword, value in edit.items():
        if value == "":
            continue
        text = value.strip()
        if not text or any(ch.isspace() for ch in text):
            raise SshEditError(f"{CANONICAL[keyword]} value {value!r} must be a single word")
        if keyword == "hostname" and not ADDRESS_RE.match(text):
            raise SshEditError(f"{text!r} is not a valid host name or address")
        if keyword == "user" and not USER_RE.match(text):
            raise SshEditError(f"{text!r} is not a valid user name")
        if keyword == "proxyjump" and not ALIAS_RE.match(text):
            raise SshEditError(f"{text!r} is not a valid alias to connect through")
        if keyword == "port":
            try:
                port = int(text)
            except ValueError:
                raise SshEditError(f"port {text!r} is not a number") from None
            if not 1 <= port <= 65535:
                raise SshEditError(f"port {port} is out of range")
    return HostEdit(
        hostname=_clean(edit.hostname),
        user=_clean(edit.user),
        port=_clean(edit.port),
        proxy_jump=_clean(edit.proxy_jump),
        identity_file=_clean(edit.identity_file),
    )


def _clean(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) else None


# ------------------------------------------------------------------------------ reading


def _split(line: str) -> tuple[str, str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return "", ""
    parts = _SPLIT.split(stripped, maxsplit=1)
    return parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")


def aliases_in(text: str) -> list[str]:
    """Every pattern named on a ``Host`` line, in file order."""
    out: list[str] = []
    for line in text.splitlines():
        keyword, value = _split(line)
        if keyword == "host":
            out.extend(value.split())
    return out


def find_stanza(lines: list[str], alias: str) -> tuple[int, int] | None:
    """Half-open line range of the stanza that declares ``alias``, or ``None``."""
    start: int | None = None
    for index, line in enumerate(lines):
        keyword, value = _split(line)
        if keyword not in {"host", "match"}:
            continue
        if start is not None:
            return start, index
        if keyword == "host" and alias in value.split():
            start = index
    return (start, len(lines)) if start is not None else None


def stanza_patterns(lines: list[str], start: int) -> list[str]:
    return _split(lines[start])[1].split()


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(errors="replace").splitlines(keepends=True)


# ------------------------------------------------------------------------------ writing


def render_stanza(alias: str, edit: HostEdit, indent: str = "    ") -> list[str]:
    lines = [f"Host {alias}\n"]
    values = dict(edit.items())
    for keyword in ORDER:
        value = values.get(keyword)
        if value:
            lines.append(f"{indent}{CANONICAL[keyword]} {value}\n")
    return lines


def _indent_of(block: list[str]) -> str:
    for line in block[1:]:
        if line.strip() and not line.lstrip().startswith("#"):
            leading = line[: len(line) - len(line.lstrip())]
            if leading:
                return leading.replace("\t", "    ")
    return "    "


def apply_edit(block: list[str], edit: HostEdit) -> list[str]:
    """Apply an edit to one stanza's lines, keeping every unmanaged line where it is."""
    out = list(block)
    indent = _indent_of(out)
    for keyword, value in edit.items():
        found = next(
            (i for i in range(1, len(out)) if _split(out[i])[0] == keyword),
            None,
        )
        if value == "":
            if found is not None:
                del out[found]
            continue
        rendered = f"{indent}{CANONICAL[keyword]} {value}\n"
        if found is not None:
            out[found] = rendered
        else:
            last = max((i for i, line in enumerate(out) if line.strip()), default=0)
            out.insert(last + 1, rendered)
    return out


def backup_path(path: Path, backups_dir: Path) -> Path:
    # Milliseconds too: several edits can land inside one second and each deserves its own copy.
    now = time.time()
    stamp = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}{int(now * 1000) % 1000:03d}"
    return backups_dir / f"{path.name}.{stamp}.bak"


def _prune_backups(path: Path, backups_dir: Path) -> None:
    existing = sorted(backups_dir.glob(f"{path.name}.*.bak"))
    for old in existing[:-_BACKUP_KEEP]:
        old.unlink(missing_ok=True)


def write_lines(path: Path, lines: list[str], backups_dir: Path) -> Path | None:
    """Replace the file atomically, after copying the current version to a backup.

    A file that does not exist yet is created (private, in a private directory) and there is
    nothing to back up, so None comes back.
    """
    backups_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = 0o600
    backup: Path | None = None
    if path.exists():
        mode = path.stat().st_mode & 0o777
        backup = backup_path(path, backups_dir)
        backup.write_bytes(path.read_bytes())
        os.chmod(backup, 0o600)
        _prune_backups(path, backups_dir)

    # Dot-prefixed so an `Include …/config.d/*` running at this instant cannot pick it up.
    tmp = path.with_name(f".{path.name}.sshfleet-tmp")
    tmp.write_text("".join(lines))
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return backup


# ------------------------------------------------------------------------- operations


def add_host(path: Path, alias: str, edit: HostEdit, backups_dir: Path) -> Path | None:
    """Append a new stanza at the end of the file. Refuses a duplicate alias."""
    alias = validate_alias(alias)
    edit = validate_edit(edit)
    lines = read_lines(path)
    if alias in aliases_in("".join(lines)):
        raise SshEditError(f"{path.name} already declares a host called {alias!r}")

    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    stanza = render_stanza(alias, edit)
    cut = _first_wildcard(lines)
    if cut is not None:
        # A trailing `Host *` block applies to everything; keep the new host above it so the
        # file reads the way a person would have written it.
        return write_lines(path, lines[:cut] + stanza + ["\n"] + lines[cut:], backups_dir)
    separator = ["\n"] if lines and lines[-1].strip() else []
    return write_lines(path, lines + separator + stanza, backups_dir)


def _first_wildcard(lines: list[str]) -> int | None:
    """Index of the first pattern stanza (``Host *``), which must stay last in the file."""
    for index, line in enumerate(lines):
        keyword, value = _split(line)
        if keyword == "host" and any(ch in pattern for pattern in value.split() for ch in "*?!"):
            return index
    return None


def update_host(
    path: Path,
    alias: str,
    edit: HostEdit,
    backups_dir: Path,
    *,
    new_alias: str | None = None,
) -> Path | None:
    """Change keywords inside an existing stanza, and optionally rename it."""
    alias = validate_alias(alias)
    edit = validate_edit(edit)
    lines = read_lines(path)
    span = find_stanza(lines, alias)
    if span is None:
        raise SshEditError(f"{path.name} has no Host stanza for {alias!r}")
    start, end = span

    patterns = stanza_patterns(lines, start)
    if len(patterns) > 1 and new_alias:
        raise SshEditError(
            f"{alias!r} shares its stanza with {', '.join(p for p in patterns if p != alias)} — rename it by hand"
        )
    block = apply_edit(lines[start:end], edit)
    if new_alias:
        renamed = validate_alias(new_alias)
        if renamed != alias and renamed in aliases_in("".join(lines)):
            raise SshEditError(f"{path.name} already declares a host called {renamed!r}")
        block[0] = f"Host {renamed}\n"
    return write_lines(path, lines[:start] + block + lines[end:], backups_dir)


def remove_host(path: Path, alias: str, backups_dir: Path) -> Path | None:
    """Delete a stanza. A stanza shared with other aliases loses only this alias."""
    alias = validate_alias(alias)
    lines = read_lines(path)
    span = find_stanza(lines, alias)
    if span is None:
        raise SshEditError(f"{path.name} has no Host stanza for {alias!r}")
    start, end = span

    patterns = stanza_patterns(lines, start)
    if len(patterns) > 1:
        remaining = [p for p in patterns if p != alias]
        lines[start] = f"Host {' '.join(remaining)}\n"
        return write_lines(path, lines, backups_dir)

    rest = lines[end:]
    head = lines[:start]
    # Keep the file tidy: no double blank line where the stanza used to be, and no trailing one.
    while head and not head[-1].strip() and rest and not rest[0].strip():
        rest = rest[1:]
    while not rest and head and not head[-1].strip():
        head = head[:-1]
    return write_lines(path, head + rest, backups_dir)
