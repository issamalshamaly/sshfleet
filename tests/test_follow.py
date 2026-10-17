"""Following logs: a source that will not come back is let go, with one message saying why.

It used to retry for ever — every two seconds against a paused host, every thirty against a
container that was gone — with an error of its own on screen each time.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from helpers import make_config

from sshfleet.inventory import build_inventory
from sshfleet.logs import commands, follow
from sshfleet.logs.model import LogFormat, LogSource, SourceError, SourceKind
from sshfleet.ssh import ErrorKind, SshError

SOURCE = LogSource(
    id="testing/order-production", kind=SourceKind.CONTAINER_STDOUT, env="testing", label="order",
    host_alias="tst-order", service="order", container="order-production", log_format=LogFormat.PINO_PRETTY,
)


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(follow, "RECONNECT_DELAY", 0.001)
    monkeypatch.setattr(follow, "MAX_BACKOFF", 0.005)
    monkeypatch.setattr(follow, "MISSING_RETRY", 0.01)
    monkeypatch.setattr(follow, "MISSING_GIVE_UP_S", 0.1)


class Runner:
    """Stands in for the SSH runner: every attempt fails the same way, or runs ``script``."""

    def __init__(self, *, error: Exception | None = None, script: str = "") -> None:
        self.error = error
        self.script = script
        self.attempts = 0

    async def spawn(self, host, remote, heavy=False):
        self.attempts += 1
        if self.error is not None:
            raise self.error
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", self.script,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        return proc, contextlib.nullcontext()


async def _errors_until_given_up(runner: Runner) -> list[SourceError]:
    session = follow.FollowSession(runner, build_inventory(make_config()), [SOURCE])  # type: ignore[arg-type]
    errors: list[SourceError] = []
    async with asyncio.timeout(10):
        async for event in session.run():
            if isinstance(event, SourceError):
                errors.append(event)
                if "stopped following it" in event.message:
                    break
    return errors


async def test_a_paused_host_is_let_go_at_once(quick):
    runner = Runner(error=SshError(ErrorKind.PAUSED, "tst-order", "paused by you"))
    errors = await _errors_until_given_up(runner)
    assert [e.kind for e in errors] == ["paused"]
    assert "paused by you" in errors[0].message
    assert runner.attempts == 1


async def test_a_host_that_keeps_failing_is_given_up_on_and_said_once(quick):
    runner = Runner(error=SshError(ErrorKind.UNREACHABLE, "tst-order", "connection timed out"))
    errors = await _errors_until_given_up(runner)
    assert runner.attempts == follow.MAX_FAILURES
    assert len(errors) == 2, "one note while it retries, one when it stops"
    assert errors[0].message.endswith("trying again")
    assert f"{follow.MAX_FAILURES} attempts in a row" in errors[1].message


async def test_a_container_that_stays_away_is_given_up_on(quick):
    runner = Runner(script=f"cat >/dev/null; echo '{commands.ERROR_MARKER} missing_container order-production' >&2; exit 90")
    errors = await _errors_until_given_up(runner)
    assert runner.attempts > 2, "it is checked again for a while before that"
    assert [e.kind for e in errors] == ["missing_container", "missing_container"]
    assert "checking again" in errors[0].message and "has not been running" in errors[1].message


async def test_a_stream_that_keeps_ending_empty_is_given_up_on(quick):
    runner = Runner(script="cat >/dev/null; exit 0")
    errors = await _errors_until_given_up(runner)
    assert runner.attempts == follow.MAX_FAILURES
    assert "without a line" in errors[-1].message


async def test_a_stream_that_works_is_followed_again_after_it_ends(quick, monkeypatch):
    """Lines reset the count: a container redeployed every few seconds is never given up on."""
    monkeypatch.setattr(follow, "MAX_FAILURES", 2)
    runner = Runner(script="cat >/dev/null; echo '2026-10-17T10:00:00.000000000Z hello'")
    session = follow.FollowSession(runner, build_inventory(make_config()), [SOURCE])  # type: ignore[arg-type]
    seen = 0
    async with asyncio.timeout(10):
        async for event in session.run():
            assert not isinstance(event, SourceError), event
            seen += 1
            if seen == 5:
                break
    assert runner.attempts >= 5
