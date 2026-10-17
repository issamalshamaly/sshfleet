"""Minimal OpenSSH client-config parser.

Only the keywords the tool needs are kept (``HostName``, ``User``, ``Port``, ``ProxyJump``,
``IdentityFile`` — the *path*, never the file's content). Keywords are case-insensitive and may
be separated by whitespace or ``=``; ``Include`` globs are followed once (relative includes
resolve against ``~/.ssh`` like OpenSSH does).
"""

from __future__ import annotations

import contextlib
import glob
import re
from dataclasses import dataclass, field
from pathlib import Path

_WANTED = {"hostname", "user", "port", "proxyjump", "identityfile"}

#: Files in ~/.ssh that are never read as configuration, matched by name before anything is
#: opened. Key material must not be read even by accident — an ``Include ~/.ssh/*`` would
#: otherwise walk straight into the private keys. Backups and public keys are not configuration
#: either.
NOT_CONFIG = ("*.pem", "id_*", "*.key", "*.ppk", "*.pub", "known_hosts*", "authorized_keys*", "*.bak", "agent*")

#: What key material looks like inside a file, whatever it is called. Checked before any file's
#: content is kept or handed out (history, export), as a second line behind the names above.
_KEY_MARKERS = (b"PRIVATE KEY-----", b"PuTTY-User-Key-File", b"-----BEGIN OPENSSH")


def looks_like_key(path: Path) -> bool:
    """A file never to open as SSH configuration: a key, a public key, a backup, an agent socket."""
    return any(path.match(pattern) for pattern in NOT_CONFIG)


def holds_key_material(data: bytes) -> bool:
    return any(marker in data for marker in _KEY_MARKERS)
_SPLIT = re.compile(r"\s*(?:=|\s)\s*")


@dataclass(slots=True)
class HostStanza:
    """One ``Host`` block, flattened to the keywords we care about."""

    patterns: list[str]
    hostname: str | None = None
    user: str | None = None
    port: int | None = None
    proxy_jump: str | None = None
    identity_files: list[str] = field(default_factory=list)
    source: Path | None = None
    line: int = 0

    @property
    def alias(self) -> str:
        return self.patterns[0]

    @property
    def is_wildcard(self) -> bool:
        return any(ch in p for p in self.patterns for ch in "*?!")


def _tokens(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = _SPLIT.split(stripped, maxsplit=1)
    if len(parts) != 2 or not parts[1]:
        return None
    return parts[0].lower(), parts[1].strip()


def parse_ssh_config(path: Path, *, _seen: set[Path] | None = None) -> list[HostStanza]:
    """Parse one config file (following ``Include``) into concrete host stanzas.

    Wildcard stanzas (``Host *``) are dropped from the result but their values are *not*
    applied to other hosts — the tool only needs per-host identity, and OpenSSH itself still
    applies the real file's wildcards when it connects.
    """
    seen = _seen if _seen is not None else set()
    path = path.expanduser()
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - unreadable symlink
        resolved = path
    if resolved in seen:
        return []
    seen.add(resolved)

    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        raise FileNotFoundError(f"cannot read ssh config {path}: {exc}") from exc
    # Named like configuration but holding a key (an environment pointed at the wrong file): the
    # names above cannot catch that, so the content is looked at once and the file dropped.
    if holds_key_material(text.encode(errors="ignore")):
        raise OSError(f"{path} holds a private key, not SSH configuration; it is not read")

    stanzas: list[HostStanza] = []
    current: HostStanza | None = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        parsed = _tokens(raw)
        if parsed is None:
            continue
        keyword, value = parsed

        if keyword == "host":
            current = HostStanza(patterns=value.split(), source=path, line=lineno)
            stanzas.append(current)
            continue

        if keyword == "include":
            for pattern in value.split():
                expanded = Path(pattern).expanduser()
                if not expanded.is_absolute():
                    expanded = path.parent / pattern if pattern.startswith(("./", "../")) else Path.home() / ".ssh" / pattern
                for found in sorted(glob.glob(str(expanded))):
                    found_path = Path(found)
                    if found_path.is_file() and not looks_like_key(found_path):
                        stanzas.extend(parse_ssh_config(found_path, _seen=seen))
            continue

        if keyword == "match":
            current = None  # values inside a Match block are conditional; ignore them
            continue

        if current is None or keyword not in _WANTED:
            continue

        if keyword == "hostname":
            current.hostname = value
        elif keyword == "user":
            current.user = value
        elif keyword == "port":
            with contextlib.suppress(ValueError):
                current.port = int(value)
        elif keyword == "proxyjump":
            current.proxy_jump = value.split(",")[0].strip()  # only the first hop matters to us
        elif keyword == "identityfile":
            current.identity_files.append(value.strip('"'))

    return [s for s in stanzas if not s.is_wildcard]


def included_files(path: Path, *, _seen: set[Path] | None = None) -> list[Path]:
    """Every file that parsing ``path`` reads, itself first, following ``Include`` once each.

    The watcher stats exactly this list, so a host added to an included file is noticed just
    like one added to the file that was registered.
    """
    seen = _seen if _seen is not None else set()
    path = path.expanduser()
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - unreadable symlink
        resolved = path
    if resolved in seen:
        return []
    seen.add(resolved)
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []

    out = [path]
    for raw in text.splitlines():
        parsed = _tokens(raw)
        if parsed is None or parsed[0] != "include":
            continue
        for pattern in parsed[1].split():
            expanded = Path(pattern).expanduser()
            if not expanded.is_absolute():
                expanded = path.parent / pattern if pattern.startswith(("./", "../")) else Path.home() / ".ssh" / pattern
            for found in sorted(glob.glob(str(expanded))):
                found_path = Path(found)
                if found_path.is_file() and not looks_like_key(found_path):
                    out.extend(included_files(found_path, _seen=seen))
    return out


def summarize(stanzas: list[HostStanza]) -> dict[str, object]:
    """Human-facing summary used by the ``env add`` wizard."""
    jumps: dict[str, int] = {}
    users: dict[str, int] = {}
    without_key = 0
    for stanza in stanzas:
        if stanza.proxy_jump:
            jumps[stanza.proxy_jump] = jumps.get(stanza.proxy_jump, 0) + 1
        if stanza.user:
            users[stanza.user] = users.get(stanza.user, 0) + 1
        if not stanza.identity_files:
            without_key += 1
    return {
        "hosts": len(stanzas),
        "proxy_jumps": dict(sorted(jumps.items(), key=lambda kv: -kv[1])),
        "users": dict(sorted(users.items(), key=lambda kv: -kv[1])),
        "without_identity_file": without_key,
        "identity_files": sorted({f for s in stanzas for f in s.identity_files}),
    }


def common_prefix(aliases: list[str]) -> str | None:
    """Longest shared alias prefix ending in ``-`` (``acme-prod-`` for the production file)."""
    if len(aliases) < 2:
        return None
    shortest = min(aliases, key=len)
    end = 0
    for index in range(len(shortest)):
        char = shortest[index]
        if any(alias[index] != char for alias in aliases):
            break
        if char == "-":
            end = index + 1
    prefix = shortest[:end]
    return prefix if len(prefix) >= 4 else None
