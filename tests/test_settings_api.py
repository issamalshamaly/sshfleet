"""The settings API: registering environments, editing hosts, and noticing edits made by hand."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sshfleet.web import state as state_module


@pytest.fixture(autouse=True)
def fast_watch(monkeypatch: pytest.MonkeyPatch):
    """Poll the config files quickly so the watcher tests do not wait two seconds."""
    monkeypatch.setattr(state_module, "WATCH_INTERVAL_S", 0.05)


async def wait_for_event(state, kind: str, timeout: float = 6.0) -> dict:
    queue = state.events.subscribe()
    try:
        async with asyncio.timeout(timeout):
            while True:
                event = await queue.get()
                if event and event.get("type") == kind:
                    return event
    finally:
        state.events.unsubscribe(queue)


# ------------------------------------------------------------------------------ reading


async def test_settings_describe_the_environment_and_what_is_watched(web, sample_env_file):
    client, _ = web
    data = (await client.get("/api/settings")).json()
    environment = data["environments"][0]
    assert environment["name"] == "testing"
    assert environment["ssh_config_path"] == str(sample_env_file)
    assert environment["hosts"] == 8
    assert environment["bastions"] == ["tst-bastion", "tst-jenkins-proxy"]
    assert str(sample_env_file) in data["watching"]
    assert data["refresh"]["fast_s"] == 120  # the default sweep
    assert Path(data["paths"]["config_file"]).name == "config.yaml"


async def test_identity_files_are_described_by_metadata_and_never_read(web, tmp_path, monkeypatch):
    client, _ = web
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "extra.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nsuper-secret\n")
    key.chmod(0o644)

    body = (await client.get("/api/settings/keys")).text
    assert "super-secret" not in body and "BEGIN PRIVATE KEY" not in body
    entry = next(e for e in (await client.get("/api/settings/keys")).json() if e["path"] == str(key))
    assert entry["ok"] is False and entry["mode"] == "0644"
    assert "too open" in " ".join(entry["problems"])
    assert entry["fix"] == f"chmod 600 {key}"


async def test_candidate_ssh_configs_skip_anything_that_looks_like_a_key(web, tmp_path, monkeypatch):
    client, _ = web
    monkeypatch.setenv("HOME", str(tmp_path))
    config_d = tmp_path / ".ssh" / "config.d"
    config_d.mkdir(parents=True)
    (config_d / "real-env").write_text("Host a\n    HostName 10.0.0.1\n")
    (config_d / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")

    listed = (await client.get("/api/settings/ssh-configs")).json()
    names = [entry["name"] for entry in listed]
    assert "real-env" in names
    assert "id_ed25519" not in names
    assert next(e for e in listed if e["name"] == "real-env")["hosts"] == 1


async def test_preview_suggests_how_to_register_a_file(web, sample_env_file):
    client, _ = web
    data = (await client.post("/api/settings/environments/preview", json={"ssh_config_path": str(sample_env_file)})).json()
    assert data["prefix"] == "tst-"
    assert data["bastions"] == ["tst-bastion", "tst-jenkins-proxy"]
    assert "tst-order" in data["hosts"]
    assert data["key_candidates"][0]["path"].endswith("test.pem")


async def test_preview_refuses_a_key_file(web, tmp_path):
    client, _ = web
    key = tmp_path / "secret.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\n")
    response = await client.post("/api/settings/environments/preview", json={"ssh_config_path": str(key)})
    assert response.status_code == 400
    assert "not an ssh config" in response.json()["detail"]


# -------------------------------------------------------------------------------- hosts


async def test_adding_a_host_writes_the_stanza_and_the_fleet_sees_it(web, sample_env_file):
    client, state = web
    response = await client.post(
        "/api/settings/hosts",
        json={
            "env": "testing",
            "alias": "extra",  # the environment's prefix is added: <project>_<short>_<name>
            "hostname": "10.99.0.77",
            "user": "ubuntu",
            "proxy_jump": "tst-bastion",
            "label": "extra",
            "main": True,
            "display": "extra order box",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["alias"] == "lab_test_extra"
    assert body["reload"]["added"] == ["lab_test_extra"]
    assert Path(body["backup"]).exists()

    text = sample_env_file.read_text()
    assert "Host lab_test_extra" in text and "ProxyJump tst-bastion" in text
    assert text.index("Host lab_test_extra") < text.index("Host *")

    listed = {h["alias"]: h for h in (await client.get("/api/settings/hosts")).json()}
    assert listed["lab_test_extra"]["proxy_jump"] == "tst-bastion"
    assert listed["lab_test_extra"]["label"] == "extra" and listed["lab_test_extra"]["main"] is True
    assert any(h["alias"] == "lab_test_extra" for h in (await client.get("/api/fleet")).json()["hosts"])
    assert state.context.config.hosts["lab_test_extra"].display == "extra order box"

    testing = next(e for e in (await client.get("/api/settings")).json()["environments"] if e["name"] == "testing")
    assert testing["host_prefix"] == "lab_test_"
    # A whole alias pasted in keeps the prefix it has instead of getting a second one.
    pasted = await client.post("/api/settings/hosts", json={"env": "testing", "alias": "lab_test_more", "hostname": "10.99.0.78"})
    assert pasted.json()["alias"] == "lab_test_more"
    assert (await client.post("/api/settings/hosts", json={"env": "testing", "alias": "  "})).status_code == 400


async def test_a_host_can_be_made_direct_and_given_a_new_address(web, sample_env_file):
    client, _ = web
    response = await client.patch("/api/settings/hosts/tst-order", json={"hostname": "10.99.0.55", "proxy_jump": ""})
    assert response.status_code == 200, response.text
    host = (await client.get("/api/hosts/tst-order")).json()
    assert host["hostname"] == "10.99.0.55"
    assert host["via"] is None
    assert "Host tst-auth" in sample_env_file.read_text()  # its neighbour survived


async def test_a_parent_that_would_loop_is_refused(web):
    client, _ = web
    response = await client.patch("/api/settings/hosts/tst-bastion", json={"proxy_jump": "tst-order"})
    assert response.status_code == 400
    assert "loop" in response.json()["detail"]


async def test_an_unknown_parent_is_refused(web):
    client, _ = web
    response = await client.patch("/api/settings/hosts/tst-order", json={"proxy_jump": "not-a-host"})
    assert response.status_code == 400
    assert "unknown host" in response.json()["detail"]


async def test_removing_a_host_needs_the_typed_alias(web, sample_env_file):
    client, _ = web
    refused = await client.delete("/api/settings/hosts/tst-auth?confirm=wrong")
    assert refused.status_code == 400
    assert "Host tst-auth" in sample_env_file.read_text()

    accepted = await client.delete("/api/settings/hosts/tst-auth?confirm=tst-auth")
    assert accepted.status_code == 200
    assert "tst-auth" not in sample_env_file.read_text()
    assert accepted.json()["reload"]["removed"] == ["tst-auth"]
    assert all(h["alias"] != "tst-auth" for h in (await client.get("/api/fleet")).json()["hosts"])


async def test_a_host_sets_how_often_its_endpoint_is_called(web, sample_env_file):
    """Some endpoints write a block of log on every call, so the interval belongs to the host."""
    client, state = web

    response = await client.patch("/api/settings/hosts/tst-order", json={"probe_min_interval_s": 900})
    assert response.status_code == 200, response.text
    assert state.context.inventory.host("tst-order").probe_min_interval_s == 900
    assert state.context.config.hosts["tst-order"].health["min_interval_s"] == 900
    assert "Host tst-order" in sample_env_file.read_text()  # a tool-side setting, not an ssh one

    row = next(h for h in (await client.get("/api/settings/hosts")).json() if h["alias"] == "tst-order")
    assert row["probe_min_interval_s"] == 900

    # An unrelated edit must not disturb it.
    await client.patch("/api/settings/hosts/tst-order", json={"display": "orders box"})
    assert state.context.inventory.host("tst-order").probe_min_interval_s == 900

    assert (await client.patch("/api/settings/hosts/tst-order", json={"probe_min_interval_s": -1})).status_code == 200
    assert state.context.inventory.host("tst-order").probe_min_interval_s == 0


async def test_an_impossible_probe_setting_is_refused(web):
    client, state = web
    assert (await client.patch("/api/settings/hosts/tst-order", json={"probe_min_interval_s": 99999})).status_code == 400
    assert (await client.patch("/api/settings/hosts/tst-order", json={"health_port": 70000})).status_code == 400
    assert (await client.patch("/api/settings/hosts/tst-order", json={"health_path": "health"})).status_code == 400
    assert state.context.inventory.host("tst-order").health_port == 3001  # unchanged


async def test_a_host_can_point_the_probe_somewhere_else(web):
    client, state = web
    response = await client.patch("/api/settings/hosts/tst-order", json={"health_port": 8081, "health_path": "/healthz"})
    assert response.status_code == 200, response.text
    host = state.context.inventory.host("tst-order")
    assert (host.health_port, host.health_path) == (8081, "/healthz")

    await client.patch("/api/settings/hosts/tst-order", json={"health_port": 0, "health_path": ""})
    host = state.context.inventory.host("tst-order")
    assert (host.health_port, host.health_path) == (None, None)  # nothing to probe until you say so


async def test_a_host_is_starred_labelled_and_filtered_from_the_api(web):
    """Everything the host model holds, through the one endpoint that edits a host."""
    client, state = web
    response = await client.patch(
        "/api/settings/hosts/tst-jenkins",
        json={"main": True, "label": "ci", "role": "ops", "workloads": "jenkins-*", "log_format": "plain"},
    )
    assert response.status_code == 200, response.text
    host = state.context.inventory.host("tst-jenkins")
    assert host.main is True and host.log_label == "ci" and host.role == "ops"
    assert host.workload_glob == "jenkins-*" and host.log_format == "plain"
    assert host.watches("jenkins-main") and not host.watches("redis")

    row = next(h for h in (await client.get("/api/settings/hosts")).json() if h["alias"] == "tst-jenkins")
    assert row["main"] is True and row["label"] == "ci" and row["workloads"] == "jenkins-*"

    # A role nobody recognises falls back rather than breaking the fleet view.
    await client.patch("/api/settings/hosts/tst-jenkins", json={"role": "spaceship"})
    assert state.context.inventory.host("tst-jenkins").role == "service"


async def test_editing_an_unknown_host_is_404(web):
    client, _ = web
    assert (await client.patch("/api/settings/hosts/nope", json={"user": "ubuntu"})).status_code == 404


# ------------------------------------------------------------------------- environments


async def test_an_environment_can_be_added_and_removed_without_touching_the_ssh_file(web, tmp_path, sample_env_file):
    client, state = web
    second = tmp_path / "second-env"
    second.write_text("Host two-app\n    HostName 10.50.0.1\n    User ubuntu\n")
    original = second.read_text()

    added = await client.post(
        "/api/settings/environments",
        json={"name": "second", "ssh_config_path": str(second), "short": "two", "color": "grape", "project": "lab"},
    )
    assert added.status_code == 200, added.text
    assert [e["name"] for e in (await client.get("/api/settings")).json()["environments"]] == ["testing", "second"]
    assert any(h["alias"] == "two-app" for h in (await client.get("/api/fleet")).json()["hosts"])

    removed = await client.delete("/api/settings/environments/second")
    assert removed.status_code == 200
    assert [e["name"] for e in (await client.get("/api/settings")).json()["environments"]] == ["testing"]
    assert second.read_text() == original  # the file itself is never edited by this
    assert state.fleet.state.snapshots.get("two-app") is None


async def test_an_environment_can_be_relabelled_and_rekeyed(web, tmp_path):
    client, _ = web
    key = tmp_path / "other.pem"
    key.write_text("x")
    key.chmod(0o600)
    response = await client.patch(
        "/api/settings/environments/testing",
        json={"short": "tst", "color": "red", "key_path": str(key)},
    )
    assert response.status_code == 200, response.text
    environment = response.json()["environment"]
    assert environment["short"] == "tst" and environment["color"] == "red"
    assert environment["key"]["ok"] is True


async def test_a_duplicate_environment_is_refused_unless_replacing(web, sample_env_file):
    client, _ = web
    body = {"name": "testing", "ssh_config_path": str(sample_env_file), "project": "lab"}
    assert (await client.post("/api/settings/environments", json=body)).status_code == 400
    assert (await client.post("/api/settings/environments", json={**body, "replace": True})).status_code == 200


async def test_two_projects_can_each_have_a_staging(web, tmp_path, monkeypatch):
    """What a second project needs: its own "testing", told apart from lab's by its id."""
    monkeypatch.setenv("SSHFLEET_SSH_DIR", str(tmp_path / "ssh"))
    client, state = web
    assert (await client.post("/api/settings/projects", json={"name": "acme"})).status_code == 200

    added = await client.post("/api/settings/environments", json={"name": "testing", "ssh_config_path": "", "project": "acme"})
    assert added.status_code == 200, added.text
    assert added.json()["environment"] == "acme-testing"
    settings = (await client.get("/api/settings")).json()
    assert [(e["id"], e["name"], e["project"]) for e in settings["environments"]] == [
        ("testing", "testing", "lab"),
        ("acme-testing", "testing", "acme"),
    ]
    assert [p["environments"] for p in settings["projects"]] == [["testing"], ["acme-testing"]]
    assert [e["id"] for e in (await client.get("/api/config")).json()["environments"]] == ["testing", "acme-testing"]

    # A host goes to the environment the id names, and the fleet keeps the two apart.
    host = await client.post("/api/settings/hosts", json={"env": "acme-testing", "alias": "web", "hostname": "192.0.2.10"})
    assert host.status_code == 200, host.text
    assert (tmp_path / "ssh" / "config.d" / "acme_test").read_text().count("Host acme_test_web") == 1
    hosts = (await client.get("/api/fleet")).json()["hosts"]
    assert [h["alias"] for h in hosts if h["env"] == "acme-testing"] == ["acme_test_web"]
    assert sum(h["env"] == "testing" for h in hosts) == 8

    # Pausing one leaves the other being watched.
    assert (await client.patch("/api/settings/environments/acme-testing", json={"paused": True})).status_code == 200
    assert state.context.inventory.paused_by("acme_test_web").to_dict() == {"kind": "environment", "name": "acme-testing"}
    assert state.context.inventory.paused_by("tst-order") is None

    # lab cannot take a second "testing" either way: neither added nor moved in.
    moved = await client.patch("/api/settings/environments/testing", json={"project": "acme"})
    assert moved.status_code == 400 and "already has an environment called 'testing'" in moved.json()["detail"]

    removed = await client.delete("/api/settings/environments/acme-testing")
    assert removed.status_code == 200
    assert [e["id"] for e in (await client.get("/api/settings")).json()["environments"]] == ["testing"]


