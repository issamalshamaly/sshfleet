"""Who may have the page, and so the token: a browser let in with a launch link.

The page used to be handed to any request for ``/``, token and all, so any user or program on the
machine could read the token out of it and drive the API, the terminal included.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request

import pytest
import uvicorn
from httpx import AsyncClient

from sshfleet.paths import Paths
from sshfleet.web import create_app, server
from sshfleet.web.session import LaunchCodes, ensure_keys, load_keys


def _bare(client: AsyncClient) -> AsyncClient:
    """A client on the same app with no token and no cookie: a browser nobody let in."""
    return AsyncClient(transport=client._transport, base_url="http://127.0.0.1")


async def test_the_page_is_not_handed_to_whoever_asks(web):
    client, state = web
    async with _bare(client) as bare:
        for path in ("/", "/fleet", "/logs?q=1"):
            response = await bare.get(path)
            assert response.status_code == 401, path
            assert state.token not in response.text
            assert "sshfleet web" in response.text, "the page says how to get in"


async def test_a_launch_link_lets_the_browser_in_and_it_stays_in(web):
    client, state = web
    async with _bare(client) as bare:
        response = await bare.get("/", params={"launch": state.launch.issue()})
        assert response.status_code == 303
        assert response.headers["location"] == "/", "the link does not stay in the address"
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=" in cookie
        assert state.token.lower() not in cookie

        page = await bare.get("/")
        assert page.status_code == 200 and state.token in page.text
        # Every page of the app, not only the first.
        assert state.token in (await bare.get("/fleet")).text


async def test_a_launch_link_works_once(web):
    client, state = web
    code = state.launch.issue()
    async with _bare(client) as first, _bare(client) as second:
        assert (await first.get("/", params={"launch": code})).status_code == 303
        spent = await second.get("/", params={"launch": code})
        assert spent.status_code == 401 and "used already" in spent.text
        assert state.token not in spent.text
        # A browser that is in already is not shut out by an old link from its history.
        again = await first.get("/", params={"launch": code})
        assert again.status_code == 303 and again.headers["location"] == "/"


def test_a_launch_link_expires():
    now = [1000.0]
    codes = LaunchCodes(ttl_s=600, clock=lambda: now[0])
    late, prompt = codes.issue(), codes.issue()
    assert codes.redeem(prompt)
    now[0] += 601
    assert not codes.redeem(late)
    assert not codes.redeem("") and not codes.redeem(None) and not codes.redeem("made-up")


async def test_a_cookie_that_is_not_the_one_is_refused(web):
    client, state = web
    async with _bare(client) as bare:
        bare.cookies.set("sshfleet-80", state.token)  # the token is not the cookie
        assert (await bare.get("/")).status_code == 401
        bare.cookies.set("sshfleet-80", state.keys.cookie[:-1])
        assert (await bare.get("/")).status_code == 401


async def test_the_launch_link_leaves_the_rest_of_the_address(web):
    client, state = web
    async with _bare(client) as bare:
        response = await bare.get("/logs", params={"launch": state.launch.issue(), "view": "x"})
        assert response.headers["location"] == "/logs?view=x"


async def test_a_new_link_needs_the_token_and_works(web):
    client, _state = web
    async with _bare(client) as bare:
        assert (await bare.post("/api/session/launch")).status_code == 401
        path = (await client.post("/api/session/launch")).json()["path"]
        assert (await bare.get(path)).status_code == 303


async def test_letting_a_browser_in_is_in_the_audit_trail(web, context):
    client, state = web
    async with _bare(client) as bare:
        await bare.get("/", params={"launch": state.launch.issue()})
    entries = [json.loads(line) for line in context.paths.audit_file.read_text().splitlines()]
    assert any(e["action"] == "web.open" and e["status"] == "ok" for e in entries)


async def test_every_response_says_what_the_page_may_do(web):
    client, _ = web
    for response in (
        await client.get("/"),
        await client.get("/api/config"),
        await client.get("/", headers={"host": "rebound.example"}),
    ):
        policy = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in policy and "script-src 'self'" in policy
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_the_keys_are_private_and_outlive_a_restart(paths: Paths):
    path = paths.web_session_file
    keys, warning = ensure_keys(path)
    assert warning is None
    assert path.stat().st_mode & 0o777 == 0o600
    assert ensure_keys(path)[0] == keys, "a bookmark and an open tab survive a restart"

    # Readable by others means already given away: replaced, and private again.
    os.chmod(path, 0o644)
    assert load_keys(path) is None
    fresh, _ = ensure_keys(path)
    assert fresh != keys and path.stat().st_mode & 0o777 == 0o600

    path.write_text("not json")
    os.chmod(path, 0o600)
    assert ensure_keys(path)[0] not in (keys, fresh)


@pytest.fixture
def running(context, unused_tcp_port):
    """The app under uvicorn on its own thread, as `sshfleet web` would leave it."""
    app = create_app(context)
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=unused_tcp_port, log_level="error"))
    thread = threading.Thread(target=uv.run, daemon=True, name="test-web-session")
    thread.start()
    for _ in range(400):
        if uv.started:
            break
        time.sleep(0.025)
    assert uv.started, "uvicorn did not start"
    try:
        yield unused_tcp_port, context.paths
    finally:
        uv.should_exit = True
        thread.join(timeout=10)


def test_sshfleet_run_again_gets_a_link_into_the_running_server(running, tmp_path):
    port, paths = running
    found = server._running(port, paths)
    assert found is not None and not found.stale
    assert found.link and found.link.startswith(f"http://127.0.0.1:{port}/?launch=")

    # The link lets a browser in; urllib follows the redirect to the page itself.
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    with opener.open(found.link, timeout=5) as response:
        assert b'name="sshfleet-token"' in response.read()

    # Another configuration's sshfleet is recognised, and not opened.
    other = Paths.detect(tmp_path / "elsewhere").ensure()
    ensure_keys(other.web_session_file)
    assert server._running(port, other) == server.Running(stale=False, link=None)


def test_a_port_held_by_something_else_is_not_mistaken_for_sshfleet(paths: Paths):
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        assert server._running(other.getsockname()[1], paths) is None
