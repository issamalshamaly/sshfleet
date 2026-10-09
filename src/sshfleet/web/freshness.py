"""Is the running server older than the code on disk?

The web assets are read from disk on every request, but the Python that answers the API was
imported once at startup. Rebuild the interface without restarting and the browser gets the new
page talking to the old routes — which fails as a puzzling "Method Not Allowed" rather than as
anything that names the cause. So the server compares itself against what is on disk and says so.
"""

from __future__ import annotations

import time
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent

#: Re-walking the package on every request would be waste; it changes only when someone edits it.
_CACHE_TTL_S = 5.0
_cached: tuple[float, float] = (0.0, 0.0)  # (checked at, newest mtime)


def newest_source_mtime() -> float:
    """The newest mtime among the package's own Python files and its built web assets."""
    global _cached
    now = time.time()
    checked_at, mtime = _cached
    if now - checked_at < _CACHE_TTL_S:
        return mtime

    newest = 0.0
    for path in PACKAGE.rglob("*"):
        if path.suffix in {".py", ".js", ".css", ".html"} and path.is_file():
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:  # a file removed mid-walk is not a reason to fail a page load
                continue
    _cached = (now, newest)
    return newest


def is_stale(started_at: float) -> bool:
    """True when sshfleet was changed on disk after this process imported it."""
    return newest_source_mtime() > started_at
