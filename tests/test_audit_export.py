"""The audit trail: filtered on the server over all of it, and exported safely."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from sshfleet.audit.log import parse_since


def _seed(audit) -> None:
    audit.append("container.restart", env="testing", host="tst-order", target="order-1", status="ok")
    audit.append("container.restart", env="production", host="prd-order", target="order-1", status="failed")
    audit.append("file.download", env="testing", host="tst-auth", target="/var/log/x", status="ok")
    audit.append("settings.sounds", status="ok")  # fleet-wide: no environment
    audit.append("container.ignore", env="testing", host="tst-order", target="=HYPERLINK(\"http://evil\",\"x\")", status="ok")


async def test_audit_export_filters_on_the_server(web, context):
    client, _ = web
    _seed(context.audit)
    body = (await client.get("/api/audit", params={"action": "container.*", "envs": "testing", "limit": 1})).json()
    assert [e["action"] for e in body["entries"]] == ["container.ignore"]  # newest first, one asked for
    assert body["total"] == 2  # counted over the whole trail, not what was loaded
    assert {"file.download", "settings.sounds", "container.restart"} <= set(body["actions"])

    scoped = (await client.get("/api/audit", params={"envs": "testing", "limit": 100})).json()["entries"]
    assert "settings.sounds" in {e["action"] for e in scoped}  # fleet-wide entries stay in scope
    assert "production" not in {e.get("env") for e in scoped}

    exported = await client.get("/api/audit/export", params={"format": "jsonl", "envs": "testing", "action": "container.*"})
    assert exported.status_code == 200
    assert exported.headers["content-disposition"].startswith('attachment; filename="sshfleet-audit-')
    lines = [json.loads(line) for line in exported.text.splitlines()]
    assert [e["action"] for e in lines] == ["container.restart", "container.ignore"]  # oldest first
    assert context.audit.tail(1)[0]["action"] == "audit.export"
    assert (await client.get("/api/audit", params={"since": "yesterday-ish"})).status_code == 400


async def test_a_formula_in_a_container_name_is_neutralised(web, context):
    """Opened in a spreadsheet, a cell starting with = runs as a formula. Every such cell is
    written with a leading quote, so it shows as the text it is."""
    client, _ = web
    _seed(context.audit)
    response = await client.get("/api/audit/export", params={"format": "csv"})
    rows = list(csv.DictReader(io.StringIO(response.text)))
    target = next(r["target"] for r in rows if r["action"] == "container.ignore")
    assert target.startswith("'=HYPERLINK")
    assert all(not str(value).startswith(("=", "+", "@")) for row in rows for value in row.values())


def test_since_reads_the_ways_people_say_it():
    now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    assert parse_since("7d", now=now) == (now - timedelta(days=7)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert parse_since("30m", now=now).startswith("2026-10-16T11:30")
    assert parse_since("2026-10-01T09:00:00Z") == "2026-10-01T09:00:00.000Z"


def test_the_cli_keeps_its_audit_command_and_gains_an_export(configured, fake_ssh, context, tmp_path):
    from sshfleet.cli.main import app

    _seed(context.audit)
    runner = CliRunner()
    assert runner.invoke(app, ["audit", "-n", "3"]).exit_code == 0
    out = tmp_path / "trail.csv"
    result = runner.invoke(app, ["audit", "export", "--action", "container.*", "--env", "testing", "--out", str(out)])
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(out.open()))
    assert [r["action"] for r in rows] == ["container.restart", "container.ignore"]
    assert out.stat().st_mode & 0o777 == 0o600
    printed = runner.invoke(app, ["audit", "export", "--jsonl", "--since", "1d"])
    assert printed.exit_code == 0 and all(json.loads(line) for line in printed.output.splitlines())
    assert runner.invoke(app, ["audit", "export", "--since", "soon"]).exit_code == 2
