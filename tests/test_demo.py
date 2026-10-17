"""`sshfleet demo`: the real interface on a made-up fleet, with a stand-in for ssh.

These run the web app and the stand-in as the demo does, a separate process per ssh call, so
what is checked is what someone trying the demo would see.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from typer.testing import CliRunner

from sshfleet.cli.main import app as cli
from sshfleet.collector.parse import parse_collect
from sshfleet.demo import fleet
from sshfleet.demo.run import prepare


@pytest.fixture
def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = prepare(tmp_path / "demo", "degraded")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
async def site(demo):
    from sshfleet.context import build_context
    from sshfleet.web import create_app

    app = create_app(build_context())
    async with app.router.lifespan_context(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        client.headers["x-sshfleet-token"] = app.state.web.token
        yield client, app.state.web


def _scenario(demo: dict[str, str], name: str) -> None:
    Path(demo["SSHFLEET_DEMO_SCENARIO"]).write_text(name)


def test_the_demo_home_is_a_complete_fleet_of_its_own(demo):
    from sshfleet.context import build_context

    context = build_context()
    assert context.warnings == []
    assert len(context.inventory.hosts()) == len(fleet.FLEET)
    assert {e.name: e.bastions for e in context.inventory.environments} == {
        "production": ["prod-bastion"], "staging": ["stg-bastion"],
    }
    assert str(context.paths.config_file).startswith(str(Path(demo["SSHFLEET_HOME"])))


def test_the_stand_in_answers_a_check_like_a_host(demo):
    from sshfleet.collector.remote import script
    from sshfleet.context import build_context

    context = build_context()
    host = context.inventory.host("prod-api")
    run = subprocess.run(
        [demo["SSHFLEET_SSH_BIN"], "-F", str(context.mux.ssh_config), "prod-api", "LC_ALL=C bash -s"],
        input=script("collect.sh"), text=True, capture_output=True, timeout=30, env=os.environ,
    )
    assert run.returncode == 0, run.stderr
    snapshot = parse_collect(run.stdout, host)
    assert snapshot.reachable and [c.name for c in snapshot.containers] == ["api"]


async def test_degraded_shows_alerts_and_a_dead_bastion_takes_its_hosts_with_it(site, demo):
    client, state = site
    await state.refresh()
    codes = {a["code"] for a in (await client.get("/api/fleet")).json()["alerts"]}
    assert {"DISK", "MEM", "STOPPED", "LOAD"} <= codes

    _scenario(demo, "bastion_down")
    await state.refresh()
    hosts = {h["alias"]: h for h in (await client.get("/api/fleet")).json()["hosts"]}
    behind = [h for alias, h in hosts.items() if alias.startswith("prod-") and alias != "prod-bastion"]
    assert behind and all(h["snapshot"]["error"]["kind"] == "bastion_down" for h in behind)
    assert hosts["stg-api"]["snapshot"]["reachable"], "staging has a bastion of its own"


async def test_a_search_finds_what_the_logs_say_with_the_lines_around_it(site):
    client, state = site
    await state.refresh()
    body = {
        "env": "production", "selection": {"main": True}, "pattern": "timeout|ECONNREFUSED",
        "window": "1h", "before": 2,
    }
    async with client.stream("POST", "/api/logs/search", json=body) as response:
        events = [json.loads(line) async for line in response.aiter_lines() if line.strip()]
    lines = [e for e in events if e["type"] == "line"]
    summary = events[-1]
    assert summary["type"] == "summary" and summary["matches"] > 0 and not summary["failed"]
    assert any(e["match"] for e in lines) and any(not e["match"] for e in lines), "matches and their context"
    # Payments is restarting in this scenario, and the default search reads running containers.
    assert {e["label"] for e in lines if e["match"]} == {"api", "orders"}


async def test_a_restart_is_verified_and_the_host_is_well_again(site):
    client, state = site
    await state.refresh()
    started = await client.post("/api/hosts/prod-payments/containers/payments/restart", json={"confirm": "payments"})
    assert started.status_code == 200, started.text
    job = started.json()["job"]
    for _ in range(100):
        status = (await client.get(f"/api/jobs/{job}")).json()
        if status["done"]:
            break
        await asyncio.sleep(0.1)
    assert status["result"]["ok"], status
    await state.refresh(alias="prod-payments")
    host = (await client.get("/api/hosts/prod-payments")).json()
    assert host["snapshot"]["alerts"] == [] or all(a["code"] == "RESTARTS" for a in host["snapshot"]["alerts"])


async def test_files_browse_and_preview_but_nothing_is_copied(site):
    client, _ = site
    assert (await client.get("/api/config")).json()["demo"] is True
    listing = (await client.get("/api/files/ls", params={"alias": "prod-api", "path": "/home/ubuntu/deploy"})).json()
    by_name = {e["name"]: e for e in listing["entries"]}
    assert by_name["compose.yaml"]["denied"] is None
    preview = await client.get("/api/files/preview", params={"alias": "prod-api", "path": "/home/ubuntu/deploy/compose.yaml"})
    assert "ghcr.io/acme/api" in preview.text

    secret = await client.get("/api/files/download", params={"alias": "prod-api", "path": "/home/ubuntu/deploy/.env.production"})
    assert secret.status_code == 403, "the deny list still answers first"
    plain = await client.get("/api/files/download", params={"alias": "prod-api", "path": "/home/ubuntu/deploy/compose.yaml"})
    assert plain.status_code == 409 and "made up" in plain.json()["detail"]
    upload = await client.post("/api/files/upload", data={"alias": "prod-api", "directory": "/tmp"}, files={"file": ("x.txt", b"x")})
    assert upload.status_code == 409


def test_following_a_container_keeps_printing_new_lines(demo):
    from sshfleet.context import build_context
    from sshfleet.logs import commands
    from sshfleet.logs.sources import container_source_for

    context = build_context()
    host = context.inventory.host("prod-api")
    script = commands.follow_pipeline(host, container_source_for(host, "api"), "", since="1m").script
    with pytest.raises(subprocess.TimeoutExpired) as ran:
        subprocess.run(
            [demo["SSHFLEET_SSH_BIN"], "-F", str(context.mux.ssh_config), "prod-api", "LC_ALL=C bash -s"],
            input=script, text=True, capture_output=True, timeout=5, env=os.environ,
        )
    printed = (ran.value.stdout or b"").decode() if isinstance(ran.value.stdout, bytes) else (ran.value.stdout or "")
    assert printed.count("\n") >= 5 and '"service":"api"' in printed


def test_the_command_says_what_it_is_and_refuses_an_unknown_scenario():
    runner = CliRunner()
    assert "made-up fleet" in runner.invoke(cli, ["demo", "--help"]).output
    assert runner.invoke(cli, ["demo", "--scenario", "on-fire"]).exit_code == 2
