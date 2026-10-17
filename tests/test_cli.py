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
