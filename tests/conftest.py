"""Test fixtures: isolated XDG dirs and a fake ``ssh`` on PATH."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from sshfleet.config import Config, EnvironmentCfg, HostOverride, ProjectCfg
from sshfleet.config import loader as config_loader
from sshfleet.context import build_context
from sshfleet.paths import Paths

TESTS = Path(__file__).parent
FAKE_BIN = TESTS / "fakes" / "bin"
FIXTURES = TESTS / "fixtures"
SSH_CONFIGS = TESTS / "sshconfigs"


@pytest.fixture
def ops_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "ops"
    monkeypatch.setenv("SSHFLEET_HOME", str(home))
    return home


@pytest.fixture
def paths(ops_home: Path) -> Paths:
    return Paths.detect(ops_home).ensure()


@dataclass
class FakeSsh:
    """Handle on the replaying ssh shim: where it logs and what it was asked to do."""

    log: Path
    state: Path
    #: A per-test copy of the fixture tree — see the ``fake_ssh`` fixture for why.
    fixtures: Path

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def remote_commands(self, alias: str | None = None) -> list[str]:
        return [
            call["remote"]
            for call in self.calls()
            if call.get("remote") and (alias is None or call["alias"] == alias)
        ]

    def write_fixture(self, alias: str, name: str, body: str, scenario: str = "healthy") -> Path:
        """Hand the shim an answer for this test. It lands in the copy, never in the checkout."""
        directory = self.fixtures / scenario / alias
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(body)
        return path


@pytest.fixture
def fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSsh:
    """Put the replaying ``ssh`` shim on PATH and point it at a copy of the fixtures.

    A copy, because a test that needs the remote to say something particular writes a fixture for
    it first. Written straight into ``tests/fixtures`` those answers stayed behind, so a checkout
    was dirty after every run and whichever test ran last decided what was in the tree.
    """
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    log = tmp_path / "ssh.log"
    state = tmp_path / "ssh-state.json"
    monkeypatch.setenv("SSHFLEET_SSH_BIN", str(FAKE_BIN / "ssh"))
    monkeypatch.setenv("SSHFLEET_FAKE_FIXTURES", str(fixtures))
    monkeypatch.setenv("SSHFLEET_FAKE_LOG", str(log))
    monkeypatch.setenv("SSHFLEET_FAKE_STATE", str(state))
    monkeypatch.setenv("SSHFLEET_FAKE_SCENARIO", "healthy")
    monkeypatch.setenv("PATH", f"{FAKE_BIN}{os.pathsep}{os.environ['PATH']}")
    return FakeSsh(log=log, state=state, fixtures=fixtures)


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch):
    def choose(name: str) -> None:
        monkeypatch.setenv("SSHFLEET_FAKE_SCENARIO", name)

    return choose


#: What the suite knows about the sample environment's hosts. The tool discovers the rest.
SAMPLE_HOSTS: dict[str, HostOverride] = {
    "tst-order": HostOverride(
        main=True, label="order", role="service", workloads="order-*",
        health={"port": 3001, "path": "/"}, log_format="pino_pretty",
        log_sources=[{"path": "/usr/src/app/logs/access.log", "format": "pino_json", "in_container": True}],
    ),
    "tst-auth": HostOverride(
        main=True, label="auth", role="service", workloads="auth-*",
        health={"port": 3001, "path": "/health/live"}, log_format="pino_pretty",
    ),
    "tst-wallet": HostOverride(
        main=True, label="wallet", role="service", workloads="ewallet-*", runtime="podman", sudo=False,
        health={"port": 8080, "path": "/health", "min_interval_s": 60}, log_format="zap_console",
        log_sources=[{"path": "/home/ec2-user/logs/ewallet/production/app.log", "format": "zap_json", "rotated_glob": "app-*.log"}],
    ),
    "tst-kong-mobile": HostOverride(
        main=True, label="kong-m", role="gateway", workloads="*kong*", health={"port": 8001, "path": "/status"}
    ),
    "tst-admin-dashboard": HostOverride(label="dashboard", role="frontend", runtime="none", web_root="/var/www/html"),
    "tst-jenkins": HostOverride(label="jenkins", role="ops"),
}


@pytest.fixture
def sample_env_file(tmp_path: Path) -> Path:
    """A small two-bastion SSH config shaped like the real ones."""
    target = tmp_path / "test-env"
    shutil.copy(SSH_CONFIGS / "sample-env", target)
    return target


@pytest.fixture
def configured(paths: Paths, sample_env_file: Path, tmp_path: Path) -> Path:
    """Write a config.yaml registering the sample environment."""
    key = tmp_path / "test.pem"
    key.write_text("not-a-real-key\n")
    key.chmod(0o600)
    config = Config(
        projects=[ProjectCfg(name="lab")],
        environments=[
            EnvironmentCfg(
                name="testing",
                ssh_config_path=sample_env_file,
                short="test",
                color="cyan",
                key_path=key,
                project="lab",
            )
        ],
    )
    config.hosts = dict(SAMPLE_HOSTS)
    config_loader.save(config, paths.config_file)
    return paths.config_file


@pytest.fixture
def context(configured: Path, fake_ssh: FakeSsh):
    return build_context()


@pytest.fixture
async def web(context, fake_ssh: FakeSsh):
    """The whole web app in-process, with the session token already on the client."""
    from httpx import ASGITransport, AsyncClient

    from sshfleet.web import create_app

    app = create_app(context)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            client.headers["x-sshfleet-token"] = app.state.web.token
            yield client, app.state.web
