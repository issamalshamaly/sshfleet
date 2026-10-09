"""Hand the operator a real shell on a host.

The master connection is already open, so the prompt appears in about a tenth of a second even
through the bastion. ``BatchMode=no`` is re-enabled for this one session so a password or
passphrase prompt still works.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Mode = Literal["auto", "suspend", "konsole", "tmux", "print"]


@dataclass(slots=True)
class ShellPlan:
    mode: Mode
    argv: list[str]
    command: str


def detect_mode(preferred: str = "auto", *, new_window: bool = False) -> Mode:
    """Where to put the shell: a new tmux window, a Konsole tab, or this terminal."""
    if preferred != "auto":
        return preferred  # type: ignore[return-value]
    if not new_window:
        return "suspend"
    if os.environ.get("TMUX") and shutil.which("tmux"):
        return "tmux"
    if os.environ.get("KONSOLE_DBUS_SERVICE") and shutil.which("konsole"):
        return "konsole"
    return "suspend"


def ssh_command(ssh_config: Path, alias: str, remote: str | None = None) -> list[str]:
    argv = [
        os.environ.get("SSHFLEET_SSH_BIN", "ssh"),
        "-F",
        str(ssh_config),
        "-o",
        "BatchMode=no",
    ]
    if remote:
        # With a command to run, ssh asks for no remote terminal unless told to. Everything
        # this tool sends that way is interactive — a shell inside a container, a pager, a
        # client that prompts — and would otherwise come back with no echo and no job control.
        argv.append("-t")
    argv.append(alias)
    if remote:
        argv.append(remote)
    return argv


def plan(ssh_config: Path, alias: str, *, mode: Mode = "suspend", remote: str | None = None) -> ShellPlan:
    base = ssh_command(ssh_config, alias, remote)
    command = shlex.join(base)
    if mode == "konsole":
        return ShellPlan(mode, ["konsole", "--new-tab", "-e", *base], command)
    if mode == "tmux":
        return ShellPlan(mode, ["tmux", "new-window", "-n", alias, command], command)
    return ShellPlan(mode, base, command)


def open_shell(ssh_config: Path, alias: str, *, mode: Mode = "suspend", remote: str | None = None) -> int:
    """Run the shell. Blocks for ``suspend``; returns immediately for a new tab or window."""
    prepared = plan(ssh_config, alias, mode=mode, remote=remote)

    if prepared.mode == "print":
        print(prepared.command)
        return 0

    if prepared.mode == "konsole":
        subprocess.Popen(
            prepared.argv,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return 0

    if prepared.mode == "tmux":
        if not os.environ.get("TMUX"):
            # Outside tmux, put it in a detached session the operator can attach to.
            subprocess.run(["tmux", "new-session", "-d", "-s", "sshfleet", "-n", alias, prepared.command], check=False)
            return 0
        return subprocess.run(prepared.argv, check=False).returncode

    return subprocess.run(prepared.argv, check=False).returncode
