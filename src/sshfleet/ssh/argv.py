"""Build argv for ``ssh``, ``scp`` and ``rsync``.

Three quoting regimes, and mixing them up is the classic bug:

* remote shell commands  -> quote with :func:`shlex.quote` (the remote shell parses them)
* ``rsync -s`` paths     -> literal (``--secluded-args`` stops the remote shell seeing them)
* OpenSSH 9+ ``scp``     -> literal (SFTP mode, no remote shell involved)
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

#: Read-only options every invocation gets; the persistent settings live in the generated config.
BASE_OPTS: tuple[str, ...] = ("-o", "LogLevel=ERROR")


def ssh_bin() -> str:
    return os.environ.get("SSHFLEET_SSH_BIN", "ssh")


def scp_bin() -> str:
    return os.environ.get("SSHFLEET_SCP_BIN", "scp")


def rsync_bin() -> str:
    return os.environ.get("SSHFLEET_RSYNC_BIN", "rsync")


def ssh_argv(
    ssh_config: Path,
    alias: str,
    remote: str | None = None,
    *,
    opts: tuple[str, ...] = (),
    interactive: bool = False,
) -> list[str]:
    """``ssh -F <cfg> [opts] -- <alias> [remote command]``.

    ``interactive`` re-enables prompts (``BatchMode=no``) for a shell the operator drives.
    """
    argv = [ssh_bin(), "-F", str(ssh_config), *BASE_OPTS]
    if interactive:
        argv += ["-o", "BatchMode=no"]
    argv += list(opts)
    argv += ["--", alias]
    if remote is not None:
        argv.append(remote)
    return argv


def ssh_control_argv(ssh_config: Path, alias: str, command: str, *extra: str) -> list[str]:
    """``ssh -F <cfg> -O <check|exit|forward|cancel|stop> [extra] <alias>``."""
    return [ssh_bin(), "-F", str(ssh_config), *BASE_OPTS, "-O", command, *extra, alias]


def bash_script_argv(ssh_config: Path, alias: str, args: list[str] | None = None) -> list[str]:
    """Run a script fed on stdin: ``bash -s -- <args>``.

    The script itself never appears on the command line, so there is no length limit and no
    double quoting.
    """
    remote = "LC_ALL=C bash -s"
    if args:
        remote += " -- " + " ".join(shlex.quote(a) for a in args)
    return ssh_argv(ssh_config, alias, remote)


def bash_command_argv(ssh_config: Path, alias: str, script: str) -> list[str]:
    """Run a script inline: ``bash -c '<script>'`` (used where stdin carries data instead)."""
    return ssh_argv(ssh_config, alias, "bash -c " + shlex.quote(script))


def scp_argv(ssh_config: Path, sources: list[str], destination: str, *, recursive: bool = False) -> list[str]:
    argv = [scp_bin(), "-F", str(ssh_config), "-p", "-q", "-o", "LogLevel=ERROR"]
    if recursive:
        argv.append("-r")
    argv += [*sources, destination]
    return argv


def rsync_argv(
    ssh_config: Path,
    source: str,
    destination: str,
    *,
    progress: bool = True,
    extra: tuple[str, ...] = (),
) -> list[str]:
    """rsync over our multiplexed ssh; ``-s`` keeps the remote shell out of path parsing."""
    remote_shell = f"{ssh_bin()} -F {shlex.quote(str(ssh_config))} -o LogLevel=ERROR"
    argv = [rsync_bin(), "-e", remote_shell, "-a", "-s"]
    if progress:
        argv += ["--info=progress2", "--no-inc-recursive"]
    argv += [*extra, source, destination]
    return argv


def quote_remote(*parts: str) -> str:
    """Join arguments into a remote shell command with every part quoted."""
    return " ".join(shlex.quote(p) for p in parts)