async def test_a_name_the_project_has_is_refused_before_any_file_is_made(web, tmp_path, monkeypatch):
    """A refused registration used to leave its new, empty file behind in ~/.ssh/config.d."""
    monkeypatch.setenv("SSHFLEET_SSH_DIR", str(tmp_path / "ssh"))
    client, _ = web
    response = await client.post(
        "/api/settings/environments", json={"name": "testing", "short": "t2", "ssh_config_path": "", "project": "lab"}
    )
    assert response.status_code == 400
    assert "project 'lab' already has an environment called 'testing'" in response.json()["detail"]
    assert not (tmp_path / "ssh" / "config.d" / "lab_t2").exists()


# --------------------------------------------------------------- refresh interval, watch


async def test_the_refresh_interval_is_saved_and_applied_without_a_restart(web):
    client, state = web
    response = await client.put("/api/settings/refresh", json={"fast_s": 120, "stale_after_s": 300})
    assert response.status_code == 200, response.text
    assert state.context.config.refresh.fast_s == 120
    assert state.context.config.refresh.stale_after_s == 300
    assert state._wake.is_set()  # the sleeping loop was woken so the new interval applies now

    from sshfleet.config import loader as config_loader

    assert config_loader.load(state.context.paths.config_file).refresh.fast_s == 120


