"""Closing checks that the port really went; a tunnel that drops by itself is noticed and reopened.

The master connection is replaced by one whose forwards are real listening sockets, so what is
checked is what the operator sees: whether anything answers on 127.0.0.1:<port>.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from sshfleet.config import TunnelCfg
from sshfleet.ssh import ErrorKind, MuxResult, SshError
from sshfleet.tunnels import TunnelManager, TunnelWatcher, port_in_use


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _tunnel(port: int) -> dict[str, TunnelCfg]:
    return {"db": TunnelCfg(name="db", env="testing", via="tst-bastion", remote="10.0.0.5:5432", local=port)}


class ListeningMux:
    """Stands in for the master connection: a forward is a socket listening on its port."""

    def __init__(self) -> None:
        self.listeners: dict[int, socket.socket] = {}
        self.reachable = True
        self.cancel_works = True
        self.warms = 0

    async def warm(self, alias: str, timeout: float = 25.0) -> MuxResult:
        self.warms += 1
        if not self.reachable:
            return MuxResult(alias, False, ErrorKind.TIMEOUT_CONNECT, "no connection within 25s")
        return MuxResult(alias, True)

    async def forward(self, alias: str, spec: str) -> None:
        port = int(spec.split(":")[1])
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        self.listeners[port] = listener

    async def cancel(self, alias: str, spec: str) -> None:
        if not self.cancel_works:
            raise SshError(ErrorKind.MUX_BROKEN, alias, "Control socket connect: No such file or directory")
        listener = self.listeners.pop(int(spec.split(":")[1]), None)
        if listener is not None:
            listener.close()

    def die(self) -> None:
        """The connection went, and every forward riding on it with it."""
        for listener in self.listeners.values():
            listener.close()
        self.listeners.clear()


@pytest.fixture
def mux():
    fake = ListeningMux()
    yield fake
    fake.die()


@pytest.fixture
def manager(context, mux, tmp_path: Path) -> TunnelManager:
    context.config.tunnels = _tunnel(_free_port())
    return TunnelManager(context.config, mux, tmp_path / "tunnels.json")


@pytest.fixture
def watched(manager):
    events: list[dict] = []
    audit: list[str] = []
    watcher = TunnelWatcher(manager, events.append, lambda action, **_: audit.append(action))
    return watcher, events, audit


def test_the_port_check_never_connects_through_the_tunnel():
    """A connection to an open tunnel reaches the database behind it; checking must not make one."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        assert port_in_use(port)
        with pytest.raises(BlockingIOError):
            listener.accept()  # nobody knocked
    assert not port_in_use(port)


# --------------------------------------------------------------------- closing


async def test_closing_waits_for_the_port_to_be_let_go(manager):
    opened = await manager.up("db")
    assert opened.up and port_in_use(opened.local)
    closed = await manager.down("db")
    assert (closed.held, closed.error) == (False, "")
    assert not port_in_use(opened.local)


async def test_a_close_that_leaves_the_port_open_says_so_and_names_who(manager, mux):
    opened = await manager.up("db")
    mux.cancel_works = False  # the master is out of reach, as after its socket file was removed
    closed = await manager.down("db")
    assert closed.held
    assert f"127.0.0.1:{opened.local} is still open" in closed.error
    assert "No such file" in closed.error  # what ssh said is kept
    if Path("/proc/net/tcp").exists():
        assert f"pid {os.getpid()}" in closed.error
    # Closed is what the operator asked for, so the record goes and nothing reopens it; the
    # port is shown as busy instead of free.
    state = manager.status_of("db")
    assert (state.wanted, state.held) == (False, True)


async def test_closing_connections_on_purpose_forgets_their_tunnels(manager):
    await manager.up("db")
    assert manager.forget_via({"tst-bastion"}) == ["db"]
    assert not manager.status_of("db").wanted
    assert manager.forget_via({"tst-bastion"}) == []


