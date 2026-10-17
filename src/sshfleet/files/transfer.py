"""Download and upload files.

rsync first (progress, resumable, cheap on re-runs), scp when the remote has no rsync (Amazon
Linux often does not), and a ``sudo cat`` stream when the file is root-only. Uploads that need
root go to a temp path and are then installed with the original mode and owner.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..inventory import Host
from ..ssh import argv as A
from ..ssh import proc as proc_module
from ..ssh.errors import ErrorKind, SshError
from ..ssh.runner import SshRunner
from .browse import resolve_file, stat_file
from .denylist import assert_allowed

#: rsync --info=progress2 writes "  1,234,567  42%  1.23MB/s    0:00:03"
PROGRESS_RE = re.compile(rb"([\d,]+)\s+(\d+)%\s+(\S+)")

ProgressCallback = Callable[["TransferProgress"], None]


@dataclass(slots=True)
class TransferProgress:
    bytes_done: int
    percent: int
    rate: str
    elapsed: float


@dataclass(slots=True)
class TransferResult:
    source: str
    destination: str
    bytes: int
    method: str  # rsync | scp | sudo-cat | sudo-install
    elapsed: float
    ok: bool
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "destination": self.destination,
            "bytes": self.bytes,
            "method": self.method,
            "elapsed": round(self.elapsed, 2),
            "ok": self.ok,
            "message": self.message,
        }


def _refuse_paused(runner: SshRunner, host: Host) -> None:
    """rsync and scp start ssh themselves, so the runner's own refusal does not cover them."""
    reason = runner.inventory.paused_reason(host.alias)
    if reason is not None:
        raise SshError(ErrorKind.PAUSED, host.alias, reason)


#: Characters a remote shell acts on. rsync runs with -s and SFTP-mode scp passes the path as
#: data, but scp speaking the legacy protocol (the default before OpenSSH 9) hands the remote
#: path to the host's shell, where `$(…)` in a file name would run.
_SHELL_SPECIAL = frozenset("$`\\\"';&|<>(){}*?[]!~")

#: Why the scp fallback was skipped, said in the result instead of trying it anyway.
_SCP_REFUSED = "scp was not tried: the path has characters a remote shell would act on"


def _scp_path_is_safe(path: str) -> bool:
    """Whether ``path`` can go to scp as a remote path without a remote shell acting on it."""
    return not any(ch in _SHELL_SPECIAL or ord(ch) < 32 or ch == "\x7f" for ch in path)


