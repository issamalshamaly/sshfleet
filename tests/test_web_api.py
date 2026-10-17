"""The web API, exercised through the ASGI app against the fake ssh."""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import AsyncClient


async def test_api_requires_the_session_token(web):
    client, state = web
    bare = AsyncClient(transport=client._transport, base_url="http://127.0.0.1")
    response = await bare.get("/api/config")
    assert response.status_code == 401
    response = await bare.get("/api/config", headers={"x-sshfleet-token": state.token})
    assert response.status_code == 200
    await bare.aclose()


async def test_index_embeds_the_token(web):
    client, state = web
    response = await client.get("/")
    # Without a built frontend the server explains what to do; with one it embeds the token.
    assert response.status_code in {200, 503}
    if response.status_code == 200:
        assert state.token in response.text


async def test_config_lists_environments(web):
    client, _ = web
    data = (await client.get("/api/config")).json()
    assert [e["name"] for e in data["environments"]] == ["testing"]
    assert data["logs"]["display_tz"]
    assert "display_tz" in data["logs"]


async def test_fleet_payload_has_every_host(web, context):
    client, state = web
    await state.refresh()
    data = (await client.get("/api/fleet")).json()
    assert len(data["hosts"]) == len(context.inventory.hosts())
    order = next(h for h in data["hosts"] if h["alias"] == "tst-order")
    assert order["snapshot"]["reachable"] is True
    assert order["snapshot"]["containers"][0]["name"] == "order-production"
    assert data["mux_total"] == len(context.inventory.hosts())
    # Each host says whether its own master is open, so a view counts only what it shows.
    assert order["connected"] is True


async def test_the_refresh_button_covers_the_project_in_view(web, monkeypatch):
    client, state = web
    scopes: list[object] = []

    async def record(env=None, alias=None):
        scopes.append(env)

    monkeypatch.setattr(state, "refresh", record)
    assert (await client.post("/api/fleet/refresh?project=lab")).status_code == 200
    assert (await client.post("/api/fleet/refresh?env=testing&project=lab")).status_code == 200
    assert (await client.post("/api/fleet/refresh?project=nope")).status_code == 404
    await asyncio.sleep(0)  # let the spawned refreshes run
    assert scopes == [["testing"], "testing"]  # the environment in view wins over its project


def test_a_scope_of_no_environments_covers_no_hosts(context):
    inventory = context.inventory
    assert inventory.hosts([]) == [] and inventory.bastions([]) == []
    assert inventory.hosts(["testing"]) == inventory.hosts("testing") == inventory.hosts()


async def test_unknown_host_is_404(web):
    client, _ = web
    assert (await client.get("/api/hosts/nope")).status_code == 404


async def test_restart_preview_shows_the_exact_command(web):
    client, _ = web
    data = (await client.get("/api/hosts/tst-order/containers/order-production/preview")).json()
    assert data["command"] == "sudo -n docker restart -t 10 order-production"
    assert data["production"] is False


async def test_production_restart_requires_typed_confirmation(web, context):
    client, _ = web
    context.config.environments[0].name = "production"
    context.inventory.environments[0].name = "production"
    for host in context.inventory.hosts():
        host.env = "production"
    response = await client.post("/api/hosts/tst-order/containers/order-production/restart", json={"confirm": "wrong"})
    assert response.status_code == 400
    assert "typed" in response.json()["detail"]


async def test_container_routes_refuse_a_name_that_is_a_command(web, fake_ssh):
    client, _ = web
    name = "x$(touch sshfleet-pwned)"  # no "/": the name is one path segment of the route
    responses = [
        await client.get(f"/api/hosts/tst-order/containers/{name}/preview"),
        await client.post(f"/api/hosts/tst-order/containers/{name}/restart", json={"confirm": name}),
        await client.delete(f"/api/hosts/tst-order/containers/{name}", params={"confirm": name}),
    ]
    assert [r.status_code for r in responses] == [400, 400, 400]
    assert all("not a container name" in r.json()["detail"] for r in responses)
    assert [c for c in fake_ssh.calls() if "pwned" in json.dumps(c)] == []


async def test_quick_actions_are_tailored_to_the_container(web):
    """The menu the browser draws is built on the server, from what it knows about the host."""
    client, state = web
    await state.refresh()
    data = (await client.get("/api/hosts/tst-order/containers/order-production/actions")).json()
    by_id = {action["id"]: action for action in data["actions"]}
    assert by_id["shell"]["command"].startswith("sudo -n docker exec -it order-production")
    assert by_id["node-toolkit"]["writes"] is True
    assert by_id["shell"]["writes"] is False
    # Nothing in the menu can stop or remove anything: that is the point of naming actions.
    assert all(word not in action["command"].split() for action in data["actions"] for word in ("stop", "rm", "kill"))


