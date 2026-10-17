"""The browser terminal, over a real WebSocket against a real pseudo-terminal.

These run the app under uvicorn rather than through the ASGI transport, because what is being
checked is the event loop itself: a bridge that blocks on the pty stops serving everything.
``ssh`` is replaced by ``cat``, which is the interesting case — a remote that writes back as it
reads, the way a shell echoes and an editor redraws.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from sshfleet.web import create_app
from sshfleet.web.server import WS_IMPLEMENTATION

LINE = "echo hello world this is a pasted line\n"


@pytest.fixture
async def live(context, fake_ssh, monkeypatch, unused_tcp_port):
    """The app under uvicorn on its own thread, with ``cat`` standing in for ssh.

    Its own thread, and so its own event loop, because a bridge that blocks would otherwise
    block the test too and the assertions would hang instead of failing.
    """
    # A plain session becomes ``cat``; a quick action echoes the line it would have run, which
    # is what the assertion about it is interested in.
    monkeypatch.setattr(
        "sshfleet.web.api.terminal.ssh_command",
        lambda config, alias, remote=None: ["cat"] if remote is None else ["echo", remote],
    )
    app = create_app(context)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=unused_tcp_port, log_level="error", ws=WS_IMPLEMENTATION)
    )
    thread = threading.Thread(target=server.run, daemon=True, name="test-web")
    thread.start()
    for _ in range(400):
        if server.started:
            break
        await asyncio.sleep(0.025)
    assert server.started, "uvicorn did not start"
    try:
        yield unused_tcp_port, app.state.web.token
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _url(port: int, token: str, alias: str = "tst-order") -> str:
    return f"ws://127.0.0.1:{port}/ws/terminal/{alias}?token={token}&cols=120&rows=40"


async def _read_until(socket, wanted: int, needle: str, timeout: float) -> str:
    seen = ""
    async with asyncio.timeout(timeout):
        while seen.count(needle) < wanted:
            frame = await socket.recv()
            seen += frame.decode(errors="replace") if isinstance(frame, bytes) else frame
    return seen


#: Enough to outlast the pty's buffers several times over. A pty holds a few kilobytes each
#: way, and a paste that fits is delivered by any implementation; this is the size at which
#: handing it over in one call stops working — about 600 lines used to arrive and the rest
#: never did, however long you waited.
PASTE_LINES = 2000


async def test_a_paste_far_larger_than_the_pty_buffer_arrives_whole(live):
    """The remote writes back as it reads, so it can only accept the paste if we keep reading."""
    port, token = live
    paste = LINE * PASTE_LINES
    async with connect(_url(port, token), max_size=None) as socket:
        await socket.send(paste.encode())
        # cat writes every line back, so the whole paste having been accepted is observable.
        seen = await _read_until(socket, PASTE_LINES, "pasted line", timeout=60)
    assert seen.count("pasted line") >= PASTE_LINES


async def test_the_rest_of_the_server_answers_while_a_paste_is_still_going(live):
    """The bridge shares its event loop with every other request, so it must never hold it."""
    import httpx

    port, token = live
    async with connect(_url(port, token), max_size=None) as socket:
        await socket.send((LINE * PASTE_LINES).encode())
        # Wait until the paste is demonstrably under way before asking for anything else: a
        # request answered before the first byte moved would prove nothing.
        await _read_until(socket, 100, "pasted line", timeout=30)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            response = await asyncio.wait_for(
                client.get("/api/config", headers={"x-sshfleet-token": token}), timeout=10
            )
        assert response.status_code == 200
        await _read_until(socket, PASTE_LINES, "pasted line", timeout=60)


async def test_typing_still_works(live):
    """The buffering must not get in the way of the ordinary case: one keystroke at a time."""
    port, token = live
    async with connect(_url(port, token)) as socket:
        for char in "whoami\n":
            await socket.send(char.encode())
        seen = await _read_until(socket, 2, "whoami", timeout=15)
    assert seen.count("whoami") >= 2  # the tty echoes it, then cat writes it back


async def test_a_page_on_another_site_cannot_open_a_terminal(live):
    """Unlike fetch, a WebSocket is not held back by CORS: the browser names the page that opens
    it, and the server must refuse every page but its own, token or not."""
    port, token = live
    for origin in ("http://evil.example", f"http://127.0.0.1:{port + 1}", "null"):
        with pytest.raises(InvalidStatus) as refused:
            async with connect(_url(port, token), origin=origin):
                pass
        assert refused.value.response.status_code == 403, origin
    # The page this server served gets its terminal.
    async with connect(_url(port, token), origin=f"http://127.0.0.1:{port}") as socket:
        await socket.send(b"x\n")
        await _read_until(socket, 1, "x", timeout=15)


def _quick_url(port: int, token: str, action: str, container: str) -> str:
    return f"{_url(port, token)}&action={action}&container={container}"


async def test_a_quick_action_runs_the_line_the_catalogue_built(live):
    """The browser names an action; the command that reaches ssh is the server's own."""
    port, token = live
    async with connect(_quick_url(port, token, "shell", "order-production")) as socket:
        seen = await _read_until(socket, 1, "docker exec", timeout=15)
    assert "exec -it order-production" in seen


async def test_an_action_outside_the_catalogue_opens_nothing(live):
    """It is refused in the session rather than at the handshake, so the reason can be read."""
    port, token = live
    async with connect(_quick_url(port, token, "docker-rm", "order-production")) as socket:
        seen = await _read_until(socket, 1, "unknown quick action", timeout=15)
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(socket.recv(), timeout=5)
    assert "docker-rm" in seen
