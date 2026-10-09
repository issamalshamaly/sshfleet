"""Classify OpenSSH failures into actionable kinds.

``ssh`` exits 255 for its own failures and passes through the remote command's exit code
otherwise, so the stderr text is the only way to tell "bastion down" from "wrong key".
Each kind carries a short cell code for the fleet table and a command the operator can run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ErrorKind(StrEnum):
    TIMEOUT_CONNECT = "timeout_connect"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"
    BASTION_DOWN = "bastion_down"
    JUMP_TARGET_UNREACHABLE = "jump_target_unreachable"
    HOSTKEY_MISMATCH = "hostkey_mismatch"
    AUTH_FAILED = "auth_failed"
    KEY_FILE = "key_file"
    SSHD_BUSY = "sshd_busy"
    MUX_STALE = "mux_stale"
    MUX_BROKEN = "mux_broken"
    DISCONNECTED = "disconnected"
    COMMAND_TIMEOUT = "command_timeout"
    SUDO_REQUIRED = "sudo_required"
    DAEMON_DOWN = "daemon_down"
    RUNTIME_MISSING = "runtime_missing"
    CONTAINER_NOT_FOUND = "container_not_found"
    REMOTE_FAILED = "remote_failed"
    CANCELLED = "cancelled"
    #: Refused before any connection: the operator paused monitoring of this host.
    PAUSED = "paused"
    UNKNOWN = "unknown"


#: Kinds worth one automatic retry (transient connection problems).
RETRYABLE = frozenset(
    {ErrorKind.SSHD_BUSY, ErrorKind.MUX_STALE, ErrorKind.MUX_BROKEN, ErrorKind.DISCONNECTED}
)

#: Short codes shown in the fleet table.
CELL_CODE: dict[ErrorKind, str] = {
    ErrorKind.TIMEOUT_CONNECT: "TIME",
    ErrorKind.REFUSED: "DOWN",
    ErrorKind.UNREACHABLE: "NET",
    ErrorKind.BASTION_DOWN: "BAST",
    ErrorKind.JUMP_TARGET_UNREACHABLE: "JUMP",
    ErrorKind.HOSTKEY_MISMATCH: "KEY!",
    ErrorKind.AUTH_FAILED: "AUTH",
    ErrorKind.KEY_FILE: "KEYF",
    ErrorKind.SSHD_BUSY: "BUSY",
    ErrorKind.MUX_STALE: "MUX",
    ErrorKind.MUX_BROKEN: "MUX",
    ErrorKind.DISCONNECTED: "DISC",
    ErrorKind.COMMAND_TIMEOUT: "SLOW",
    ErrorKind.SUDO_REQUIRED: "SUDO",
    ErrorKind.DAEMON_DOWN: "DOCK",
    ErrorKind.RUNTIME_MISSING: "NORT",
    ErrorKind.CONTAINER_NOT_FOUND: "NOCT",
    ErrorKind.REMOTE_FAILED: "ERR",
    ErrorKind.CANCELLED: "CANC",
    ErrorKind.UNKNOWN: "ERR",
}

_PATTERNS: list[tuple[re.Pattern[str], ErrorKind]] = [
    (re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed", re.I), ErrorKind.HOSTKEY_MISMATCH),
    (re.compile(r"UNPROTECTED PRIVATE KEY FILE|Identity file .* not accessible|bad permissions", re.I), ErrorKind.KEY_FILE),
    (re.compile(r"Permission denied \(|Too many authentication failures|No supported authentication", re.I), ErrorKind.AUTH_FAILED),
    (re.compile(r"kex_exchange_identification.*Connection closed|Connection closed by remote host", re.I), ErrorKind.SSHD_BUSY),
    (re.compile(r"ControlSocket .* already exists|multiplexing", re.I), ErrorKind.MUX_STALE),
    (re.compile(r"Control socket connect\(.*\): (Connection refused|No such file)", re.I), ErrorKind.MUX_BROKEN),
    (re.compile(r"channel \d+: open failed|administratively prohibited|open failed: connect failed", re.I), ErrorKind.JUMP_TARGET_UNREACHABLE),
    (re.compile(r"Operation timed out|Connection timed out|timed out during banner exchange", re.I), ErrorKind.TIMEOUT_CONNECT),
    (re.compile(r"Connection refused", re.I), ErrorKind.REFUSED),
    (re.compile(r"No route to host|Network is unreachable|Name or service not known|Could not resolve", re.I), ErrorKind.UNREACHABLE),
    (re.compile(r"client_loop: send disconnect|Broken pipe|Connection reset by peer", re.I), ErrorKind.DISCONNECTED),
    (re.compile(r"sudo: a (password|terminal) is required|sudo: no tty present", re.I), ErrorKind.SUDO_REQUIRED),
    (re.compile(r"Cannot connect to the Docker daemon|Is the docker daemon running", re.I), ErrorKind.DAEMON_DOWN),
    (re.compile(r"(docker|podman): command not found|no such file or directory: (docker|podman)", re.I), ErrorKind.RUNTIME_MISSING),
    (re.compile(r"No such container|no such object", re.I), ErrorKind.CONTAINER_NOT_FOUND),
]

_SUGGESTIONS: dict[ErrorKind, str] = {
    ErrorKind.TIMEOUT_CONNECT: "ssh -F {cfg} -vvv {alias} true   # instance stopped or security group blocking 22?",
    ErrorKind.REFUSED: "ssh -F {cfg} -vvv {alias} true   # sshd may be down",
    ErrorKind.UNREACHABLE: "ssh -F {cfg} -vvv {alias} true",
    ErrorKind.BASTION_DOWN: "ssh -F {cfg} -vvv {bastion} true",
    ErrorKind.JUMP_TARGET_UNREACHABLE: "ssh -F {cfg} {bastion} -- nc -zv {hostname} 22",
    ErrorKind.HOSTKEY_MISMATCH: "ssh-keygen -R {hostname}   # only after you confirm the instance was replaced",
    ErrorKind.AUTH_FAILED: "ssh -F {cfg} -G {alias} | grep -E '^(user|identityfile)'",
    ErrorKind.KEY_FILE: "chmod 600 {key}",
    ErrorKind.SSHD_BUSY: "lower concurrency.per_bastion in config.yaml",
    ErrorKind.MUX_STALE: "sshfleet mux reset",
    ErrorKind.MUX_BROKEN: "sshfleet mux reset",
    ErrorKind.COMMAND_TIMEOUT: "ssh -F {cfg} {alias} uptime   # host reachable but slow",
    ErrorKind.SUDO_REQUIRED: "ssh -F {cfg} {alias} sudo -n true   # check /etc/sudoers.d",
    ErrorKind.DAEMON_DOWN: "ssh -F {cfg} {alias} sudo systemctl status docker",
    ErrorKind.RUNTIME_MISSING: "ssh -F {cfg} {alias} 'command -v docker podman'",
    ErrorKind.CONTAINER_NOT_FOUND: "ssh -F {cfg} {alias} sudo docker ps -a",
}


@dataclass(slots=True)
class SshError(Exception):
    """A failed SSH invocation, classified."""

    kind: ErrorKind
    alias: str
    message: str
    stderr: str = ""
    returncode: int | None = None
    command: str = ""

    def __str__(self) -> str:
        return f"{self.alias}: {self.kind.value}: {self.message}"

    @property
    def cell_code(self) -> str:
        return CELL_CODE.get(self.kind, "ERR")

    def suggestion(self, *, cfg: str = "~/.config/sshfleet/ssh_config", bastion: str = "", hostname: str = "", key: str = "") -> str:
        template = _SUGGESTIONS.get(self.kind, "")
        if not template:
            return ""
        return template.format(cfg=cfg, alias=self.alias, bastion=bastion or self.alias, hostname=hostname or self.alias, key=key or "<key>")


def first_line(stderr: str) -> str:
    """The most useful stderr line — skip OpenSSH's informational warnings."""
    for line in stderr.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith(("Warning: Permanently added", "Shared connection to", "Connection to ")):
            continue
        return text
    return stderr.strip().splitlines()[0] if stderr.strip() else ""


def classify(returncode: int, stderr: str, *, timed_out: bool = False, mux_alive: bool = False) -> ErrorKind | None:
    """Return the error kind, or None when the invocation succeeded.

    A non-255 exit code is the *remote command's* result, so only stderr patterns that clearly
    describe a remote-side problem (sudo, docker) are classified; anything else is
    ``REMOTE_FAILED`` and the caller decides whether that matters.
    """
    if timed_out:
        return ErrorKind.COMMAND_TIMEOUT if mux_alive else ErrorKind.TIMEOUT_CONNECT
    if returncode == 0:
        return None

    for pattern, kind in _PATTERNS:
        if pattern.search(stderr):
            return kind

    if returncode == 255:
        return ErrorKind.UNKNOWN
    return ErrorKind.REMOTE_FAILED