async def test_quick_actions_for_an_unknown_host_are_404(web):
    client, _ = web
    assert (await client.get("/api/hosts/nope/containers/anything/actions")).status_code == 404


async def test_destructive_verbs_are_not_reachable(web):
    client, _ = web
    # There is simply no route for stop/rm; the only container action is restart.
    for verb in ("stop", "rm", "kill", "prune"):
        response = await client.post(f"/api/hosts/tst-order/containers/order-production/{verb}")
        assert response.status_code in {404, 405}


async def test_files_ls_marks_secrets(web, fake_ssh):
    client, _ = web
    fake_ssh.write_fixture(
        "tst-order", "list.txt",
        "f\t100\t1757760000\t644\tubuntu\tubuntu\torder-log.txt\t\0"
        "f\t9000\t1757760000\t600\tubuntu\tubuntu\torder.env.production\t\0",
    )
    data = (await client.get("/api/files/ls", params={"alias": "tst-order", "path": "/home/ubuntu"})).json()
    by_name = {e["name"]: e for e in data["entries"]}
    assert by_name["order-log.txt"]["denied"] is None
    assert "secret" in by_name["order.env.production"]["denied"]


async def test_files_download_refuses_secrets_before_connecting(web, fake_ssh):
    client, _ = web
    response = await client.get("/api/files/download", params={"alias": "tst-order", "path": "/home/ubuntu/order.env.production"})
    assert response.status_code == 403
    # Nothing was attempted *for this path*. Counting all calls would race the refresh loop,
    # which is sweeping the fake fleet in the background for the whole test.
    attempted = [c for c in fake_ssh.calls() if "order.env.production" in json.dumps(c)]
    assert attempted == []


async def test_log_sources_come_from_what_the_hosts_are_running(web, state_refresh=None):
    client, state = web
    await state.refresh()
    data = (await client.get("/api/logs/sources", params={"env": "testing"})).json()
    labels = [s["label"] for s in data["main"]]
    assert "order" in labels and "auth" in labels  # the starred hosts
    assert all(s["host"] for s in data["main"])
    assert any(h["alias"] == "tst-order" and h["main"] for h in data["hosts"])


async def test_log_search_streams_ndjson(web, fake_ssh):
    client, _ = web
    fake_ssh.write_fixture(
        "tst-order", "logsearch.txt",
        "2026-09-13T11:00:01.000000000Z [2026-09-13 11:00:01.000 +0000] ERROR: boom\n",
    )
    body = {
        "env": "testing",
        "selection": {"containers": [{"host": "tst-order", "name": "order-production"}]},
        "pattern": "",
        # An absolute ISO range, the shape the browser sends for a custom window. The window is
        # enforced on this side, so a relative one would place the replayed line outside it.
        "window": "2026-09-13T10:00:00Z..2026-09-13T12:00:00Z",
    }
    async with client.stream("POST", "/api/logs/search", json=body) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) async for line in response.aiter_lines() if line.strip()]
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert "line" in types
    assert types[-1] == "summary"
    line = next(e for e in events if e["type"] == "line")
    assert line["label"] == "order"
    assert line["ts"] is not None
    assert "ERROR: boom" in line["text"]


async def test_log_search_refuses_a_container_name_that_is_a_command(web, fake_ssh):
    client, _ = web
    body = {
        "env": "testing",
        "selection": {"containers": [{"host": "tst-order", "name": "x$(touch /tmp/sshfleet-pwned)"}]},
        "pattern": "",
        "window": "1h",
    }
    for route in ("/api/logs/search", "/api/logs/follow"):
        response = await client.post(route, json=body)
        assert response.status_code == 400, route
        assert "not a container name" in response.json()["detail"]
    # Refused before any connection *for this name*; the refresh loop runs alongside.
    assert [c for c in fake_ssh.calls() if "pwned" in json.dumps(c)] == []


async def test_log_search_rejects_an_empty_selection(web):
    client, _ = web
    response = await client.post("/api/logs/search", json={"env": "testing", "selection": {}, "pattern": "x", "window": "1h"})
    assert response.status_code == 400


async def test_log_search_rejects_a_bad_window(web):
    client, state = web
    await state.refresh()  # sources come from what the hosts are running
    response = await client.post(
        "/api/logs/search",
        json={"env": "testing", "selection": {"main": True}, "pattern": "x", "window": "not-a-window"},
    )
    assert response.status_code == 400
    assert "window" in response.json()["detail"]


