"""Container commands, restricted to the ones that cannot take a service down.

The tool restarts containers in place and never stops, kills or prunes them. The one removal
it allows is of a container that is *already stopped*, and only through
:func:`sshfleet.containers.actions.remove`, which checks the state on the host first. That is
enforced here rather than in the UI: :class:`RemoteCommand` refuses to build anything outside
``ALLOWED_VERBS``, ``rm`` needs the ``guarded`` flag that only the remove action sets, and a
test asserts no destructive verb appears anywhere else in the source.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import StrEnum

from ..inventory import Host
from .names import check_container_name

#: Read-only inspection plus the single in-place action the tool offers.
ALLOWED_VERBS: frozenset[str] = frozenset(
    {"ps", "inspect", "stats", "logs", "restart", "image", "version", "exec"}
)

#: Buildable only by the remove action, for a container that is not running.
GUARDED_VERBS: frozenset[str] = frozenset({"rm"})

#: Never buildable, for any reason.
FORBIDDEN_VERBS: frozenset[str] = frozenset(
    {"stop", "remove", "kill", "prune", "down", "create", "run", "start", "pause", "unpause", "update", "rename"}
)


class Runtime(StrEnum):
    DOCKER = "docker"
    PODMAN = "podman"
    NONE = "none"


class ForbiddenCommand(Exception):
    """Raised when something tries to build a destructive container command."""


@dataclass(slots=True)
class RemoteCommand:
    """A container command for one host, with the runtime and sudo decided by the host."""

    host: Host
    verb: str
    args: tuple[str, ...] = ()
    runtime: str | None = None
    guarded: bool = False  # set only by remove_command(); lets ``rm`` through

    def __post_init__(self) -> None:
        if self.verb in FORBIDDEN_VERBS:
            raise ForbiddenCommand(
                f"{self.verb!r} is not available: sshfleet only restarts containers in place"
            )
        if self.verb in GUARDED_VERBS:
            if not self.guarded:
                raise ForbiddenCommand(f"{self.verb!r} is only available for a stopped container, through the remove action")
            return
        if self.verb not in ALLOWED_VERBS:
            raise ForbiddenCommand(f"{self.verb!r} is not an allowed container command")

    @property
    def binary(self) -> str:
        return self.runtime or self.host.runtime or "docker"

    def render(self) -> str:
        """The remote shell command, fully quoted."""
        prefix = "sudo -n " if self.host.sudo else ""
        parts = [self.binary, self.verb, *self.args]
        return prefix + " ".join(shlex.quote(p) for p in parts)

    def preview(self, ssh_config: str = "~/.config/sshfleet/ssh_config") -> str:
        """What the operator would type to do the same thing by hand."""
        return f"ssh -F {ssh_config} {self.host.alias} -- {self.render()}"


def detect_command(host: Host) -> str:
    """Shell snippet that resolves the runtime when the host has not been probed yet."""
    if host.runtime in {"docker", "podman"}:
        return host.runtime
    return "$(command -v podman 2>/dev/null || command -v docker 2>/dev/null)"


def _container(name: str) -> str:
    """A name about to go into a command, refused unless a runtime could have produced it.

    Every argument is quoted when the command is rendered; this is the second lock, so that what
    reaches a host is a container name and nothing that only looks like one once quoted.
    """
    try:
        return check_container_name(name)
    except ValueError as exc:
        raise ForbiddenCommand(str(exc)) from None


def ps_command(host: Host, name: str | None = None) -> RemoteCommand:
    args = ["-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"]
    if name:
        args += ["--filter", f"name=^{_container(name)}$"]
    return RemoteCommand(host, "ps", tuple(args))


def inspect_command(host: Host, name: str) -> RemoteCommand:
    template = (
        "{{.Name}}\t{{.Config.Image}}\t{{.Image}}\t{{.Created}}\t{{.State.StartedAt}}"
        "\t{{.State.Status}}\t{{.State.ExitCode}}\t{{.RestartCount}}\t{{.HostConfig.NetworkMode}}\t{{.LogPath}}"
    )
    return RemoteCommand(host, "inspect", ("--format", template, _container(name)))


def restart_command(host: Host, name: str, timeout: int = 10) -> RemoteCommand:
    """``docker restart -t <timeout> <name>`` — in place, no stop/rm pair."""
    return RemoteCommand(host, "restart", ("-t", str(timeout), _container(name)))


def logs_command(host: Host, name: str, tail: int = 80) -> RemoteCommand:
    return RemoteCommand(host, "logs", ("--tail", str(tail), _container(name)))


def remove_command(host: Host, name: str) -> RemoteCommand:
    """``<runtime> rm <name>`` for a container that :func:`actions.remove` has just seen stopped.

    Never ``-f``: a container that started running between the check and this command makes
    the runtime refuse, which is exactly what we want.
    """
    return RemoteCommand(host, "rm", (_container(name),), guarded=True)
