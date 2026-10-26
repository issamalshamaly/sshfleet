"""The command line: what a first-time user meets, and the commands that had no test at all."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from sshfleet import __version__
from sshfleet.cli.main import app


def test_sshfleet_on_its_own_shows_where_to_start():
    """It used to open the old terminal interface; now it explains itself."""
    result = CliRunner().invoke(app, [])
    assert "sshfleet web" in result.output
    assert "Usage" in result.output


def test_version_flag_and_command_agree():
    runner = CliRunner()
    assert runner.invoke(app, ["--version"]).output.strip() == __version__
    assert runner.invoke(app, ["version"]).output.strip() == __version__


def test_hosts_lists_the_inventory(configured):
    """Regression: the table read a "service" key the rows no longer had, so it crashed on any
    inventory."""
    runner = CliRunner()
    table = runner.invoke(app, ["hosts", "--env", "testing"])
    assert table.exit_code == 0, table.output
    assert "tst-order" in table.output
    listed = runner.invoke(app, ["hosts", "--json"])
    assert listed.exit_code == 0
    rows = {row["alias"]: row for row in json.loads(listed.output)["data"]}
    assert rows["tst-order"]["label"] == "order" and rows["tst-order"]["main"] is True


def test_hosts_with_no_match_says_so_in_the_exit_code(configured):
    result = CliRunner().invoke(app, ["hosts", "--role", "nonexistent-role"])
    assert result.exit_code != 0


def test_env_add_test_exits_non_zero_when_a_bastion_cannot_be_reached(paths, fake_ssh, scenario, sample_env_file):
    """The environment is registered either way, but the test failed and a script must see it."""
    scenario("bastion_down")
    runner = CliRunner()
    assert runner.invoke(app, ["project", "add", "lab"]).exit_code == 0
    result = runner.invoke(app, ["env", "add", "-n", "testing", "-c", str(sample_env_file), "--project", "lab", "--json"])
    assert result.exit_code == 4, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert any(not outcome["ok"] for outcome in payload["data"]["bastion_test"].values())


def test_env_add_test_exits_zero_when_the_bastions_answer(paths, fake_ssh, sample_env_file):
    runner = CliRunner()
    assert runner.invoke(app, ["project", "add", "lab"]).exit_code == 0
    result = runner.invoke(app, ["env", "add", "-n", "testing", "-c", str(sample_env_file), "--project", "lab", "--json"])
    assert result.exit_code == 0, result.output


def test_env_add_records_production_when_told_and_says_when_it_guessed(paths, fake_ssh, sample_env_file, monkeypatch):
    from sshfleet.cli.output import console

    runner = CliRunner()
    assert runner.invoke(app, ["project", "add", "lab"]).exit_code == 0
    told = runner.invoke(app, ["env", "add", "-n", "main", "-c", str(sample_env_file), "--project", "lab", "--production", "--no-test", "--json"])
    assert told.exit_code == 0, told.output
    assert json.loads(told.output)["data"]["production"] is True and json.loads(told.output)["data"]["production_set"] is True

    guessed = runner.invoke(app, ["env", "add", "-n", "live", "-c", str(sample_env_file), "--project", "lab", "--no-test"])
    assert guessed.exit_code == 0, guessed.output
    assert "production: yes, guessed from its name" in guessed.output

    listed = json.loads(runner.invoke(app, ["env", "list", "--json"]).output)["data"]
    assert [(e["name"], e["production"], e["production_set"]) for e in listed] == [("main", True, True), ("live", True, False)]
    monkeypatch.setattr(console, "width", 240)  # room for the long temporary paths
    assert "yes (by name)" in runner.invoke(app, ["env", "list"]).output


def test_doctor_names_the_real_cure_for_a_control_path_that_is_too_long(context, tmp_path):
    """It said "chmod 700", which cannot fix "ControlPath too long"."""
    from sshfleet.doctor import run_checks

    deep = tmp_path / ("d" * 60)
    deep.mkdir(mode=0o700)
    context.config.ssh.control_dir = str(deep)
    check = next(c for c in run_checks(context) if c.name == "control dir")
    assert check.level == "warn"
    assert "ssh.control_dir" in check.fix and "chmod" not in check.fix


def test_doctor_says_how_host_keys_are_believed():
    from sshfleet.doctor import host_key_check

    first_use = host_key_check("accept-new")
    assert first_use.level == "ok" and "first time it is seen" in first_use.detail
    assert host_key_check("yes").level == "ok"
    for blind in ("no", "off"):
        check = host_key_check(blind)
        assert check.level == "warn" and "any key is accepted" in check.detail
    assert host_key_check("ask").level == "warn", "BatchMode makes ask fail on every new host"


def test_a_container_that_is_gone_turns_the_fleet_table_red():
    """The table read a field the collector never filled, so a gone container never showed."""
    from sshfleet.cli.fleet_cmds import _containers_cell
    from sshfleet.collector.model import HostSnapshot

    snapshot = HostSnapshot(alias="tst-order", env="testing", reachable=True, runtime="docker")
    snapshot.vanished_containers = ["order-production"]
    assert _containers_cell(snapshot).style == "red"
    assert "missing_containers" not in snapshot.to_dict(), "one name for one thing"


def test_the_package_and_pyproject_agree_on_the_version():
    """The release workflow checks the tag against pyproject.toml; --version reads the package."""
    import tomllib
    from pathlib import Path

    from sshfleet import __version__

    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    assert project["version"] == __version__