async def test_automatic_refresh_can_be_turned_off(web):
    client, state = web
    assert (await client.put("/api/settings/refresh", json={"fast_s": 0})).status_code == 200
    assert state.context.config.refresh.fast_s == 0


async def test_an_impossible_interval_is_refused(web):
    client, _ = web
    assert (await client.put("/api/settings/refresh", json={"fast_s": -5})).status_code == 422


async def test_warning_thresholds_must_stay_below_critical_ones(web):
    client, _ = web
    response = await client.put("/api/settings/thresholds", json={"disk_warn_pct": 95, "disk_crit_pct": 90})
    assert response.status_code == 400
    assert (await client.put("/api/settings/thresholds", json={"disk_warn_pct": 70})).status_code == 200


async def test_a_host_added_to_the_file_by_hand_appears_without_a_restart(web, sample_env_file):
    client, state = web
    sample_env_file.write_text(
        sample_env_file.read_text().replace(
            "Host *\n",
            "Host tst-handmade\n    HostName 10.99.0.88\n    User ubuntu\n    ProxyJump tst-bastion\n\nHost *\n",
        )
    )
    event = await wait_for_event(state, "inventory")
    assert event["added"] == ["tst-handmade"]
    assert "changed on disk" in event["reason"]

    hosts = (await client.get("/api/fleet")).json()["hosts"]
    assert any(h["alias"] == "tst-handmade" and h["via"] == "tst-bastion" for h in hosts)