async def test_tunnels_list_and_unknown_tunnel(web):
    client, _ = web
    assert (await client.get("/api/tunnels")).json() == []
    assert (await client.post("/api/tunnels/nope/up")).status_code == 404


async def test_the_config_reports_a_server_older_than_the_code_on_disk(web, monkeypatch):
    """A rebuilt bundle served by a process that started before it is the confusing case: the
    page is new, the routes behind it are not. The interface is told so it can say it."""
    client, state = web
    assert (await client.get("/api/config")).json()["stale"] is False

    from sshfleet.web import freshness

    monkeypatch.setattr(freshness, "_cached", (0.0, 0.0))
    monkeypatch.setattr(freshness, "newest_source_mtime", lambda: state.started_at + 60)
    assert (await client.get("/api/config")).json()["stale"] is True


async def test_audit_is_newest_first_and_says_how_many_there_are(web, context):
    """The total is what tells the page whether "load more" still has something to load."""
    client, _ = web
    for n in range(5):
        context.audit.append(f"test.{n}", status="ok")
    body = (await client.get("/api/audit", params={"limit": 2})).json()
    assert [e["action"] for e in body["entries"]] == ["test.4", "test.3"]
    assert body["total"] == 5


async def test_event_bus_fans_out_and_drops_oldest_when_a_client_lags(web):
    """The endless /api/events stream cannot be read through the test transport (it buffers
    whole responses), so the mechanism behind it is exercised directly."""
    from sshfleet.web.state import QUEUE_SIZE, Broadcaster

    bus = Broadcaster()
    fast = bus.subscribe()
    slow = bus.subscribe()
    assert bus.clients == 2

    bus.publish({"type": "host", "n": 1})
    assert (await fast.get())["n"] == 1
    assert (await slow.get())["n"] == 1

    # A browser that stops reading must not stall everyone else: its oldest events are dropped.
    for n in range(QUEUE_SIZE + 5):
        bus.publish({"type": "progress", "n": n})
    assert slow.qsize() == QUEUE_SIZE
    assert (await slow.get())["n"] == 5  # the first five were dropped

    bus.close()
    while True:
        item = await fast.get()
        if item is None:
            break
    bus.unsubscribe(fast)
    bus.unsubscribe(slow)
    assert bus.clients == 0


async def test_refresh_waits_for_a_running_sweep_instead_of_racing_it(web):
    _, state = web
    await asyncio.gather(state.refresh(), state.refresh())
    assert state.fleet.snapshot("tst-order") is not None


async def test_spa_fallback_never_serves_files_outside_static(web):
    client, _ = web
    response = await client.get("/../../etc/passwd")
    assert response.status_code in {200, 404, 503}
    assert "root:" not in response.text


async def test_leaving_a_host_out_keeps_the_others_untouched(web):
    """Clicking a chip off sends the rest by alias; they must resolve to the same sources."""
    from sshfleet.web.api.logs import Selection, resolve_sources

    client, state = web
    await state.refresh()
    main = (await client.get("/api/logs/sources", params={"env": "testing"})).json()["main"]
    hosts = list(dict.fromkeys(s["host"] for s in main))
    assert len(hosts) > 1

    kept = hosts[1:]
    resolved = resolve_sources(state, "testing", Selection(starred_hosts=kept))
    assert [s.host_alias for s in resolved] == [s["host"] for s in main if s["host"] in kept]
    assert [s.id for s in resolved] == [s["id"] for s in main if s["host"] in kept]

    # Everything excluded searches nothing, which is why the UI disables the buttons instead.
    assert resolve_sources(state, "testing", Selection(starred_hosts=[])) == []


