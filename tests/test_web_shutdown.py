"""Stopping the web server while a page is open.

The page holds /api/events open for as long as it is open, and the terminal holds its socket.
uvicorn waits for open responses before it runs the app's own shutdown, so the server tests run
the app under a real uvicorn: that order is what is being checked, and the test transport does
not have it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass

import httpx
import pytest
from websockets.asyncio.client import connect

from sshfleet.web import create_app
from sshfleet.web.server import WS_IMPLEMENTATION, make_server
from sshfleet.web.state import WebState

#: Long enough that waiting it out cannot pass for stopping at once.
GRACE_S = 5


@dataclass
class Live:
    server: object
    thread: threading.Thread
    state: WebState
    port: int

    @property
    def headers(self) -> dict[str, str]:
        return {"x-sshfleet-token": self.state.token}

    async def stop(self, *, twice: bool = False) -> float:
        """Ctrl+C, or Ctrl+C twice in quick succession; returns how long the server took."""
        started = time.monotonic()
        self.server.should_exit = True
        if twice:
            self.server.force_exit = True
        # Joined off the loop, so the clients here can still answer a closing handshake.
        await asyncio.to_thread(self.thread.join, GRACE_S + 5)
        return time.monotonic() - started


@pytest.fixture
async def live(context, fake_ssh, monkeypatch, unused_tcp_port, caplog):
    monkeypatch.setattr("sshfleet.web.api.terminal.ssh_command", lambda config, alias, remote=None: ["cat"])
    caplog.set_level(logging.INFO)
    app = create_app(context)
    # log_config=None leaves uvicorn's records to reach the root logger, where caplog reads them.
    server = make_server(
        app, host="127.0.0.1", port=unused_tcp_port, log_level="info", log_config=None,
        ws=WS_IMPLEMENTATION, timeout_graceful_shutdown=GRACE_S,
    )
    thread = threading.Thread(target=server.run, daemon=True, name="test-web")
    thread.start()
    for _ in range(400):
        if server.started:
            break
        await asyncio.sleep(0.025)
    assert server.started, "uvicorn did not start"
    try:
        yield Live(server, thread, app.state.web, unused_tcp_port)
    finally:
        server.should_exit = True
        thread.join(timeout=GRACE_S + 5)


def _errors(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


@contextlib.asynccontextmanager
async def _open_page(live: Live):
    """What an open tab holds: the events stream, greeting already read."""
    async with (
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{live.port}", headers=live.headers) as client,
        client.stream("GET", "/api/events") as events,
    ):
        lines = events.aiter_lines()
        assert json.loads(await anext(lines))["type"] == "hello"
        yield lines


async def test_an_open_page_does_not_hold_up_stopping(live, caplog):
    """Ctrl+C with the events stream and a terminal open: the server stops at once, quietly."""
    async with _open_page(live) as lines:
        url = f"ws://127.0.0.1:{live.port}/ws/terminal/tst-order?token={live.state.token}&cols=80&rows=24"
        async with connect(url) as terminal:
            await terminal.send(b"ping\n")
            async with asyncio.timeout(15):
                while "ping" not in (await terminal.recv()).decode(errors="replace"):
                    pass
            elapsed = await live.stop()
        # The stream was ended, not cut: a cut one raises here for the missing last chunk.
        async for _ in lines:
            pass
    assert elapsed < 2, f"stopping took {elapsed:.1f}s; something waited out the grace period"
    assert _errors(caplog) == []


async def test_a_second_ctrl_c_stops_quietly_too(live, caplog, monkeypatch):
    """uvicorn skips the app's shutdown on a second Ctrl+C. The lifespan was then cancelled at
    exit, which still ran the cleanup but printed the cancellation as a failed shutdown."""
    stopped: list[bool] = []
    original_stop = WebState.stop

    async def stop(self) -> None:
        stopped.append(True)
        await original_stop(self)

    monkeypatch.setattr(WebState, "stop", stop)
    async with _open_page(live):
        elapsed = await live.stop(twice=True)
    assert stopped == [True]
    assert elapsed < 2
    assert _errors(caplog) == []


async def test_a_stream_ends_between_chunks_once_shutdown_begins(web):
    _, state = web
    cleaned_up: list[str] = []

    async def waiting():
        try:
            yield b"first\n"
            await asyncio.Event().wait()  # a follow with nothing new to say
            yield b"never\n"
        finally:
            cleaned_up.append("waiting")

    async def ready():
        try:
            for n in range(1000):
                yield f"{n}\n".encode()
        finally:
            cleaned_up.append("ready")

    idle = state.until_shutdown(waiting())
    busy = state.until_shutdown(ready())
    assert await anext(idle) == b"first\n"
    assert await anext(busy) == b"0\n"
    pending = asyncio.ensure_future(anext(idle, None))
    await asyncio.sleep(0)  # let it reach the wait

    state.begin_shutdown()
    assert await asyncio.wait_for(pending, timeout=1) is None
    assert await anext(busy, None) is None  # it had more ready, but it was between chunks
    assert sorted(cleaned_up) == ["ready", "waiting"]


async def test_a_source_that_times_out_on_its_own_still_fails(web):
    """Only the shutdown's own expiry ends a stream quietly; a real timeout is still an error."""
    _, state = web

    async def slow_host():
        raise TimeoutError("ssh did not answer")
        yield b""

    with pytest.raises(TimeoutError, match="ssh did not answer"):
        await anext(state.until_shutdown(slow_host()))
