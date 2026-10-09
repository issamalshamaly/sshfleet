"""Turning parsed SSH stanzas into the host inventory."""

from __future__ import annotations

from pathlib import Path

from helpers import make_config

from sshfleet.config import Config, EnvironmentCfg, HostOverride
from sshfleet.inventory import build_inventory

SAMPLE = Path(__file__).parent / "sshconfigs" / "sample-env"


def _config(**overrides) -> Config:
    config = make_config()
    config.environments = [
        EnvironmentCfg(
            name="testing",
            ssh_config_path=SAMPLE,
            short="test",
        )
    ]
    return config


def test_bastions_are_the_proxyjump_targets():
    inventory = build_inventory(_config())
    environment = inventory.environment("testing")
    assert environment.bastions == ["tst-bastion", "tst-jenkins-proxy"]
    assert inventory.host("tst-bastion").is_bastion is True
    assert inventory.host("tst-order").is_bastion is False


def test_every_host_knows_which_connection_carries_it():
    inventory = build_inventory(_config())
    assert inventory.host("tst-order").bastion == "tst-bastion"
    assert inventory.host("tst-bastion").bastion == "tst-bastion"  # itself
    assert {h.alias for h in inventory.behind("tst-bastion")} == {
        "tst-order",
        "tst-auth",
        "tst-wallet",
        "tst-admin-dashboard",
    }


def test_what_a_host_is_comes_from_its_own_record():
    """No alias matching any more: a host is what the operator wrote about it, or a bare default."""
    inventory = build_inventory(_config())
    order = inventory.host("tst-order")
    assert order.log_label == "order" and order.main is True and order.role == "service"
    assert (order.health_port, order.health_path) == (3001, "/")
    assert order.workload_glob == "order-*"

    # A host nobody described is still a host: a plain service box with a derived label.
    plain = inventory.host("tst-jenkins-proxy")
    assert plain.role == "service" and plain.main is False
    assert plain.log_label == "jenkins-"[:8]  # the alias with the environment prefix stripped
    assert plain.workload_glob is None and plain.health_port is None


def test_the_workload_filter_is_the_only_thing_said_about_containers():
    inventory = build_inventory(_config())
    order = inventory.host("tst-order")
    assert order.watches("order-production") and order.watches("order-rc")
    assert not order.watches("redis") and not order.watches("node-exporter")
    assert order.visible(["redis", "order-rc", "order-a"]) == ["order-a", "order-rc"]

    # No filter means every workload the host reports.
    plain = inventory.host("tst-bastion")
    assert plain.watches("anything at all")


def test_a_host_can_be_podman_without_sudo():
    inventory = build_inventory(_config())
    wallet = inventory.host("tst-wallet")
    assert wallet.runtime == "podman" and wallet.sudo is False and wallet.user == "ec2-user"
    order = inventory.host("tst-order")
    assert order.runtime is None and order.sudo is True  # probed on the host, sudo assumed


def test_roles_fall_back_to_service():
    from sshfleet.config import HostRole

    inventory = build_inventory(_config())
    assert inventory.host("tst-admin-dashboard").role == HostRole.FRONTEND
    assert inventory.host("tst-kong-mobile").role == HostRole.GATEWAY
    assert inventory.host("tst-jenkins").role == HostRole.OPS
    assert HostRole.parse(None) is HostRole.SERVICE
    assert HostRole.parse("spaceship") is HostRole.SERVICE


def test_starred_hosts_are_listed_per_environment():
    inventory = build_inventory(_config())
    assert {h.alias for h in inventory.starred("testing")} == {
        "tst-order",
        "tst-auth",
        "tst-wallet",
        "tst-kong-mobile",
    }


def test_a_host_sets_how_often_its_endpoint_is_called():
    config = _config()
    config.hosts["tst-wallet"] = HostOverride(health={"port": 8080, "min_interval_s": 900})
    inventory = build_inventory(config)

    assert inventory.host("tst-wallet").probe_min_interval_s == 900
    assert inventory.host("tst-order").probe_min_interval_s == 0  # unset means every refresh
