"""The commands people script against, run whole against the fake fleet: output and exit codes.

Exit codes are part of the interface (README): 0 results, 1 nothing found, 2 usage, 3 partial
failure, 4 total failure.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from sshfleet.cli.main import app


def _json(output: str) -> dict:
    return json.loads(output[output.index("{"):])


def test_fleet_reports_every_host_and_exits_0_when_all_answer(context):
    result = CliRunner().invoke(app, ["fleet", "--json"])
    assert result.exit_code == 0, result.output
    payload = _json(result.output)
    assert payload["ok"] is True
    aliases = {h["alias"] for h in payload["data"]}
    assert {"tst-order", "tst-auth"} <= aliases
    assert all(h["reachable"] for h in payload["data"])


def test_fleet_exit_code_says_how_much_failed(context, scenario):
    scenario("bastion_down")
    result = CliRunner().invoke(app, ["fleet", "--json"])
    payload = _json(result.output)
    reached = [h["reachable"] for h in payload["data"]]
    assert payload["ok"] is False
    assert result.exit_code == (4 if not any(reached) else 3), result.output


def test_fleet_with_nothing_matching_exits_1(context):
    result = CliRunner().invoke(app, ["fleet", "--host", "no-such-host-anywhere"])
    assert result.exit_code == 1
    assert "no hosts matched" in result.output


def test_containers_lists_what_runs_and_what_is_gone(context):
    result = CliRunner().invoke(app, ["containers", "tst-order", "--json"])
    assert result.exit_code == 0, result.output
    payload = _json(result.output)["data"]
    assert payload["host"] == "tst-order"
    assert any(c["name"] == "order-production" for c in payload["containers"])
    assert payload["gone"] == []


def test_containers_of_an_unknown_host_is_a_usage_error(context):
    assert CliRunner().invoke(app, ["containers", "no-such-host"]).exit_code == 2


def test_logs_search_finds_and_says_when_it_found_nothing(context, fake_ssh):
    fake_ssh.write_fixture(
        "tst-order", "logsearch.txt",
        "2026-09-13T11:00:01.000000000Z [2026-09-13 11:00:01.000 +0000] ERROR: boom\n",
    )
    # No pattern: the fake replays the host's answer as it is, and matching happens on the host.
    search = ["logs", "search", "-e", "testing", "-c", "tst-order:order-production", "--since", "2026-09-13T10:00:00Z..2026-09-13T12:00:00Z"]
    found = CliRunner().invoke(app, search)
    assert found.exit_code == 0, found.output
    assert "boom" in found.output

    fake_ssh.write_fixture("tst-order", "logsearch.txt", "")
    nothing = CliRunner().invoke(app, search)
    assert nothing.exit_code == 1, nothing.output