async def test_reloading_by_hand_reports_what_changed(web, sample_env_file):
    client, _ = web
    sample_env_file.write_text(sample_env_file.read_text() + "\nHost tst-late\n    HostName 10.99.0.90\n")
    summary = (await client.post("/api/settings/reload")).json()
    assert summary["ok"] is True
    assert summary["added"] == ["tst-late"]
    assert summary["hosts"] == 9


async def test_leaving_bastions_empty_means_detect_them(web, tmp_path):
    """An empty list is "work it out from ProxyJump", not "this environment has none"."""
    client, _ = web
    second = tmp_path / "third-env"
    second.write_text(
        "Host three-jump\n    HostName 10.60.0.1\n    User ubuntu\n\n"
        "Host three-app\n    HostName 10.60.0.2\n    User ubuntu\n    ProxyJump three-jump\n"
    )
    response = await client.post(
        "/api/settings/environments",
        json={"name": "third", "ssh_config_path": str(second), "bastions": [], "project": "lab"},
    )
    assert response.status_code == 200, response.text
    environment = next(e for e in (await client.get("/api/settings")).json()["environments"] if e["name"] == "third")
    assert environment["bastions"] == ["three-jump"]
    assert environment["bastions_auto"] is True


# ----------------------------------------------------------------------------- projects


