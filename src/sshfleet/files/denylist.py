"""Paths sshfleet will never read, preview or download.

Servers often keep deploy-time secrets next to the application: ``/home/ubuntu/order.env.production``,
``jwt_private_key.pem`` and friends. Downloading one would copy a production secret onto a laptop,
so the check lives here — in the transfer and preview code paths, not in the UI — and cannot be
switched off.
"""

from __future__ import annotations

import fnmatch
import posixpath

#: Cannot be disabled or overridden by configuration.
HARD_DENY: tuple[str, ...] = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.env",
    "*.env.*",
    ".env*",
    "id_rsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "id_dsa*",
    "authorized_keys",
    "known_hosts",
    "*.kdbx",
    "credentials",
    "credentials.*",
    ".netrc",
    ".pgpass",
    ".htpasswd",
    "shadow",
    "*secret*",
    "*.keystore",
    "*.truststore",
)

#: Directories whose contents are off limits regardless of file name.
HARD_DENY_DIRS: tuple[str, ...] = (
    "/etc/ssl/private",
    "/etc/shadow",
    "/root/.ssh",
    "/home/*/.ssh",
    "/home/*/.aws",
    "/root/.aws",
    "/var/lib/jenkins/secrets",
)


class DeniedPath(Exception):
    """Raised when a path matches the deny list."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _matches(name: str, patterns: tuple[str, ...] | list[str]) -> str | None:
    lowered = name.lower()
    for pattern in patterns:
        if fnmatch.fnmatch(lowered, pattern.lower()):
            return pattern
    return None


def reason_denied(path: str, extra: list[str] | None = None) -> str | None:
    """Why this path is refused, or None when it is allowed."""
    clean = posixpath.normpath(path.strip())
    base = posixpath.basename(clean)

    for directory in HARD_DENY_DIRS:
        if fnmatch.fnmatch(clean, directory) or fnmatch.fnmatch(clean, directory.rstrip("/") + "/*"):
            return f"{directory} holds credentials"

    pattern = _matches(base, HARD_DENY)
    if pattern:
        return f"matches {pattern} (secret material)"

    if extra:
        pattern = _matches(base, extra) or _matches(clean, extra)
        if pattern:
            return f"matches {pattern} from files.deny_extra"
    return None


def is_denied(path: str, extra: list[str] | None = None) -> bool:
    return reason_denied(path, extra) is not None


def assert_allowed(path: str, extra: list[str] | None = None) -> None:
    reason = reason_denied(path, extra)
    if reason:
        raise DeniedPath(path, reason)
