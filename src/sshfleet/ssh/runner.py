"""Run remote commands as asyncio subprocesses.

Every remote call in sshfleet goes through here, which is where the performance and safety
rules live: bounded concurrency per bastion, a hard timeout on everything, kill-the-process-group
on cancel, one retry for transient connection failures, and classification of whatever failed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ConcurrencyCfg
from ..inventory import Host, Inventory
from . import argv as A
from . import proc as proc_module
from .errors import RETRYABLE, ErrorKind, SshError, classify, first_line
from .mux import MuxManager

DEFAULT_TIMEOUT = 20.0
RETRY_DELAY = 0.7
STREAM_LIMIT = 4 << 20  # 4 MiB: pino object dumps can produce very long single lines


@dataclass(slots=True)
class RunResult:
    alias: str
    argv: list[str]
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    elapsed: float = 0.0
    kind: ErrorKind | None = None
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.kind is None

    @property
    def out(self) -> str:
        return self.stdout.decode(errors="replace")

    @property
    def err(self) -> str:
        return self.stderr.decode(errors="replace")

    def error(self) -> SshError | None:
        if self.kind is None:
            return None
        return SshError(
            kind=self.kind,
            alias=self.alias,
            message=first_line(self.err) or f"exited {self.returncode}",
            stderr=self.err,
            returncode=self.returncode,
            command=" ".join(self.argv),
        )

    def raise_for_status(self) -> RunResult:
        error = self.error()
        if error is not None:
            raise error
        return self


@dataclass(slots=True)
class _Limits:
    global_sem: asyncio.Semaphore
    per_bastion: dict[str, asyncio.Semaphore] = field(default_factory=dict)
    per_bastion_size: int = 8

    def bastion(self, name: str) -> asyncio.Semaphore:
        sem = self.per_bastion.get(name)
        if sem is None:
            sem = asyncio.Semaphore(self.per_bastion_size)
            self.per_bastion[name] = sem
        return sem


class SshRunner:
    """Runs commands on hosts through the multiplexed ssh config."""

    def __init__(
        self,
        mux: MuxManager,
        inventory: Inventory,
        limits: ConcurrencyCfg | None = None,
    ) -> None:
        self.mux = mux
        self.inventory = inventory
        cfg = limits or ConcurrencyCfg()
        self._limits = _Limits(
            global_sem=asyncio.Semaphore(cfg.global_),
            per_bastion_size=cfg.per_bastion,
        )
        self._heavy = _Limits(
            global_sem=asyncio.Semaphore(cfg.heavy_logs_global),
            per_bastion_size=cfg.heavy_logs_per_bastion,
        )

    @property
    def ssh_config(self) -> Path:
        return self.mux.ssh_config

    def _host(self, host: Host | str) -> Host:
        return self.inventory.host(host) if isinstance(host, str) else host

    def _paused(self, host: Host) -> RunResult | None:
        """The answer for a paused host, given without starting ssh at all."""
        reason = self.inventory.paused_reason(host.alias)
        if reason is None:
            return None
        return RunResult(host.alias, [], 255, b"", reason.encode(), 0.0, ErrorKind.PAUSED)

    @contextlib.asynccontextmanager
    async def _slot(self, host: Host, *, heavy: bool) -> AsyncIterator[None]:
        limits = self._heavy if heavy else self._limits
        async with limits.global_sem, limits.bastion(host.bastion):
            yield

    # ------------------------------------------------------------------ exec

    async def _once(self, alias: str, args: list[str], timeout: float, stdin_data: bytes | None) -> RunResult:
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await proc_module.run(args, stdin_data=stdin_data, timeout=timeout, limit=STREAM_LIMIT)
        elapsed = loop.time() - started
        if result.timed_out:
            alive = alias in self.mux.alive
            return RunResult(
                alias, args, 255, result.stdout, result.stderr or b"timed out", elapsed,
                classify(255, "", timed_out=True, mux_alive=alive),
            )
        return RunResult(
            alias, args, result.returncode, result.stdout, result.stderr, elapsed,
            classify(result.returncode, result.stderr.decode(errors="replace")),
        )

    async def run(
        self,
        host: Host | str,
        remote: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        stdin_data: bytes | None = None,
        retries: int = 1,
        heavy: bool = False,
        opts: tuple[str, ...] = (),
    ) -> RunResult:
        """Run one remote command and collect its output."""
        target = self._host(host)
        if (refused := self._paused(target)) is not None:
            return refused
        args = A.ssh_argv(self.ssh_config, target.alias, remote, opts=opts)
        async with self._slot(target, heavy=heavy):
            return await self._with_retries(target, args, timeout, stdin_data, retries)

    async def run_script(
        self,
        host: Host | str,
        script: str,
        args: list[str] | None = None,
        *,
        timeout: float = 25.0,
        retries: int = 1,
        heavy: bool = False,
    ) -> RunResult:
        """Feed a script to ``bash -s`` on stdin — no length limit, no double quoting."""
        target = self._host(host)
        if (refused := self._paused(target)) is not None:
            return refused
        argv = A.bash_script_argv(self.ssh_config, target.alias, args)
        async with self._slot(target, heavy=heavy):
            return await self._with_retries(target, argv, timeout, script.encode(), retries)

    async def _with_retries(
        self,
        host: Host,
        args: list[str],
        timeout: float,
        stdin_data: bytes | None,
        retries: int,
    ) -> RunResult:
        attempt = 0
        while True:
            attempt += 1
            result = await self._once(host.alias, args, timeout, stdin_data)
            result.attempts = attempt
            if result.ok or attempt > retries or result.kind not in RETRYABLE:
                if result.ok:
                    self.mux.alive.add(host.alias)
                return result
            if result.kind in {ErrorKind.MUX_STALE, ErrorKind.MUX_BROKEN}:
                await self.mux.reset_socket(host.alias)
            await asyncio.sleep(RETRY_DELAY)

    # --------------------------------------------------------------- streams

    async def spawn(
        self,
        host: Host | str,
        remote: str,
        *,
        limit: int = STREAM_LIMIT,
        heavy: bool = True,
        stdin_pipe: bool = True,
    ) -> tuple[asyncio.subprocess.Process, contextlib.AbstractAsyncContextManager[None]]:
        """Start a long-running remote command and hand back the live process.

        stdin is a pipe by default so the caller can send a script to ``bash -s``; the caller
        reads ``proc.stdout`` itself (log search reads fixed-size chunks). The second item is
        the concurrency slot: hold it until the process is done.
        """
        target = self._host(host)
        reason = self.inventory.paused_reason(target.alias)
        if reason is not None:
            raise SshError(ErrorKind.PAUSED, target.alias, reason)
        slot = self._slot(target, heavy=heavy)
        await slot.__aenter__()
        try:
            args = A.ssh_argv(self.ssh_config, target.alias, remote)
            proc = await proc_module.spawn(args, stdin_pipe=stdin_pipe, limit=limit)
        except BaseException:
            await slot.__aexit__(None, None, None)
            raise
        return proc, slot

    async def stream_lines(
        self,
        host: Host | str,
        remote: str,
        *,
        heavy: bool = True,
    ) -> AsyncIterator[bytes]:
        """Iterate a remote command's stdout line by line (convenience over :meth:`spawn`)."""
        proc, slot = await self.spawn(host, remote, heavy=heavy)
        try:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.readline()
                if not chunk:
                    break
                yield chunk
            await proc.wait()
        finally:
            await proc_module.terminate(proc)
            await slot.__aexit__(None, None, None)

    # ----------------------------------------------------------------- fanout

    async def fan_out(
        self,
        hosts: Sequence[Host],
        remote: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> AsyncIterator[RunResult]:
        """Run the same command on many hosts, yielding results as they finish."""
        tasks = [asyncio.create_task(self.run(host, remote, timeout=timeout)) for host in hosts]
        try:
            for completed in asyncio.as_completed(tasks):
                yield await completed
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
