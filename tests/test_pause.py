"""Pausing a host: sshfleet stops connecting to it, and closes what it had open."""

from __future__ import annotations

from sshfleet.config import loader as config_loader
from sshfleet.ssh.errors import ErrorKind


def _calls_for(fake_ssh, aliases: set[str], since: int) -> list[dict]:
    return [c for c in fake_ssh.calls()[since:] if c.get("alias") in aliases]


async def test_a_paused_host_gets_no_connection_and_its_open_one_is_closed(web, fake_ssh, context):
    client, state = web
    await state.refresh()
    assert "tst-order" in context.mux.alive and state.fleet.snapshot("tst-order") is not None

    mark = len(fake_ssh.calls())
    response = await client.patch("/api/settings/hosts/tst-order", json={"paused": True})
    assert response.status_code == 200, response.text
    assert config_loader.load(context.paths.config_file).hosts["tst-order"].paused is True

    # The master is closed through its local control socket — the only call it gets.
    closing = _calls_for(fake_ssh, {"tst-order"}, mark)
    assert closing and all(c["control"] == "exit" for c in closing)
    assert "tst-order" not in context.mux.alive
    host = (await client.get("/api/hosts/tst-order")).json()
    assert host["paused_by"] == "tst-order" and host["connected"] is False and host["snapshot"] is None
    assert not any(a["host"] == "tst-order" for a in (await client.get("/api/fleet")).json()["alerts"])

    # From here on, nothing reaches it: not a sweep, not a command, not a shell or a file.
    mark = len(fake_ssh.calls())
    await state.refresh()
    result = await context.runner.run("tst-order", "true")
    warmed = await context.mux.warm("tst-order")
    assert result.kind is ErrorKind.PAUSED and "paused" in result.err
    assert warmed.kind is ErrorKind.PAUSED
    assert (await client.post("/api/fleet/refresh?alias=tst-order")).status_code == 409
    assert (await client.get("/api/files/ls?alias=tst-order")).status_code == 409
    assert _calls_for(fake_ssh, {"tst-order"}, mark) == []
    assert state.fleet.snapshot("tst-auth") is not None  # its neighbours are checked as before

    # Resuming brings it back, and the flag leaves the file.
    response = await client.patch("/api/settings/hosts/tst-order", json={"paused": False})
    assert response.status_code == 200, response.text
    assert config_loader.load(context.paths.config_file).hosts["tst-order"].paused is None
    await state.refresh()
    assert state.fleet.snapshot("tst-order").reachable is True


async def test_pausing_a_bastion_takes_the_hosts_behind_it_out_of_reach(web, fake_ssh, context):
    client, state = web
    response = await client.patch("/api/settings/hosts/tst-bastion", json={"paused": True})
    assert response.status_code == 200, response.text

    behind = {h.alias for h in context.inventory.behind("tst-bastion")}
    assert "tst-order" in behind
    order = (await client.get("/api/hosts/tst-order")).json()
    assert order["paused_by"] == "tst-bastion"  # reaching it would pass through the bastion

    mark = len(fake_ssh.calls())
    await state.refresh()
    assert _calls_for(fake_ssh, behind | {"tst-bastion"}, mark) == []
    assert "tst-bastion" not in state.fleet.state.bastion_ok  # not reported as a bastion down
    assert state.fleet.snapshot("tst-jenkins").reachable is True  # the other bastion's hosts are
    assert not ({h.alias for h in context.inventory.starred()} & behind)  # out of the default log search


def test_the_flag_is_written_only_while_it_is_set(context, tmp_path):
    config = context.config
    config.hosts["tst-order"].paused = True
    config.hosts["tst-auth"].paused = None
    path = tmp_path / "config.yaml"
    config_loader.save(config, path)
    text = path.read_text()
    assert text.count("paused") == 1
    assert config_loader.load(path).hosts["tst-order"].paused is True
