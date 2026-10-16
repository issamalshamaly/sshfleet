"""Who may talk to the web API at all: the Host check and the session token."""

from __future__ import annotations

from httpx import AsyncClient


def _bare(client: AsyncClient) -> AsyncClient:
    """A client on the same app with no token on it."""
    return AsyncClient(transport=client._transport, base_url="http://127.0.0.1")


async def test_a_request_naming_another_host_is_refused(web):
    """DNS rebinding: a site that points its own name at 127.0.0.1 must not get the page, which
    carries the token, nor any route behind it."""
    client, state = web
    page = await client.get("/", headers={"host": "rebound.example"})
    assert page.status_code == 400
    assert state.token not in page.text
    api = await client.get("/api/config", headers={"host": "rebound.example:8765"})
    assert api.status_code == 400
    # The names the server is really reached by still work.
    assert (await client.get("/api/config", headers={"host": "localhost:8765"})).status_code == 200


async def test_the_token_in_the_address_only_opens_downloads(web):
    client, state = web
    async with _bare(client) as bare:
        for path in ("/api/config", "/api/settings", "/api/audit", "/api/fleet"):
            response = await bare.get(path, params={"token": state.token})
            assert response.status_code == 401, path
        # Past the token check on the two download routes; what answers is the route itself.
        bundle = await bare.get("/api/logs/bundles/nothing.zip", params={"token": state.token})
        assert bundle.status_code == 404
        download = await bare.get("/api/files/download", params={"alias": "no-such-host", "path": "/x", "token": state.token})
        assert download.status_code == 404
        # And without it, refused like everything else.
        assert (await bare.get("/api/logs/bundles/nothing.zip")).status_code == 401
        assert (await bare.get("/api/files/download", params={"alias": "no-such-host", "path": "/x"})).status_code == 401


async def test_a_wrong_token_is_refused(web):
    client, state = web
    wrong = ("", "x", state.token[:-1], state.token[:-1] + ("A" if state.token[-1] != "A" else "B"), state.token + "x")
    async with _bare(client) as bare:
        for token in wrong:
            response = await bare.get("/api/config", headers={"x-sshfleet-token": token})
            assert response.status_code == 401, token