async def test_a_project_is_created_empty_and_an_environment_moves_into_it(web):
    """Projects come first: the project exists, then environments belong to it."""
    client, _ = web
    created = await client.post("/api/settings/projects", json={"name": "acme", "color": "violet"})
    assert created.status_code == 200, created.text
    assert created.json()["project"] == {
        "name": "acme", "color": "violet", "description": "", "environments": [], "hosts": 0, "paused": False
    }

    response = await client.patch("/api/settings/environments/testing", json={"project": "acme"})
    assert response.status_code == 200, response.text
    assert response.json()["environment"]["project"] == "acme"

    settings = (await client.get("/api/settings")).json()
    acme = next(p for p in settings["projects"] if p["name"] == "acme")
    assert acme["environments"] == ["testing"] and acme["hosts"] == 8


async def test_an_environment_cannot_be_created_without_a_project(web, tmp_path):
    """The invariant the whole model rests on: there is no environment outside a project."""
    client, _ = web
    second = tmp_path / "loose"
    second.write_text("Host loose-one\n    HostName 10.70.0.1\n    User ubuntu\n")

    response = await client.post("/api/settings/environments", json={"name": "loose", "ssh_config_path": str(second)})
    assert response.status_code == 422  # the field is required, not merely validated

    response = await client.post(
        "/api/settings/environments", json={"name": "loose", "ssh_config_path": str(second), "project": ""}
    )
    assert response.status_code == 400
    assert "belongs to a project" in response.json()["detail"]


