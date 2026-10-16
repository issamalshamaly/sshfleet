"""Putting an earlier state of the configuration back: exact, checked first, and undoable."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sshfleet.config import loader as config_loader
from sshfleet.history import History, HistoryError, RestoreRefused, TrackedFile, plan_restore

FIXTURES = Path(__file__).parent / "fixtures"


async def _add_host(client, name: str = "extra") -> dict:
    response = await client.post("/api/settings/hosts", json={"env": "testing", "alias": name, "hostname": "10.99.0.77"})
    assert response.status_code == 200, response.text
    return response.json()


async def _restore(client, state, state_id: str, **extra):
    code = state.history.get(state_id).code
    return await client.post(f"/api/settings/history/{state_id}/restore", json={"confirm": code, **extra})


@pytest.fixture
def quiet(web, monkeypatch):
    """No sweep started by a reload left running when the test's event loop closes."""
    _, state = web
    monkeypatch.setattr(state, "wake", lambda: None)
    monkeypatch.setattr(state, "refresh", lambda **_: asyncio.sleep(0))
    return web


async def test_restoring_gives_back_the_exact_bytes(quiet, configured, sample_env_file):
    client, state = quiet
    original_config, original_ssh = configured.read_bytes(), sample_env_file.read_bytes()
    start = state.history.latest().id
    await _add_host(client)
    await client.patch("/api/settings/hosts/tst-order", json={"display": "Orders API"})
    assert configured.read_bytes() != original_config and sample_env_file.read_bytes() != original_ssh

    response = await _restore(client, state, start)
    assert response.status_code == 200, response.text
    body = response.json()
    assert configured.read_bytes() == original_config
    assert sample_env_file.read_bytes() == original_ssh
    assert set(body["written"]) == {str(configured), str(sample_env_file)}
    assert body["reload"]["removed"] == ["lab_test_extra"]
    assert "lab_test_extra" not in state.context.inventory.by_alias
    after = state.history.get(body["after"])
    assert after.source == "restore" and after.restored_from == start
    entry = state.context.audit.tail(1)[0]
    assert entry["action"] == "config.restore" and entry["target"] == start


async def test_restoring_is_undone_by_restoring_the_state_before_it(quiet, configured, sample_env_file):
    client, state = quiet
    start = state.history.latest().id
    await _add_host(client)
    changed_config, changed_ssh = configured.read_bytes(), sample_env_file.read_bytes()
    body = (await _restore(client, state, start)).json()
    undone = await _restore(client, state, body["before"])
    assert undone.status_code == 200, undone.text
    assert configured.read_bytes() == changed_config
    assert sample_env_file.read_bytes() == changed_ssh
    assert "lab_test_extra" in state.context.inventory.by_alias


async def test_an_invalid_state_is_refused_and_nothing_changes(quiet, configured, sample_env_file):
    client, state = quiet
    good = configured.read_text()
    configured.write_text("environments: [unclosed\n")
    broken = (await state.reload(reason="config.yaml changed on disk", source="watcher"))["state"]
    configured.write_text(good)
    await state.reload(reason="config.yaml changed on disk", source="watcher")
    before = (configured.read_bytes(), sample_env_file.read_bytes(), len(state.history._ids()))

    response = await _restore(client, state, broken)
    assert response.status_code == 400
    assert "cannot be restored" in response.json()["detail"]
    assert (configured.read_bytes(), sample_env_file.read_bytes(), len(state.history._ids())) == before
    # Asking what a restore would do says the same, without trying.
    shown = (await client.get(f"/api/settings/history/{broken}", params={"against": "current"})).json()
    assert "refused" in shown["restore"]


async def test_a_newer_environment_file_is_left_in_place(quiet, configured, tmp_path):
    client, state = quiet
    start = state.history.latest().id
    second = tmp_path / "second-env"
    second.write_text("Host two-app\n    HostName 10.50.0.1\n")
    added = await client.post(
        "/api/settings/environments",
        json={"name": "second", "ssh_config_path": str(second), "short": "two", "color": "grape", "project": "lab"},
    )
    assert added.status_code == 200, added.text

    body = (await _restore(client, state, start)).json()
    assert body["left_in_place"] == [str(second)]
    assert second.read_text() == "Host two-app\n    HostName 10.50.0.1\n"  # never deleted
    assert [e.name for e in state.context.config.environments] == ["testing"]