# -------------------------------------------------------------------- dropping


async def test_a_tunnel_whose_connection_died_counts_as_dropped(manager, mux):
    await manager.up("db")
    mux.die()
    state = manager.status_of("db")
    assert (state.wanted, state.up, state.dropped) == (True, False, True)


async def test_the_watcher_reopens_a_dropped_tunnel_on_the_same_port(manager, mux, watched):
    watcher, events, audit = watched
    opened = await manager.up("db")
    await watcher.tick()
    assert events == []  # an open tunnel is left alone

    mux.die()
    await watcher.tick()
    assert [e["state"] for e in events] == ["dropped", "reopened"]
    assert events[0]["tunnel"]["dropped"]
    assert port_in_use(opened.local)
    assert manager.status_of("db").up
    assert audit == ["tunnel.dropped", "tunnel.reopen"]
    assert watcher.retries == {}


async def test_the_watcher_backs_off_and_keeps_the_reason(manager, mux, watched):
    watcher, events, _ = watched
    await manager.up("db")
    mux.die()
    mux.reachable = False
    await watcher.tick()
    assert [e["state"] for e in events] == ["dropped", "retrying"]
    assert mux.warms == 2  # the open, then one reopen attempt

    await watcher.tick()
    assert mux.warms == 2  # not due yet: one attempt per step of the backoff, not one per tick

    row = watcher.describe(manager.status_of("db"))
    assert row["dropped"] and row["attempts"] == 1
    assert "no connection" in row["error"] and row["retry_at"] > 0

    mux.reachable = True
    watcher.retries["db"].next_at = 0  # skip the wait
    await watcher.tick()
    assert events[-1]["state"] == "reopened"
    assert manager.status_of("db").up


async def test_closing_a_dropped_tunnel_stops_the_retries(manager, mux, watched):
    watcher, events, _ = watched
    await manager.up("db")
    mux.die()
    mux.reachable = False
    await watcher.tick()
    warms = mux.warms

    closed = await manager.down("db")
    assert not closed.held
    watcher.retries["db"].next_at = 0
    await watcher.tick()
    assert mux.warms == warms  # nothing reopened what the operator closed
    assert events[-1]["state"] == "closed"  # and the browser stops showing it as reconnecting
    assert watcher.retries == {}


async def test_a_tunnel_closed_by_hand_is_never_reopened(manager, watched):
    watcher, events, _ = watched
    await manager.up("db")
    await manager.down("db")
    await watcher.tick()
    assert events == []
    assert await manager.reopen("db") is None


# ------------------------------------------------------------------------ web


async def test_the_api_reports_a_close_that_left_the_port_open(web):
    client, state = web
    fake = ListeningMux()
    state.context.config.tunnels = _tunnel(_free_port())
    state.tunnels.mux = fake
    try:
        opened = await client.post("/api/tunnels/db/up")
        assert opened.status_code == 200 and opened.json()["wanted"]
        fake.cancel_works = False
        closed = await client.post("/api/tunnels/db/down")
        assert closed.status_code == 409
        assert "still open" in closed.json()["detail"]
        row = (await client.get("/api/tunnels")).json()[0]
        assert (row["held"], row["wanted"], row["dropped"]) == (True, False, False)
        assert state.context.audit.tail(1)[0]["status"] == "failed"
    finally:
        fake.die()


async def test_a_tunnel_set_to_open_cannot_be_edited_even_while_it_is_down(web):
    client, state = web
    fake = ListeningMux()
    state.context.config.tunnels = _tunnel(_free_port())
    state.tunnels.mux = fake
    try:
        assert (await client.post("/api/tunnels/db/up")).status_code == 200
        fake.die()  # dropped: nothing listens, but the watcher is about to reopen it
        changed = await client.patch("/api/settings/tunnels/db", json={"local": _free_port()})
        assert changed.status_code == 400
        assert "close it before" in changed.json()["detail"]
    finally:
        fake.die()
