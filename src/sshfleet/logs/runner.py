"""Read one source's output, line by line, with the window and the caps enforced locally.

Output is read in fixed-size chunks and split here rather than with ``readline``, because a
single pino object dump can exceed any reasonable line limit and ``readline`` raises instead of
truncating. The byte cap is applied on this side too: the remote ``head -c`` can be buffered,
but killing the ssh process cannot be.

The time window is decided here as well. The remote filters compare timestamps as text and
cannot read the offset a timestamp carries, and several formats have no remote filter at all,
so a line that is outside the window still reaches this side. By the time a line gets here its
format has been parsed and its offset applied, which is the first point at which "is this in
the window?" can be answered correctly — so that is where it is answered.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import AsyncIterator

from ..inventory import Host
from ..ssh import SshRunner
from ..ssh import proc as proc_module
from . import commands, parsers
from .grouper import EntryGrouper
from .model import (
    LogLine,
    LogSource,
    ResolvedWindow,
    SearchRequest,
    SourceDone,
    SourceError,
    SourceProgress,
)

CHUNK = 64 * 1024
MAX_LINE = 4 * 1024 * 1024
PROGRESS_EVERY = 0.2  # seconds
GREP_PREFIX = re.compile(rb"^(\d+)([:-])")

ERROR_KINDS = {
    "no_runtime": "no docker or podman on this host",
    "missing_container": "container not found",
    "missing_file": "log file not found",
    "too_large": "file set is larger than the cap",
    "no_files": "no log files in the window",
    "denied": "path is on the deny list",
}

#: A docker daemon error arrives on stdout with no timestamp prefix.
DAEMON_ERROR = re.compile(
    r"^(Error response from daemon|Error grabbing logs|Cannot connect to the Docker daemon|error during connect)"
)


class SourceRunner:
    """Runs one pipeline and turns its bytes into :class:`LogLine` events."""

    def __init__(
        self,
        runner: SshRunner,
        host: Host,
        source: LogSource,
        request: SearchRequest,
        window: ResolvedWindow,
    ) -> None:
        self.runner = runner
        self.host = host
        self.source = source
        self.request = request
        self.window = window
        self.pipeline = commands.build(host, source, request, window)
        self._has_grep = "grep" in self.pipeline.stages
        # Whole seconds, matching what every remote stage compares against: the producer decides
        # inclusion at second granularity, and a tighter local bound would drop lines it kept.
        # --raw-dump reproduces the manual tail+grep, which has no window, so it opts out.
        self._window_ns: tuple[int, int] | None = None
        if not request.raw_dump:
            self._window_ns = (
                int(window.since.timestamp()) * 1_000_000_000,
                (int(window.until.timestamp()) + 1) * 1_000_000_000 - 1,
            )
        self.out_of_window = 0
        self.grouper = EntryGrouper(source.log_format)
        self.lines = 0
        self.matches = 0
        self.bytes = 0
        self.truncated = False
        self.truncated_reason = ""
        self.oldest_ts: int | None = None
        self.newest_ts: int | None = None
        self._markers: dict[str, str] = {}
        self._exit_codes: list[int] = []
        self._stderr = b""

    # ----------------------------------------------------------------- stderr

    def _consume_stderr(self, text: str) -> None:
        for line in text.splitlines():
            if line.startswith(commands.STATUS_MARKER):
                self._exit_codes = [int(c) for c in line.split()[1:] if c.lstrip("-").isdigit()]
            elif line.startswith(commands.ERROR_MARKER):
                parts = line.split(None, 2)
                self._markers["error"] = parts[1] if len(parts) > 1 else "unknown"
                self._markers["error_detail"] = parts[2] if len(parts) > 2 else ""
            elif line.startswith(commands.FIRST_MARKER):
                self._markers["first"] = line.split(None, 1)[1] if " " in line else ""
            elif line.startswith(commands.COUNT_MARKER):
                self._markers["count"] = line.split(None, 1)[1].strip() if " " in line else "0"
            elif line.startswith(commands.FILES_MARKER):
                self._markers["files"] = line.split(None, 1)[1] if " " in line else ""

    # ------------------------------------------------------------------ lines

    def _emit(self, raw: bytes, seq: int) -> LogLine | None:
        text = raw.decode("utf-8", errors="replace")
        truncated = False
        if len(raw) > MAX_LINE:
            text = text[:MAX_LINE]
            truncated = True

        if text.strip() == "--":
            self.grouper.boundary()
            return None

        is_match = True
        if self._has_grep:
            # grep -n marks matches with "N:" and context lines with "N-". Only look for that
            # prefix when grep actually ran: a bare docker timestamp starts "2026-", which the
            # same pattern would happily eat.
            match = GREP_PREFIX.match(raw)
            if match:
                is_match = match.group(2) == b":"
                text = text[match.end() :]

        text = parsers.sanitize(text)
        if not text:
            return None

        ts, body = parsers.split_docker_prefix(text)
        line = self.grouper.feed(body if ts is not None else text, ts)

        # A continuation inherits its header's timestamp, so an entry is kept or dropped whole.
        # A line with no timestamp anywhere above it is kept: the window cannot rule it out.
        if self._window_ns is not None and line.ts_ns is not None:
            since_ns, until_ns = self._window_ns
            if not since_ns <= line.ts_ns <= until_ns:
                self.out_of_window += 1
                return None

        line.source_id = self.source.id
        line.label = self.source.label
        line.seq = seq
        line.is_match = is_match
        line.truncated = truncated

        if line.ts_ns is not None:
            self.oldest_ts = line.ts_ns if self.oldest_ts is None else min(self.oldest_ts, line.ts_ns)
            self.newest_ts = line.ts_ns if self.newest_ts is None else max(self.newest_ts, line.ts_ns)
        if is_match:
            self.matches += 1
        return line

    # ------------------------------------------------------------------- run

    async def run(self) -> AsyncIterator[LogLine | SourceProgress | SourceDone | SourceError]:
        started = time.monotonic()
        remote = "LC_ALL=C bash -s"
        proc, slot = await self.runner.spawn(self.host, remote, limit=MAX_LINE + CHUNK)
        stderr_task: asyncio.Task | None = None
        try:
            assert proc.stdout is not None and proc.stderr is not None
            await proc_module.feed_stdin(proc, self.pipeline.script.encode())

            async def read_stderr() -> None:
                assert proc.stderr is not None  # spawned with stderr=PIPE
                chunks = []
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                self._stderr = b"".join(chunks)
                self._consume_stderr(self._stderr.decode(errors="replace"))

            stderr_task = asyncio.create_task(read_stderr())

            buffer = b""
            seq = 0
            last_progress = started
            while True:
                chunk = await proc.stdout.read(CHUNK)
                if not chunk:
                    break
                self.bytes += len(chunk)
                buffer += chunk

                while True:
                    index = buffer.find(b"\n")
                    if index == -1:
                        if len(buffer) > MAX_LINE:
                            raw, buffer = buffer[:MAX_LINE], buffer[MAX_LINE:]
                            self.lines += 1
                            line = self._emit(raw, seq)
                            seq += 1
                            if line:
                                yield line
                        break
                    raw, buffer = buffer[:index], buffer[index + 1 :]
                    self.lines += 1
                    line = self._emit(raw, seq)
                    seq += 1
                    if line:
                        yield line

                now = time.monotonic()
                if now - last_progress >= PROGRESS_EVERY:
                    last_progress = now
                    yield SourceProgress(self.source.id, self.lines, self.bytes, self.matches, now - started)

                if self.request.max_bytes and self.bytes >= self.request.max_bytes:
                    self.truncated = True
                    self.truncated_reason = f"byte cap reached ({self.request.max_bytes} bytes)"
                    break

            if buffer.strip():
                self.lines += 1
                line = self._emit(buffer, seq)
                if line:
                    yield line

            if stderr_task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(stderr_task, timeout=5)
            await proc.wait()
        except asyncio.CancelledError:
            raise
        finally:
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            await proc_module.terminate(proc)
            await slot.__aexit__(None, None, None)

        elapsed = time.monotonic() - started
        error = self._error(elapsed)
        if error is not None:
            yield error
            return

        yield SourceDone(
            source_id=self.source.id,
            label=self.source.label,
            lines=self.lines,
            bytes=self.bytes,
            matches=self.matches,
            elapsed=elapsed,
            command=self.pipeline.description,
            truncated=self.truncated,
            truncated_reason=self.truncated_reason,
            coverage_ok=self._coverage_ok(),
            oldest_ts_ns=self.oldest_ts,
            newest_ts_ns=self.newest_ts,
            exit_codes=self._exit_codes,
        )

    # ---------------------------------------------------------------- verdict

    def _coverage_ok(self) -> bool:
        """Did the producer look at the whole window, or did ``--tail`` cut it short?

        Docker applies ``--tail`` before ``--since``, so the first line returned is always at or
        after the window start and its timestamp proves nothing on its own. What matters is
        whether the tail cap was reached: if it was, and the oldest line is newer than the
        window start, older lines existed that were never examined.
        """
        count_text = self._markers.get("count")
        if count_text is None:
            return True
        try:
            count = int(count_text)
        except ValueError:
            return True
        if count < self.request.tail_lines:
            return True  # the producer ran out of log before it ran out of tail

        first = self._markers.get("first")
        if not first:
            return True
        ts = parsers.parse_iso(first.strip())
        if ts is None:
            return True
        return ts <= int(self.window.since.timestamp() * 1_000_000_000)

    def _error(self, elapsed: float) -> SourceError | None:
        marker = self._markers.get("error")
        if marker:
            detail = self._markers.get("error_detail", "")
            return SourceError(
                self.source.id, self.source.label, marker,
                f"{ERROR_KINDS.get(marker, marker)}{': ' + detail if detail else ''}",
                self.pipeline.description, self._stderr.decode(errors="replace")[-400:],
            )

        stderr_text = self._stderr.decode(errors="replace")
        if "sudo:" in stderr_text and "password" in stderr_text:
            return SourceError(self.source.id, self.source.label, "permission", "sudo -n failed on this host",
                               self.pipeline.description, stderr_text[-400:])

        for index, code in enumerate(self._exit_codes):
            stage = self.pipeline.stage_of(index)
            if code == 0:
                continue
            if stage == "grep" and code == 1:
                continue  # grep found nothing; that is a result, not a failure
            if code in {141, 143}:
                continue  # SIGPIPE from an early `exit` or the byte cap
            if code == 124:
                return SourceError(self.source.id, self.source.label, "timeout",
                                   f"the {stage} stage hit the {self.request.timeout_s}s remote timeout",
                                   self.pipeline.description, stderr_text[-400:])
            if stage == "grep" and code == 2:
                return SourceError(self.source.id, self.source.label, "bad_regex",
                                   f"grep rejected the pattern: {stderr_text.strip()[:200]}",
                                   self.pipeline.description, stderr_text[-400:])
            if stage in {"logs", "exec", "journalctl", "read"}:
                return SourceError(self.source.id, self.source.label, "remote_tool",
                                   f"{stage} exited {code}: {stderr_text.strip()[:200]}",
                                   self.pipeline.description, stderr_text[-400:])
        if self.lines == 0 and DAEMON_ERROR.search(stderr_text):
            return SourceError(self.source.id, self.source.label, "remote_tool", stderr_text.strip()[:200],
                               self.pipeline.description, stderr_text[-400:])
        return None
