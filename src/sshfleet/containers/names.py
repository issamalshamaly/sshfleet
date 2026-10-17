"""What a container name may look like, checked before a name is let near a shell.

Every name is quoted on its way into a remote command as well; this is the second lock. A name
that docker or podman would never have produced has no business in a command at all, however
carefully it is quoted.
"""

from __future__ import annotations

import re

#: Container names the runtimes themselves accept.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

#: The same, with ``*`` as a wildcard: a log source may name a container it can only guess,
#: such as ``*kong*`` for a hand-deployed gateway whose name changes with every deploy.
GLOB_RE = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9_.*-]{0,127}$")


def check_container_name(name: str, *, glob: bool = False) -> str:
    """Return ``name`` if it is a container name (or, with ``glob``, a wildcard for one)."""
    pattern = GLOB_RE if glob else NAME_RE
    if not isinstance(name, str) or not pattern.match(name):
        raise ValueError(f"{name!r} is not a container name")
    return name
