"""Listing and previewing remote files.

``find -printf`` with NUL records gives one round-trip and survives file names containing
spaces, quotes or newlines — which a ``ls -l`` parser does not.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import UTC, datetime

from ..inventory import Host
from ..ssh import SshRunner
from .denylist import is_denied, reason_denied

RECORD = "%y\\t%s\\t%T@\\t%m\\t%u\\t%g\\t%f\\t%l\\0"


@dataclass(slots=True)
class RemoteEntry:
    name: str
    kind: str  # "d" directory, "f" file, "l" symlink
    size: int
    mtime: float
    mode: str
    owner: str
    group: str
    link_target: str = ""
    denied_reason: str | None = None

    @property
    def is_dir(self) -> bool:
        return self.kind == "d"

    @property
    def denied(self) -> bool:
        return self.denied_reason is not None

    @property
    def modified(self) -> datetime:
        return datetime.fromtimestamp(self.mtime, UTC)


@dataclass(slots=True)
class Listing:
    path: str
    entries: list[RemoteEntry]
    used_sudo: bool = False
    truncated: bool = False


def _list_script(path: str, *, use_sudo: bool, limit: int) -> str:
    quoted = shlex.quote(path)
    sudo = "sudo -n " if use_sudo else ""
    return (
        "# sshfleet:list\n"
        "export LC_ALL=C\n"
        f"test -d {quoted} || {{ echo 'SSHFLEET_ERR not_a_directory' >&2; exit 90; }}\n"
        f"{sudo}find {quoted} -mindepth 1 -maxdepth 1 -printf '{RECORD}' 2>/dev/null | head -c 4000000\n"
    )


def parse_listing(raw: str, path: str, deny_extra: list[str] | None = None, limit: int = 5000) -> Listing:
    entries: list[RemoteEntry] = []
    records = [r for r in raw.split("\0") if r.strip()]
    truncated = len(records) > limit
    for record in records[:limit]:
        fields = record.split("\t")
        if len(fields) < 7:
            continue
        kind, size, mtime, mode, owner, group, name = fields[:7]
        link = fields[7] if len(fields) > 7 else ""
        full = path.rstrip("/") + "/" + name
        try:
            entries.append(
                RemoteEntry(
                    name=name,
                    kind=kind if kind in {"d", "f", "l"} else "f",
                    size=int(size or 0),
                    mtime=float(mtime or 0),
                    mode=mode,
                    owner=owner,
                    group=group,
                    link_target=link,
                    denied_reason=reason_denied(full, deny_extra),
                )
            )
        except ValueError:
            continue
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return Listing(path=path, entries=entries, truncated=truncated)


async def list_dir(
    runner: SshRunner,
    host: Host,
    path: str,
    *,
    deny_extra: list[str] | None = None,
    allow_sudo: bool = True,
    limit: int = 5000,
) -> Listing:
    """List a directory, retrying with sudo when permission is denied."""
    result = await runner.run_script(host, _list_script(path, use_sudo=False, limit=limit), timeout=25)
    if result.ok and result.out.strip():
        return parse_listing(result.out, path, deny_extra, limit)

    if "not_a_directory" in result.err:
        raise NotADirectoryError(f"{host.alias}:{path} is not a directory")

    if allow_sudo and host.sudo:
        sudo_result = await runner.run_script(host, _list_script(path, use_sudo=True, limit=limit), timeout=25)
        if sudo_result.ok:
            listing = parse_listing(sudo_result.out, path, deny_extra, limit)
            listing.used_sudo = True
            return listing

    if not result.ok:
        error = result.error()
        raise OSError(f"{host.alias}:{path}: {error.message if error else 'listing failed'}")
    return parse_listing(result.out, path, deny_extra, limit)


async def preview(
    runner: SshRunner,
    host: Host,
    path: str,
    *,
    max_bytes: int = 65536,
    deny_extra: list[str] | None = None,
) -> str:
    """First bytes of a text file. Refuses anything on the deny list."""
    from .denylist import assert_allowed

    assert_allowed(path, deny_extra)
    quoted = shlex.quote(path)
    sudo = "sudo -n " if host.sudo else ""
    script = (
        "# sshfleet:preview\n"
        "export LC_ALL=C\n"
        f"test -r {quoted} && head -c {max_bytes} -- {quoted} || {sudo}head -c {max_bytes} -- {quoted}\n"
    )
    result = await runner.run_script(host, script, timeout=25)
    if not result.ok:
        error = result.error()
        raise OSError(f"{host.alias}:{path}: {error.message if error else 'preview failed'}")
    return result.out


async def stat_file(runner: SshRunner, host: Host, path: str) -> tuple[int, str, str, str] | None:
    """``(size, mode, owner, group)`` or None when the path does not exist."""
    quoted = shlex.quote(path)
    sudo = "sudo -n " if host.sudo else ""
    script = (
        "# sshfleet:stat\n"
        f"stat -c '%s %a %U %G' -- {quoted} 2>/dev/null || {sudo}stat -c '%s %a %U %G' -- {quoted} 2>/dev/null\n"
    )
    result = await runner.run_script(host, script, timeout=20)
    parts = result.out.split()
    if len(parts) != 4:
        return None
    try:
        return int(parts[0]), parts[1], parts[2], parts[3]
    except ValueError:
        return None


def filter_visible(listing: Listing, show_hidden: bool = False) -> list[RemoteEntry]:
    return [e for e in listing.entries if show_hidden or not e.name.startswith(".")]


__all__ = ["Listing", "RemoteEntry", "filter_visible", "is_denied", "list_dir", "parse_listing", "preview", "stat_file"]
