"""Writing a file so that no reader, crash or symlink ever sees half of it.

Every configuration file sshfleet writes goes through here: ``config.yaml``, the operator's SSH
config files, and what history and restore put back.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def real_path(path: Path) -> Path:
    """The file a path really names. A dotfile manager keeps the real file elsewhere and links to
    it, and writing over the link would quietly fork the two."""
    return Path(os.path.realpath(path.expanduser()))


def fsync_dir(directory: Path) -> None:
    """Make a rename in ``directory`` durable. Some filesystems cannot, and that is not an error."""
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path: Path, data: bytes | str, *, mode: int | None = None) -> Path:
    """Replace the file at ``path`` with ``data`` in one step; returns the file actually written.

    The new content is written to a dot-prefixed file beside the target (``glob(3)``'s ``*`` skips
    it, so an ``Include config.d/*`` read at that instant never sees it), flushed to disk, renamed
    over the target, and the directory is flushed after. ``mode`` defaults to the current file's
    permissions, or 0600 for a new one.
    """
    target = real_path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if mode is None:
        try:
            mode = target.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600
    payload = data.encode() if isinstance(data, str) else data
    tmp = target.with_name(f".{target.name}.sshfleet-tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    fsync_dir(target.parent)
    return target
