"""Who may open the page.

The page carries the session token, and the token is the whole API, the terminal included. So
the page is only served to a browser that has been let in: one that opened a launch link
(``/?launch=…``) and was given a cookie for it. Anyone else on the machine who asks for
``http://127.0.0.1:8765/`` gets a page that says how to get in, and nothing more.

A launch link works once, within ten minutes. It is what ``sshfleet web`` opens in the browser,
so the address that lands in a process list, the browser's history or a terminal's scrollback is
spent by the time anyone else could read it.

The token and the cookie's value are kept in ``web-session.json`` in the state directory, mode
0600. A bookmark and a tab left open both keep working across a restart, and ``sshfleet web`` or
``sshfleet desktop`` run again can ask the running server for a fresh link. Deleting the file
signs every browser out at the next start.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..fsutil import atomic_write

FILE_NAME = "web-session.json"
#: How long a launch link waits to be opened.
LAUNCH_TTL_S = 600
#: How long a browser stays in without opening a launch link again; renewed on every page load.
COOKIE_MAX_AGE_S = 30 * 86400
#: Links nobody opened are dropped past this many, oldest first.
MAX_LIVE_CODES = 32


@dataclass(frozen=True, slots=True)
class SessionKeys:
    #: Sent by the page in a header on every API call; also in the address of the terminal's
    #: WebSocket and of the two download routes, which cannot carry a header.
    token: str
    #: The cookie's value: what lets a browser have the page, and so the token.
    cookie: str


def load_keys(path: Path) -> SessionKeys | None:
    """The keys in the file, or None when there are none to trust: missing, unreadable, not a
    plain file, someone else's, or readable by anyone else."""
    try:
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        token, cookie = data["token"], data["cookie"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not all(isinstance(value, str) and len(value) >= 24 for value in (token, cookie)):
        return None
    return SessionKeys(token=token, cookie=cookie)


def ensure_keys(path: Path) -> tuple[SessionKeys, str | None]:
    """The keys in the file, made and kept there when there are none to trust. The second value
    is a warning when they could not be kept, in which case they last as long as the process."""
    keys = load_keys(path)
    if keys is not None:
        return keys, None
    keys = SessionKeys(token=secrets.token_urlsafe(24), cookie=secrets.token_urlsafe(32))
    try:
        atomic_write(path, json.dumps({"token": keys.token, "cookie": keys.cookie}) + "\n", mode=0o600)
    except OSError as exc:
        return keys, f"web session: could not keep {path} ({exc}); browsers are signed out at every restart"
    return keys, None


def matches(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    # Constant time, so a value cannot be guessed a character at a time from response timings.
    return secrets.compare_digest(provided.encode(), expected.encode())


def cookie_name(port: int | None) -> str:
    """One per port: browsers keep cookies by host alone, so two servers on 127.0.0.1 would
    otherwise sign each other's browsers out."""
    return f"sshfleet-{port or 80}"


class LaunchCodes:
    """Single-use codes, each good for :data:`LAUNCH_TTL_S`. Only their hashes are kept."""

    def __init__(self, ttl_s: float = LAUNCH_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._live: dict[str, float] = {}

    def issue(self) -> str:
        now = self._clock()
        self._live = {digest: until for digest, until in self._live.items() if until > now}
        while len(self._live) >= MAX_LIVE_CODES:
            self._live.pop(next(iter(self._live)))
        code = secrets.token_urlsafe(24)
        self._live[_digest(code)] = now + self.ttl_s
        return code

    def redeem(self, code: str | None) -> bool:
        if not code:
            return False
        until = self._live.pop(_digest(code), None)
        return until is not None and until > self._clock()


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def read_token(path: Path) -> str | None:
    """For another sshfleet process on this machine: the token of the server that keeps its keys
    in ``path``, if there is one this user may use."""
    keys = load_keys(path)
    return keys.token if keys else None