async def test_an_environment_cannot_be_moved_out_of_every_project(web):
    client, _ = web
    response = await client.patch("/api/settings/environments/testing", json={"project": ""})
    assert response.status_code == 400
    assert "belongs to a project" in response.json()["detail"]
    assert (await client.get("/api/settings")).json()["environments"][0]["project"] == "lab"


async def test_a_project_must_exist_before_an_environment_can_name_it(web):
    client, _ = web
    response = await client.patch("/api/settings/environments/testing", json={"project": "ghost"})
    assert response.status_code == 400
    assert "unknown project" in response.json()["detail"]


async def test_renaming_a_project_carries_its_environments_along(web):
    client, _ = web
    response = await client.patch("/api/settings/projects/lab", json={"name": "lab-platform"})
    assert response.status_code == 200, response.text
    assert response.json()["project"]["environments"] == ["testing"]

    settings = (await client.get("/api/settings")).json()
    assert [p["name"] for p in settings["projects"]] == ["lab-platform"]
    assert settings["environments"][0]["project"] == "lab-platform"


async def test_a_project_holding_environments_cannot_be_removed(web):
    """Removing it would leave an environment belonging to nothing, so it is refused by name."""
    client, _ = web
    response = await client.delete("/api/settings/projects/lab", params={"confirm": "lab"})
    assert response.status_code == 400
    assert "still holds testing" in response.json()["detail"]
    assert (await client.get("/api/settings")).json()["environments"][0]["project"] == "lab"


async def test_an_empty_project_is_removed_once_its_name_is_typed(web):
    client, _ = web
    await client.post("/api/settings/projects", json={"name": "acme"})
    assert (await client.delete("/api/settings/projects/acme")).status_code == 400  # needs the name
    assert (await client.delete("/api/settings/projects/acme", params={"confirm": "acme"})).status_code == 200
    assert [p["name"] for p in (await client.get("/api/settings")).json()["projects"]] == ["lab"]


async def test_a_duplicate_project_is_refused(web):
    client, _ = web
    assert (await client.post("/api/settings/projects", json={"name": "acme"})).status_code == 200
    response = await client.post("/api/settings/projects", json={"name": "acme"})
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]


# ------------------------------------------------------------------------------ tunnels


async def test_a_tunnel_can_be_added_edited_and_removed(web):
    client, state = web
    added = await client.post(
        "/api/settings/tunnels",
        json={
            "name": "kibana-test",
            "env": "testing",
            "via": "tst-bastion",
            "remote_host": "127.0.0.1",
            "remote_port": 5601,
            "local": 15601,
            "open_url": "http://127.0.0.1:15601",
        },
    )
    assert added.status_code == 200, added.text

    listed = (await client.get("/api/tunnels")).json()
    assert [t["name"] for t in listed] == ["kibana-test"]
    assert listed[0]["remote"] == "127.0.0.1:5601"
    assert listed[0]["endpoint"] == "127.0.0.1:15601"
    assert listed[0]["via"] == "tst-bastion"

    changed = await client.patch("/api/settings/tunnels/kibana-test", json={"local": 15602})
    assert changed.status_code == 200, changed.text
    assert (await client.get("/api/tunnels")).json()[0]["endpoint"] == "127.0.0.1:15602"

    removed = await client.delete("/api/settings/tunnels/kibana-test")
    assert removed.status_code == 200
    assert (await client.get("/api/tunnels")).json() == []
    assert "kibana-test" not in state.context.config.tunnels

    # Every tunnel action names its environment, or the trail cannot be filtered by one.
    entries = (await client.get("/api/audit", params={"limit": 20})).json()["entries"]
    for action in ("settings.tunnel.add", "settings.tunnel.update", "settings.tunnel.remove"):
        entry = next(e for e in entries if e["action"] == action)
        assert entry["env"] == "testing", action


