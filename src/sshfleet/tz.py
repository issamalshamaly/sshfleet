"""Time zones for display.

``logs.display_tz`` is an IANA name or ``"local"``. The local zone is worked out the way
programs on the machine do it: ``$TZ`` first, then the ``/etc/localtime`` symlink, then
``/etc/timezone``; UTC when none of them says anything usable.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOCAL = "local"


def _valid(name: str | None) -> str | None:
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


def local_zone_name() -> str:
    """The machine's zone as an IANA name, or ``UTC`` when it cannot be determined."""
    env = os.environ.get("TZ", "").lstrip(":")
    if _valid(env):
        return env
    try:
        target = Path("/etc/localtime").resolve()
        if "zoneinfo" in target.parts:
            candidate = "/".join(target.parts[target.parts.index("zoneinfo") + 1 :])
            if _valid(candidate):
                return candidate
    except OSError:
        pass
    try:
        text = Path("/etc/timezone").read_text().strip()
        if _valid(text):
            return text
    except OSError:
        pass
    return "UTC"


def resolve_name(name: str | None) -> str:
    """Turn a config value into a concrete IANA name (``local``/empty/unknown -> resolved)."""
    if not name or name == LOCAL:
        return local_zone_name()
    return name if _valid(name) else "UTC"


def zone_for(name: str | None) -> ZoneInfo:
    return ZoneInfo(resolve_name(name))