async def test_removing_a_container_needs_it_stopped_and_its_name_typed(web, fake_ssh):
    from sshfleet.containers import runtime as rt

    client, state = web
    await state.refresh(alias="tst-order")
    host = state.context.inventory.host("tst-order")

    def fixture(command: str) -> str:
        return "cmd_" + "".join(ch if ch.isalnum() else "_" for ch in command)[:80] + ".txt"

    # Running, per the last snapshot: refused before anything is sent to the host.
    running = await client.delete("/api/hosts/tst-order/containers/order-production", params={"confirm": "order-production"})
    assert running.status_code == 409

    # Stopped, but the name was not typed back.
    untyped = await client.delete("/api/hosts/tst-order/containers/loyalty-production", params={"confirm": ""})
    assert untyped.status_code == 400

    inspect_line = "/loyalty-production\tx\tsha256:b\t2026-08-01T08:00:00Z\t2026-08-01T08:00:05Z\texited\t137\t2\tbridge\t/x.log\n"
    fake_ssh.write_fixture("tst-order", fixture(rt.inspect_command(host, "loyalty-production").render() + " 2>&1"), inspect_line)
    fake_ssh.write_fixture("tst-order", fixture(rt.logs_command(host, "loyalty-production", 2000).render() + " 2>&1"), "goodbye\n")
    fake_ssh.write_fixture("tst-order", fixture("sudo -n docker rm loyalty-production 2>&1"), "loyalty-production\n")

    removed = await client.delete("/api/hosts/tst-order/containers/loyalty-production", params={"confirm": "loyalty-production"})
    assert removed.status_code == 200, removed.text
    body = removed.json()
    assert body["ok"] is True and body["state"] == "exited"
    assert body["saved_log"] and body["saved_log"].endswith(".log")
    from pathlib import Path

    assert Path(body["saved_log"]).read_text() == "goodbye\n"
    assert "loyalty-production" in state.context.config.hosts["tst-order"].ignored_containers
    assert "loyalty-production" in state.context.inventory.host("tst-order").ignored_containers

    entries = [json.loads(line) for line in state.context.paths.audit_file.read_text().splitlines()]
    removes = [e for e in entries if e["action"] == "container.remove"]
    assert [e["status"] for e in removes] == ["started", "ok"]
    assert removes[-1]["command"] == "sudo -n docker rm loyalty-production"


async def test_a_vanished_container_can_be_ignored(web):
    client, state = web
    response = await client.post("/api/hosts/tst-order/containers/old-thing/ignore")
    assert response.status_code == 200
    assert response.json()["ignored"] == ["old-thing"]
    assert state.context.inventory.host("tst-order").ignored_containers == ["old-thing"]


async def test_ignoring_a_job_container_saves_the_glob_not_the_one_name(web):
    """A scheduler invents a name per run, so the name on screen would silence exactly nothing."""
    client, state = web
    name = "destination-postgres-write-43138-0-nxulk"
    response = await client.post(f"/api/hosts/tst-order/containers/{name}/ignore", params={"scope": "family"})
    assert response.status_code == 200
    assert response.json()["pattern"] == "destination-postgres-write-*"
    host = state.context.inventory.host("tst-order")
    assert host.ignored_containers == ["destination-postgres-write-*"]
    assert host.ignores("destination-postgres-write-44001-2-qqzab")  # the next run, too
    assert not host.ignores("order-production")

    refused = await client.post(f"/api/hosts/tst-order/containers/{name}/ignore", params={"scope": "everything"})
    assert refused.status_code == 400


def test_desktop_falls_back_to_a_browser_window_when_pywebview_cannot_draw(monkeypatch):
    """A plain `pip install pywebview` on Linux has no GTK/Qt bindings: the window must still open."""
    import sys
    import types

    from sshfleet.web import server

    broken = types.ModuleType("webview")
    broken.create_window = lambda *a, **k: None  # type: ignore[attr-defined]

    def start():
        raise RuntimeError("You must have either QT or GTK with Python extensions installed")

    broken.start = start  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", broken)

    calls: list[list[str]] = []
    which = lambda name: f"/usr/bin/{name}" if name == "google-chrome-stable" else None  # noqa: E731
    used = server.open_desktop_window("http://127.0.0.1:1/", which=which, run=lambda argv, check: calls.append(argv))
    assert used == "app-mode:/usr/bin/google-chrome-stable"
    assert calls == [["/usr/bin/google-chrome-stable", "--app=http://127.0.0.1:1/", "--window-size=1500,950"]]

    # No Chromium-family browser at all: any browser is better than a traceback.
    calls.clear()
    which = lambda name: "/usr/bin/firefox" if name == "firefox" else None  # noqa: E731
    used = server.open_desktop_window("http://127.0.0.1:1/", which=which, run=lambda argv, check: calls.append(argv))
    assert used == "browser:/usr/bin/firefox" and calls == [["/usr/bin/firefox", "http://127.0.0.1:1/"]]

    # None of those on PATH (macOS): the platform's default browser, through the standard library.
    opened: list[str] = []
    used = server.open_desktop_window(
        "http://127.0.0.1:1/", which=lambda name: None, run=lambda argv, check: None, open_default=lambda u: opened.append(u) or True
    )
    assert used == "browser:default" and opened == ["http://127.0.0.1:1/"]

    with pytest.raises(SystemExit, match="sshfleet web"):
        server.open_desktop_window(
            "http://127.0.0.1:1/", which=lambda name: None, run=lambda argv, check: None, open_default=lambda u: False
        )