async def test_restoring_needs_the_typed_code(quiet, configured):
    client, state = quiet
    start = state.history.latest().id
    await _add_host(client)
    changed = configured.read_bytes()
    for confirm in ("", "nope", start):  # the full id is not the code either
        response = await client.post(f"/api/settings/history/{start}/restore", json={"confirm": confirm})
        assert response.status_code == 400
    assert configured.read_bytes() == changed
    assert (await client.post("/api/settings/history/20200101T000000000000Z-abcdef/restore", json={"confirm": "abcdef"})).status_code == 404
    assert (await client.get("/api/settings/history/../../etc/passwd")).status_code == 404


async def test_the_list_and_the_diff_explain_each_state(quiet):
    client, _ = quiet
    await client.patch("/api/settings/hosts/tst-order", json={"display": "Orders API"})
    listed = (await client.get("/api/settings/history", params={"limit": 5})).json()
    newest = listed["states"][0]
    assert listed["total"] >= 2
    assert newest["source"] == "web" and newest["code"] == newest["id"][-6:]
    assert all("sha256" not in f for f in newest["files"])  # the page has no use for content hashes
    shown = (await client.get(f"/api/settings/history/{newest['id']}")).json()
    config_diff = next(d for d in shown["diffs"] if d["role"] == "config")
    assert config_diff["change"] == "changed" and "+    display: Orders API" in config_diff["diff"]


def test_a_version_two_state_is_upgraded_on_the_way_back(tmp_path: Path):
    """An old state's config.yaml is upgraded with the aliases its own SSH files held then."""
    config = tmp_path / "config.yaml"
    shutil.copy(FIXTURES / "config-v2-realistic.yaml", config)
    history = History(tmp_path / "history")
    old, _ = history.record([TrackedFile("config", config)], reason="v2", source="cli")
    config.write_text("version: 3\n")
    history.record([TrackedFile("config", config)], reason="v3", source="cli")

    plan = plan_restore(history, old.id, [TrackedFile("config", config)])
    assert plan.migrated is True
    restored = config_loader.parse_text(plan.writes[0].content.decode())
    assert restored.version == 3


def test_a_state_named_by_its_code_alone(tmp_path: Path):
    config = tmp_path / "config.yaml"
    history = History(tmp_path / "history")
    config.write_text("version: 3\n# one\n")
    one, _ = history.record([TrackedFile("config", config)], reason="one", source="cli")
    assert history.resolve(one.code).id == one.id
    with pytest.raises(HistoryError):
        history.resolve("zzzzzz")
    config.write_text("version: 3\nenvironments: [{name: x}]\n")  # no ssh_config_path: does not load
    bad, _ = history.record([TrackedFile("config", config)], reason="bad", source="cli")
    with pytest.raises(RestoreRefused):
        plan_restore(history, bad.code, [TrackedFile("config", config)])


async def test_the_watcher_never_records_half_a_restore(quiet, configured, sample_env_file, monkeypatch):
    """A restore writes several files. The watcher must not catch it between them and record (or
    reload) a mix of the old and the new."""
    client, state = quiet
    start = state.history.latest().id
    await _add_host(client)
    from sshfleet import history as history_module

    real = history_module.replace_file

    def slow(*args, **kwargs):
        result = real(*args, **kwargs)
        time.sleep(2.6)  # longer than a watcher tick plus its settle time
        return result

    monkeypatch.setattr(history_module, "replace_file", slow)
    count = len(state.history._ids())
    body = (await _restore(client, state, start)).json()
    await asyncio.sleep(2.5)  # let the watcher look again after the restore
    sources = [state.history.get(i).source for i in state.history._ids()[count:]]
    assert sources == ["restore", "restore"] or sources == ["restore"]
    assert body["ok"]


def test_the_cli_lists_and_restores(configured, fake_ssh, sample_env_file, monkeypatch):
    from sshfleet.cli.main import app

    runner = CliRunner()
    listed = runner.invoke(app, ["config", "history", "--json"])
    assert listed.exit_code == 0, listed.output
    original = sample_env_file.read_bytes()
    sample_env_file.write_text(sample_env_file.read_text() + "\nHost tst-cli\n    HostName 10.1.1.1\n")
    # Any CLI run notices the hand edit and keeps it.
    runner.invoke(app, ["config", "history"])
    history = History(Path(configured).parents[1] / "state" / "history")
    first = history.list(limit=1000)[0][-1]
    dry = runner.invoke(app, ["config", "restore", first.code, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert sample_env_file.read_bytes() != original
    done = runner.invoke(app, ["config", "restore", first.code, "--yes"])
    assert done.exit_code == 0, done.output
    assert sample_env_file.read_bytes() == original
    assert "Undo with" in done.output
