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


def test_doctor_names_the_real_cure_for_a_control_path_that_is_too_long(context, tmp_path):
    """It said "chmod 700", which cannot fix "ControlPath too long"."""
    from sshfleet.doctor import run_checks

    deep = tmp_path / ("d" * 60)
    deep.mkdir(mode=0o700)
    context.config.ssh.control_dir = str(deep)
    check = next(c for c in run_checks(context) if c.name == "control dir")
    assert check.level == "warn"
    assert "ssh.control_dir" in check.fix and "chmod" not in check.fix