async def test_everything_the_picker_lists_can_actually_be_searched(web):
    """A picked source is looked up, not rebuilt: rebuilding it from a path loses what it is.

    Every file and journal the picker shows comes back from /api/logs/sources with an id. Sent
    back, it has to resolve to the same source — a journal has no path to rebuild from, an
    in-container file would be looked for on the host, and the format that drives the remote
    time filter and the glob that reaches rotated files would both be gone.
    """
    from sshfleet.web.api.logs import Selection, resolve_sources

    client, state = web
    await state.refresh()
    listing = (await client.get("/api/logs/sources", params={"env": "testing"})).json()

    offered = [(h["alias"], f) for h in listing["hosts"] for f in h["files"]]
    assert any(f["kind"] == "journal" for _, f in offered), "no journal in the listing to check"
    assert any(f["kind"] == "container_file" for _, f in offered), "no in-container file to check"

    for alias, entry in offered:
        got = resolve_sources(state, "testing", Selection(files=[{"host": alias, "id": entry["id"]}]))
        assert len(got) == 1, entry
        assert got[0].id == entry["id"]
        assert got[0].kind.value == entry["kind"], entry
        assert got[0].log_format.value == entry["format"], entry
        assert got[0].container == entry["container"], entry


async def test_a_path_nobody_listed_is_still_searchable(web):
    """Typing a path is how an ad-hoc file is reached; that must keep working."""
    from sshfleet.web.api.logs import Selection, resolve_sources

    _, state = web
    got = resolve_sources(state, "testing", Selection(files=[{"host": "tst-order", "path": "/var/log/ad-hoc.log"}]))
    assert [s.path for s in got] == ["/var/log/ad-hoc.log"]


async def test_a_source_nobody_offered_is_refused_not_guessed(web):
    from fastapi import HTTPException

    from sshfleet.web.api.logs import Selection, resolve_sources

    _, state = web
    with pytest.raises((KeyError, HTTPException)):
        resolve_sources(state, "testing", Selection(files=[{"host": "tst-order", "id": "nope"}]))


async def test_reloads_never_overlap(web, monkeypatch):
    """Two reloads used to interleave at the await in the middle of one, swapping in one
    inventory and reporting the other's changes."""
    _, state = web
    monkeypatch.setattr(state, "wake", lambda: None)  # no sweep left running when the loop closes
    active = peak = 0
    original = state._reload

    async def slow(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        try:
            return await original(**kwargs)
        finally:
            active -= 1

    state._reload = slow
    results = await asyncio.gather(state.reload(reason="one"), state.reload(reason="two"))
    assert peak == 1
    assert all(r["ok"] for r in results)


async def test_a_task_holding_the_config_can_reload_without_waiting_on_itself(web, monkeypatch):
    _, state = web
    monkeypatch.setattr(state, "wake", lambda: None)

    async def restore_like() -> dict:
        async with state.config_lock():
            return await state.reload(reason="after writing")

    summary = await asyncio.wait_for(restore_like(), timeout=5)
    assert summary["ok"]


async def test_an_environment_path_with_a_line_break_is_refused(web, tmp_path):
    """Each path becomes one line of the generated ssh config; a line break would add another."""
    client, _ = web
    response = await client.post(
        "/api/settings/environments",
        json={"name": "second", "ssh_config_path": f"{tmp_path}/env\nProxyCommand id", "project": "lab"},
    )
    assert response.status_code == 422
    assert "one line" in response.text
    response = await client.patch("/api/settings/environments/testing", json={"key_path": "/k\nProxyCommand id"})
    assert response.status_code == 422


def test_web_on_a_taken_port_says_who_has_it(monkeypatch):
    """It used to print uvicorn's "[Errno 98]" and then announce the address as if it had started."""
    import socket

    from sshfleet.web import server

    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        monkeypatch.setattr(server, "create_app", lambda *_a, **_k: pytest.fail("must not start a second server"))

        with pytest.raises(SystemExit, match=f"port {port} is taken by another program"):
            server.run_web(port=port, open_browser=False)

        # Our own server on it: say so and stop, rather than fail.
        monkeypatch.setattr(server, "_already_serving", lambda _port: (True, False))
        assert server.run_web(port=port, open_browser=False) is None

        monkeypatch.setattr(server, "_already_serving", lambda _port: (True, True))
        with pytest.raises(SystemExit, match="older sshfleet"):
            server.run_web(port=port, open_browser=False)
