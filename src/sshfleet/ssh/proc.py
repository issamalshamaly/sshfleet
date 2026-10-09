"""Subprocess execution with a deadline that is actually honoured.

``asyncio.wait_for(proc.communicate(), timeout)`` does **not** bound the wall clock: on timeout
it cancels ``communicate()`` and then waits for that coroutine to finish, which in turn waits
for the child's pipes to close — and a hung ``ssh`` keeps them open. Observed in practice: a
10 s deadline took 30 s against a host whose sshd had stopped answering.

So the order here is: wait, then **kill the process group first**, then collect the output.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_LIMIT = 4 << 20  # 4 MiB: a pino object dump can be one very long line
GRACE = 3.0  # seconds to collect output after killing


@dataclass(slots=True)
class ProcResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


async def spawn(
    argv: Sequence[str],
    *,
    stdin_data: bytes | None = None,
    stdin_pipe: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> asyncio.subprocess.Process:
    """Start a process in its own session so the whole group can be killed together.

    ``stdin_pipe`` opens stdin without having the data ready — streaming callers write the
    remote script themselves and then read stdout as it arrives.
    """
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if (stdin_data is not None or stdin_pipe) else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=limit,
    )


async def terminate(proc: asyncio.subprocess.Process) -> None:
    """Kill the process group: ssh spawns a child for the ProxyJump hop."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


async def feed_stdin(proc: asyncio.subprocess.Process, data: bytes | None) -> None:
    """Write stdin and close it, tolerating a child that has already gone.

    A child that exits before reading everything — ssh losing its connection mid-script — closes
    the pipe. Plain asyncio reports that as ``BrokenPipeError``; uvloop raises ``RuntimeError``
    ("the handler is closed") instead, which ``communicate()`` does not catch, so the output and
    exit status that explain the failure were lost with it.
    """
    if proc.stdin is None:
        return
    try:
        if data:
            proc.stdin.write(data)
            await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, RuntimeError):
        pass
    finally:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
            proc.stdin.close()


async def _communicate(proc: asyncio.subprocess.Process, data: bytes | None) -> tuple[bytes, bytes]:
    assert proc.stdout is not None and proc.stderr is not None
    out, err, _ = await asyncio.gather(proc.stdout.read(), proc.stderr.read(), feed_stdin(proc, data))
    await proc.wait()
    return out, err


async def run(
    argv: Sequence[str],
    *,
    stdin_data: bytes | None = None,
    timeout: float,
    limit: int = DEFAULT_LIMIT,
) -> ProcResult:
    """Run to completion or kill it at ``timeout``; either way return what it produced."""
    proc = await spawn(argv, stdin_data=stdin_data, limit=limit)
    task = asyncio.ensure_future(_communicate(proc, stdin_data))
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        await terminate(proc)
        raise

    if task in done:
        out, err = task.result()
        return ProcResult(proc.returncode or 0, out or b"", err or b"", False)

    # Deadline hit: kill first so the pipes close, only then collect whatever was written.
    await terminate(proc)
    out = err = b""
    with contextlib.suppress(Exception):
        out, err = await asyncio.wait_for(task, timeout=GRACE)
    task.cancel()
    return ProcResult(proc.returncode if proc.returncode is not None else 255, out or b"", err or b"", True)