async def test_a_tunnel_needs_a_real_host_and_a_free_local_port(web):
    client, _ = web
    body = {"name": "one", "env": "testing", "via": "tst-bastion", "remote_port": 5601, "local": 15601}
    assert (await client.post("/api/settings/tunnels", json=body)).status_code == 200

    unknown_host = await client.post("/api/settings/tunnels", json={**body, "name": "two", "via": "nope", "local": 15603})
    assert unknown_host.status_code == 400
    assert "unknown host" in unknown_host.json()["detail"]

    clash = await client.post("/api/settings/tunnels", json={**body, "name": "three"})
    assert clash.status_code == 400
    assert "already used by the one tunnel" in clash.json()["detail"]

    duplicate = await client.post("/api/settings/tunnels", json={**body, "local": 15604})
    assert duplicate.status_code == 400
    assert "already exists" in duplicate.json()["detail"]


async def test_new_tunnels_are_visible_without_a_restart(web):
    """Regression: the tunnel manager kept a reference to the Config object reload replaced."""
    client, state = web
    await client.post(
        "/api/settings/tunnels",
        json={"name": "seq-test", "env": "testing", "via": "tst-bastion", "remote_port": 8001, "local": 18001},
    )
    assert [t.name for t in state.tunnels.status()] == ["seq-test"]
    assert state.tunnels.config is state.context.config


async def test_the_default_sweep_is_two_minutes(web):
    client, _ = web
    data = (await client.get("/api/config")).json()
    assert data["refresh"]["fast_s"] == 120
    assert data["refresh"]["stale_after_s"] > data["refresh"]["fast_s"]


# ------------------------------------------------------- environments without a file


