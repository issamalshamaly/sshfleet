"""Bringing a version-2 config to version 3: services become properties of each host.

The conversion has to keep answering the same way forever, so it is tested against a whole file
rather than field by field, and the writing order is tested for what survives a crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sshfleet.config import ConfigError, migrate

FIXTURES = Path(__file__).parent / "fixtures"
V2 = FIXTURES / "config-v2-realistic.yaml"
V3 = FIXTURES / "config-v3-expected.yaml"

ALIASES = {
    "staging": [
        "st-bastion",
        "st-order-service",
        "st-wallet-service",
        "st-mobile-gateway",
        "st-admin-dashboard",
        "st-legacy",
        "st-unmatched",
    ],
    "production": ["prod-bastion", "prod-order", "prod-wallet", "prod-mobile-gateway", "prod-oddball"],
}


def test_the_whole_file_converts_to_the_expected_shape():
    """Compared in full, not field by field: a silent difference anywhere is a failure."""
    produced = migrate.migrate_v2_to_v3(yaml.safe_load(V2.read_text()), ALIASES)
    assert produced == yaml.safe_load(V3.read_text())


def test_every_kind_of_service_lands_where_it_should():
    """The same comparison, read out loud, so a future change says which rule it broke."""
    hosts = migrate.migrate_v2_to_v3(yaml.safe_load(V2.read_text()), ALIASES)["hosts"]

    # A starred service on an environment with several deploy envs becomes a prefix filter.
    assert hosts["st-order-service"]["workloads"] == "order-*"
    assert hosts["st-order-service"]["main"] is True
    assert hosts["st-order-service"]["label"] == "order"
    # ... and an exact name where the environment runs one.
    assert hosts["prod-order"]["workloads"] == "order-production"

    # The host override wins over the service, and is kept.
    assert hosts["st-order-service"]["display"] == "orders (staging)"
    assert hosts["st-order-service"]["expected_count"] == 3
    assert hosts["st-wallet-service"]["health"]["min_interval_s"] == 900
    assert hosts["prod-wallet"]["health"]["min_interval_s"] == 60  # from the service

    # podman without sudo, its host file with the deploy env expanded, its format.
    assert hosts["prod-wallet"]["runtime"] == "podman" and hosts["prod-wallet"]["sudo"] is False
    assert hosts["prod-wallet"]["log_sources"][0]["path"] == "/home/app/logs/ewallet/production/app.log"
    assert hosts["prod-wallet"]["log_format"] == "zap_console"

    # A glob service keeps its glob; a frontend keeps its web root and "none" runtime.
    assert hosts["st-mobile-gateway"]["workloads"] == "*gateway*"
    assert hosts["st-admin-dashboard"]["role"] == "frontend"
    assert hosts["st-admin-dashboard"]["web_root"] == "/var/www/html"

    # A host pinned to a service by hand follows that service.
    assert hosts["prod-oddball"]["label"] == "order" and hosts["prod-oddball"]["runtime"] == "docker"

    # A host no service matched is a plain service host, unstarred, unnamed.
    assert hosts["st-unmatched"] == {"role": "service"}

    # An override for a host that left the ssh config is carried over, minus the dead keys.
    assert hosts["gone-from-ssh"] == {"display": "retired"}


def test_nothing_fleet_wide_survives():
    produced = migrate.migrate_v2_to_v3(yaml.safe_load(V2.read_text()), ALIASES)
    assert "services" not in produced
    assert "main_services" not in produced
    assert "naming" not in produced
    assert all("ports_by_deploy_env" not in env for env in produced["environments"])
    # Settings that have nothing to do with services are untouched.
    assert produced["refresh"] == {"fast_s": 60}
    assert produced["logs"]["display_tz"] == "Asia/Baghdad"
    assert produced["probes"]["discovered_path"] == "/health/live"


def test_the_conversion_is_pure():
    """It must not touch what it was given, or a failed write would leave a mangled config."""
    raw = yaml.safe_load(V2.read_text())
    before = yaml.safe_dump(raw)
    migrate.migrate_v2_to_v3(raw, ALIASES)
    assert yaml.safe_dump(raw) == before


# ------------------------------------------------------------------------ writing


def test_the_write_never_leaves_the_config_path_empty(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(V2.read_text())
    original = path.read_text()

    note = migrate.upgrade(path)
    assert note is not None
    assert yaml.safe_load(path.read_text())["version"] == 3
    backup = path.with_suffix(".yaml.bak")
    assert backup.read_text() == original  # the previous file, whole
    assert backup.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".*upgrading"))  # no temp left behind


def test_an_existing_backup_is_rotated_not_overwritten(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(V2.read_text())
    backup = path.with_suffix(".yaml.bak")
    backup.write_text("version: 2\n# an older backup worth keeping\n")

    migrate.upgrade(path)
    assert "older backup worth keeping" in backup.with_suffix(".bak.1").read_text()
    assert yaml.safe_load(backup.read_text())["version"] == 2  # the file as it was a moment ago


def test_a_result_that_would_not_load_is_never_written(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(V2.read_text())
    before = path.read_text()

    def broken(raw, aliases):
        return {"version": 3, "environments": [{"short": "x"}]}  # no name: the loader refuses it

    monkeypatch.setattr(migrate, "migrate_v2_to_v3", broken)
    with pytest.raises(ConfigError):
        migrate.upgrade(path)
    assert path.read_text() == before  # untouched
    assert not path.with_suffix(".yaml.bak").exists()


def test_a_config_that_is_already_current_is_left_alone(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(V3.read_text())
    before = path.read_text()
    assert migrate.upgrade(path) is None
    assert path.read_text() == before


def test_the_same_sources_come_out_the_other_side():
    """Source ids change on purpose; what they point at must not.

    Version 2 named a source after the service it belonged to; version 3 names it after the host
    and the workload. Comparing ids would only prove they are different, so the comparison is on
    the host, the workload and the kind — which is what actually gets read.
    """
    from sshfleet.config import Config, HostOverride
    from sshfleet.inventory import Host
    from sshfleet.logs import sources as sources_module

    hosts = migrate.migrate_v2_to_v3(yaml.safe_load(V2.read_text()), ALIASES)["hosts"]

    class Snapshot:
        def __init__(self, names):
            from sshfleet.collector.model import ContainerInfo

            self.containers = [ContainerInfo(name=n, state="running") for n in names]
            self.nginx = ""

    running = {
        "st-order-service": ["order-staging", "order-rc"],
        "prod-order": ["order-production"],
        "st-wallet-service": ["ewallet-staging"],
        "prod-wallet": ["ewallet-production"],
        "st-mobile-gateway": ["gateway-1"],
        "prod-mobile-gateway": ["gateway-1"],
        "prod-oddball": ["order-production"],
    }

    config = Config(hosts={alias: HostOverride.from_dict(raw) for alias, raw in hosts.items()})
    seen: set[tuple[str, str, str]] = set()
    for alias, names in running.items():
        override = config.hosts[alias]
        host = Host(
            alias=alias,
            env="staging" if alias.startswith("st-") else "production",
            main=override.main,
            log_label=override.label or alias,
            workload_glob=override.workloads,
        )
        if not host.main:
            continue
        for source in sources_module.workload_sources(host, Snapshot(names), running_only=True):
            seen.add((source.host_alias, source.container, source.kind.value))

    # Every service version 2 called "main" still has its host and its workloads searched.
    assert ("prod-order", "order-production", "container_stdout") in seen
    assert ("st-order-service", "order-staging", "container_stdout") in seen
    assert ("st-order-service", "order-rc", "container_stdout") in seen  # the glob covers them all
    assert ("prod-wallet", "ewallet-production", "container_stdout") in seen
    assert ("st-mobile-gateway", "gateway-1", "container_stdout") in seen
    # And a host that was never a main service stays out of the default search.
    assert not any(alias == "st-legacy" for alias, _, _ in seen)
