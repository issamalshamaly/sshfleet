"""Small builders shared by tests (importable because pytest puts tests/ on sys.path)."""

from __future__ import annotations

from pathlib import Path

from conftest import SAMPLE_HOSTS

from sshfleet.config import Config, EnvironmentCfg, ProjectCfg

SAMPLE_ENV = Path(__file__).parent / "sshconfigs" / "sample-env"


def make_config(name: str = "testing") -> Config:
    """A config registering the sample environment, with what the suite knows about its hosts."""
    config = Config(hosts=dict(SAMPLE_HOSTS))
    config.projects = [ProjectCfg(name="lab")]
    config.environments = [
        EnvironmentCfg(
            name=name,
            ssh_config_path=SAMPLE_ENV,
            short=name[:4],
            project="lab",
        )
    ]
    return config