async def test_an_environment_can_start_with_no_file_and_grow_from_the_hosts_tab(web, tmp_path, monkeypatch):
    monkeypatch.setenv("SSHFLEET_SSH_DIR", str(tmp_path / "ssh"))
    client, _ = web

    # The file is named <project>_<short>; without a short label, the one suggested from the name.
    preview = await client.post(
        "/api/settings/environments/preview", json={"ssh_config_path": "", "name": "Lab Rack", "short": "Rack 1", "project": "lab"}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["new_file"] is True
    assert preview.json()["hosts"] == []
    assert preview.json()["path"].endswith("/ssh/config.d/lab_rack-1")
    guessed = await client.post("/api/settings/environments/preview", json={"ssh_config_path": "", "name": "production", "project": "lab"})
    assert guessed.json()["path"].endswith("/ssh/config.d/lab_prod")
    orphan = await client.post("/api/settings/environments/preview", json={"ssh_config_path": "", "name": "lab"})
    assert orphan.status_code == 400

    response = await client.post("/api/settings/environments", json={"name": "lab", "ssh_config_path": "", "project": "lab"})
    assert response.status_code == 200, response.text
    created = tmp_path / "ssh" / "config.d" / "lab_lab"
    assert response.json()["created_file"] == str(created)
    assert next(e for e in (await client.get("/api/settings")).json()["environments"] if e["name"] == "lab")["short"] == "lab"

    # A second environment with the same short label would share the file, and its hosts.
    clash = await client.post("/api/settings/environments", json={"name": "lab2", "short": "lab", "ssh_config_path": "", "project": "lab"})
    assert clash.status_code == 400 and "another short label" in clash.text
    assert created.stat().st_mode & 0o777 == 0o600
    assert created.parent.stat().st_mode & 0o777 == 0o700
    assert "ssh_include" in response.json()

    lab = next(e for e in (await client.get("/api/settings")).json()["environments"] if e["name"] == "lab")
    assert lab["hosts"] == 0 and lab["ssh_config_exists"] is True

    test = (await client.post("/api/settings/environments/lab/test")).json()
    assert test["ok"] is True and "add a host" in test["note"]

    first = await client.post(
        "/api/settings/hosts", json={"env": "lab", "alias": "web", "hostname": "192.0.2.10", "user": "ubuntu"}
    )
    assert first.status_code == 200, first.text
    text = created.read_text()
    assert text.startswith("# lab:") and "Host lab_lab_web" in text  # header kept, stanza appended
    assert any(h["alias"] == "lab_lab_web" for h in (await client.get("/api/fleet")).json()["hosts"])

    # Re-pointing an environment at a file that does not exist needs an explicit `create`.
    refused = await client.patch("/api/settings/environments/lab", json={"ssh_config_path": str(tmp_path / "ssh" / "config.d" / "elsewhere")})
    assert refused.status_code == 400
    moved = await client.patch(
        "/api/settings/environments/lab", json={"ssh_config_path": str(tmp_path / "ssh" / "config.d" / "elsewhere"), "create": True}
    )
    assert moved.status_code == 200, moved.text
    assert (tmp_path / "ssh" / "config.d" / "elsewhere").exists()


async def test_the_include_line_is_offered_and_added_only_on_request(web, tmp_path, monkeypatch):
    ssh = tmp_path / "ssh"
    ssh.mkdir()
    (ssh / "config").write_text("Host x\n  HostName 192.0.2.1\n")
    monkeypatch.setenv("SSHFLEET_SSH_DIR", str(ssh))
    client, _ = web

    status = (await client.get("/api/settings/ssh-include")).json()
    assert status["includes_config_d"] is False
    assert status["line"] == f"Include {ssh}/config.d/*"

    # Registering an environment reports the gap but does not touch ~/.ssh/config.
    await client.post("/api/settings/environments", json={"name": "lab", "ssh_config_path": "", "project": "lab"})
    assert (ssh / "config").read_text() == "Host x\n  HostName 192.0.2.1\n"

    added = await client.post("/api/settings/ssh-include")
    assert added.status_code == 200, added.text
    text = (ssh / "config").read_text()
    assert text.startswith(f"Include {ssh}/config.d/*\n\nHost x\n")
    assert Path(added.json()["backup"]).exists()
    assert (await client.get("/api/settings/ssh-include")).json()["includes_config_d"] is True

    again = (await client.post("/api/settings/ssh-include")).json()
    assert again["backup"] is None
    assert (ssh / "config").read_text().count("Include") == 1

    # A user with no ~/.ssh/config at all gets one, private.
    (ssh / "config").unlink()
    created = (await client.post("/api/settings/ssh-include")).json()
    assert created["backup"] is None and created["includes_config_d"] is True
    assert (ssh / "config").stat().st_mode & 0o777 == 0o600


async def test_a_vanished_file_empties_one_environment_not_the_fleet(web, sample_env_file, tmp_path, monkeypatch):
    monkeypatch.setenv("SSHFLEET_SSH_DIR", str(tmp_path / "ssh"))
    client, _ = web
    await client.post("/api/settings/environments", json={"name": "lab", "ssh_config_path": "", "project": "lab"})
    await client.post("/api/settings/hosts", json={"env": "lab", "alias": "web", "hostname": "192.0.2.10"})

    sample_env_file.unlink()  # the testing environment's file disappears behind the tool's back
    reload = (await client.post("/api/settings/reload")).json()
    assert reload["ok"] is True
    assert any("testing" in w for w in reload["warnings"])
    aliases = {h["alias"] for h in (await client.get("/api/fleet")).json()["hosts"]}
    assert aliases == {"lab_lab_web"}
    assert any("testing" in w for w in (await client.get("/api/config")).json()["warnings"])


# ------------------------------------------------------------------------------ sounds


async def test_sounds_are_a_setting_with_known_names(web):
    client, state = web
    before = (await client.get("/api/config")).json()["ui"]["sounds"]
    assert before["enabled"] is False and before["crit"] == "alarm"

    response = await client.put("/api/settings/sounds", json={"enabled": True, "crit": "bell", "desktop": True})
    assert response.status_code == 200, response.text
    after = (await client.get("/api/config")).json()["ui"]["sounds"]
    assert after == {"enabled": True, "crit": "bell", "warn": "chime", "done": "soft", "desktop": True}
    assert state.context.config.ui["sounds"]["crit"] == "bell"
    assert state.context.config.ui["open_ssh"] == "auto"  # the rest of ui survived

    assert (await client.put("/api/settings/sounds", json={"warn": "kazoo"})).status_code == 400
