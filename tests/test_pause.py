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
    assert host["paused_by"] == {"kind": "host", "name": "tst-order"}
    assert host["connected"] is False and host["snapshot"] is None
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
    assert order["paused_by"] == {"kind": "host", "name": "tst-bastion"}  # reaching it passes through it

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


def test_environment_and_project_flags_are_written_only_while_set(context, tmp_path):
    config = context.config
    path = tmp_path / "config.yaml"
    config_loader.save(config, path)
    assert "paused" not in path.read_text()

    config.environments[0].paused = True
    config.projects[0].paused = True
    config_loader.save(config, path)
    loaded = config_loader.load(path)
    assert path.read_text().count("paused") == 2
    assert loaded.environments[0].paused is True and loaded.projects[0].paused is True


async def _second_environment(client, tmp_path) -> str:
    """A second project with one environment of one host, so a pause has neighbours to spare."""
    second = tmp_path / "second-env"
    second.write_text("Host two-app\n    HostName 10.50.0.1\n    User ubuntu\n")
    assert (await client.post("/api/settings/projects", json={"name": "acme"})).status_code == 200
    response = await client.post(
        "/api/settings/environments",
        json={"name": "second", "ssh_config_path": str(second), "project": "acme"},
    )
    assert response.status_code == 200, response.text
    return "two-app"


async def test_pausing_an_environment_holds_every_host_in_it(web, fake_ssh, context, tmp_path):
    client, state = web
    neighbour = await _second_environment(client, tmp_path)
    await state.refresh()
    testing = {h.alias for h in context.inventory.hosts("testing")}
    assert testing & context.mux.alive

    response = await client.patch("/api/settings/environments/testing", json={"paused": True})
    assert response.status_code == 200, response.text
    assert config_loader.load(context.paths.config_file).environment("testing").paused is True
    assert not (testing & context.mux.alive)  # every master closed, bastions included
    for alias in testing:
        assert context.inventory.paused_by(alias).to_dict() == {"kind": "environment", "name": "testing"}
    order = (await client.get("/api/hosts/tst-order")).json()
    assert order["paused_by"] == {"kind": "environment", "name": "testing"} and order["snapshot"] is None
    assert "environment testing is paused" in context.inventory.paused_reason("tst-order")
    environments = {e["name"]: e for e in (await client.get("/api/environments")).json()}
    assert environments["testing"]["paused"] is True and environments["second"]["paused"] is False

    mark = len(fake_ssh.calls())
    await state.refresh()
    assert (await context.runner.run("tst-auth", "true")).kind is ErrorKind.PAUSED
    assert _calls_for(fake_ssh, testing, mark) == []
    assert _calls_for(fake_ssh, {neighbour}, mark)  # the other environment is checked as before
    assert not ({h.alias for h in context.inventory.starred()} & testing)

    response = await client.patch("/api/settings/environments/testing", json={"paused": False})
    assert response.status_code == 200, response.text
    assert "paused" not in config_loader.load(context.paths.config_file).environment("testing").to_dict()
    await state.refresh()
    assert state.fleet.snapshot("tst-order").reachable is True


async def test_pausing_a_project_holds_every_environment_in_it(web, fake_ssh, context, tmp_path):
    client, state = web
    neighbour = await _second_environment(client, tmp_path)
    await state.refresh()

    response = await client.patch("/api/settings/projects/lab", json={"paused": True})
    assert response.status_code == 200, response.text
    assert response.json()["project"]["paused"] is True
    assert config_loader.load(context.paths.config_file).project("lab").paused is True
    lab = {h.alias for h in context.inventory.hosts("testing")}
    assert not (lab & context.mux.alive)
    assert context.inventory.paused_by("tst-order").to_dict() == {"kind": "project", "name": "lab"}
    assert context.inventory.paused_by(neighbour) is None
    projects = {p["name"]: p for p in (await client.get("/api/config")).json()["projects"]}
    assert projects["lab"]["paused"] is True and projects["acme"]["paused"] is False

    mark = len(fake_ssh.calls())
    await state.refresh()
    assert _calls_for(fake_ssh, lab, mark) == []
    assert _calls_for(fake_ssh, {neighbour}, mark)

    response = await client.patch("/api/settings/projects/lab", json={"paused": False})
    assert response.status_code == 200, response.text
    assert context.inventory.paused_by("tst-order") is None
    await state.refresh()
    assert state.fleet.snapshot("tst-order").reachable is True


async def test_the_widest_pause_is_named_and_each_keeps_its_own_flag(web, context):
    client, _state = web
    await client.patch("/api/settings/hosts/tst-order", json={"paused": True})
    await client.patch("/api/settings/environments/testing", json={"paused": True})
    await client.patch("/api/settings/projects/lab", json={"paused": True})
    # The project is the one to resume first, so it is the one named.
    assert context.inventory.paused_by("tst-order").to_dict() == {"kind": "project", "name": "lab"}

    await client.patch("/api/settings/projects/lab", json={"paused": False})
    assert context.inventory.paused_by("tst-order").to_dict() == {"kind": "environment", "name": "testing"}

    # Resuming the environment leaves the host's own pause where it was.
    await client.patch("/api/settings/environments/testing", json={"paused": False})
    assert context.inventory.paused_by("tst-order").to_dict() == {"kind": "host", "name": "tst-order"}
    assert context.inventory.paused_by("tst-auth") is None


async def test_a_host_added_to_a_paused_environment_is_held_from_the_start(web, fake_ssh, context):
    client, _state = web
    await client.patch("/api/settings/environments/testing", json={"paused": True})
    mark = len(fake_ssh.calls())
    response = await client.post(
        "/api/settings/hosts", json={"env": "testing", "alias": "tst-new", "hostname": "10.99.0.77", "user": "ubuntu"}
    )
    assert response.status_code == 200, response.text
    alias = response.json()["alias"]
    assert context.inventory.paused_by(alias).to_dict() == {"kind": "environment", "name": "testing"}
    assert _calls_for(fake_ssh, {alias}, mark) == []