def _staging_name(name: str) -> str:
    """A name for the copy left in /tmp before ``sudo install``, made of safe characters only."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80] or "upload"


def download_dir_for(base: Path, host: Host) -> Path:
    return base / host.env / host.alias / time.strftime("%Y%m%d-%H%M%S")


async def _stream_progress(
    proc: asyncio.subprocess.Process,
    on_progress: ProgressCallback | None,
    started: float,
) -> tuple[bytes, bytes]:
    """rsync writes progress with \\r; read it in chunks rather than lines."""
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []

    async def read_err() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            err_chunks.append(chunk)

    err_task = asyncio.create_task(read_err())
    assert proc.stdout is not None
    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            out_chunks.append(chunk)
            if on_progress:
                for match in PROGRESS_RE.finditer(chunk):
                    on_progress(
                        TransferProgress(
                            bytes_done=int(match.group(1).replace(b",", b"")),
                            percent=int(match.group(2)),
                            rate=match.group(3).decode(errors="replace"),
                            elapsed=time.monotonic() - started,
                        )
                    )
    finally:
        await proc.wait()
        await err_task
    return b"".join(out_chunks), b"".join(err_chunks)


async def download(
    runner: SshRunner,
    host: Host,
    remote_path: str,
    destination: Path,
    *,
    deny_extra: list[str] | None = None,
    on_progress: ProgressCallback | None = None,
    timeout: float = 1800.0,
) -> TransferResult:
    """Fetch one remote file. Refuses anything on the deny list, before connecting."""
    assert_allowed(remote_path, deny_extra)
    _refuse_paused(runner, host)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_dir():
        destination = destination / Path(remote_path).name

    started = time.monotonic()
    # Fetch what the path really is, once it is known to be a file the deny list allows.
    remote_path = await resolve_file(runner, host, remote_path, deny_extra=deny_extra)
    source = f"{host.alias}:{remote_path}"

    # 1. rsync: progress, and cheap when the file is already partly there.
    argv = A.rsync_argv(runner.ssh_config, source, str(destination))
    proc = await proc_module.spawn(argv)
    _, err = await _stream_progress(proc, on_progress, started)
    if proc.returncode == 0:
        return TransferResult(source, str(destination), destination.stat().st_size, "rsync", time.monotonic() - started, True)

    stderr = err.decode(errors="replace")
    rsync_failed_remotely = proc.returncode in {12, 127} or "rsync: not found" in stderr or "command not found" in stderr

    # 2. scp: no progress, but present everywhere.
    if (rsync_failed_remotely or proc.returncode == 23) and not _scp_path_is_safe(remote_path):
        stderr = f"{stderr.rstrip()}\n{_SCP_REFUSED}"
    elif rsync_failed_remotely or proc.returncode == 23:
        scp = A.scp_argv(runner.ssh_config, [source], str(destination))
        result = await proc_module.run(scp, timeout=timeout)
        if result.returncode == 0 and destination.exists():
            return TransferResult(source, str(destination), destination.stat().st_size, "scp", time.monotonic() - started, True)
        stderr = result.stderr.decode(errors="replace") or stderr

    # 3. The file is root-only: stream it through sudo cat.
    if host.sudo and ("Permission denied" in stderr or "permission denied" in stderr.lower()):
        remote = f"sudo -n cat -- {shlex.quote(remote_path)}"
        args = A.ssh_argv(runner.ssh_config, host.alias, remote)
        cat = await proc_module.spawn(args)
        assert cat.stdout is not None
        written = 0
        with destination.open("wb") as handle:
            while True:
                chunk = await cat.stdout.read(65536)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                if on_progress:
                    on_progress(TransferProgress(written, 0, "", time.monotonic() - started))
        cat_err = await cat.stderr.read() if cat.stderr else b""
        await cat.wait()
        if cat.returncode == 0 and written:
            return TransferResult(source, str(destination), written, "sudo-cat", time.monotonic() - started, True)
        destination.unlink(missing_ok=True)
        stderr = cat_err.decode(errors="replace") or stderr

    return TransferResult(source, str(destination), 0, "rsync", time.monotonic() - started, False, stderr.strip()[:400] or "download failed")


async def upload(
    runner: SshRunner,
    host: Host,
    local_path: Path,
    remote_path: str,
    *,
    deny_extra: list[str] | None = None,
    on_progress: ProgressCallback | None = None,
    timeout: float = 1800.0,
) -> TransferResult:
    """Send one local file to the host. Mutating, so callers must confirm and audit it."""
    assert_allowed(remote_path, deny_extra)
    _refuse_paused(runner, host)
    if not local_path.is_file():
        raise FileNotFoundError(local_path)

    started = time.monotonic()
    destination = f"{host.alias}:{remote_path}"
    size = local_path.stat().st_size

    argv = A.rsync_argv(runner.ssh_config, str(local_path), destination)
    proc = await proc_module.spawn(argv)
    _, err = await _stream_progress(proc, on_progress, started)
    if proc.returncode == 0:
        return TransferResult(str(local_path), destination, size, "rsync", time.monotonic() - started, True)

    stderr = err.decode(errors="replace")
    if not _scp_path_is_safe(remote_path):
        message = f"{stderr.strip()[:300]}\n{_SCP_REFUSED}".strip()
        return TransferResult(str(local_path), destination, 0, "rsync", time.monotonic() - started, False, message)
    scp = A.scp_argv(runner.ssh_config, [str(local_path)], destination)
    result = await proc_module.run(scp, timeout=timeout)
    if result.returncode == 0:
        return TransferResult(str(local_path), destination, size, "scp", time.monotonic() - started, True)
    stderr = result.stderr.decode(errors="replace") or stderr

    # Root-owned target: land in /tmp, then install with the original mode and owner.
    if host.sudo and "Permission denied" in stderr:
        original = await stat_file(runner, host, remote_path)
        staging = f"/tmp/sshfleet-{int(time.time())}-{_staging_name(local_path.name)}"
        staged = await proc_module.run(
            A.scp_argv(runner.ssh_config, [str(local_path)], f"{host.alias}:{staging}"), timeout=timeout
        )
        if staged.returncode == 0:
            mode = original[1] if original else "644"
            owner = f"{original[2]}:{original[3]}" if original else "root:root"
            install = (
                f"sudo -n install -m {shlex.quote(mode)} -o {shlex.quote(owner.split(':')[0])} "
                f"-g {shlex.quote(owner.split(':')[1])} -- {shlex.quote(staging)} {shlex.quote(remote_path)} "
                f"&& rm -f -- {shlex.quote(staging)}"
            )
            moved = await runner.run(host, install, timeout=60, retries=0)
            if moved.ok:
                return TransferResult(str(local_path), destination, size, "sudo-install", time.monotonic() - started, True)
            stderr = moved.err or stderr

    return TransferResult(str(local_path), destination, 0, "scp", time.monotonic() - started, False, stderr.strip()[:400] or "upload failed")
