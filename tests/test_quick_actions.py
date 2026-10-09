"""Quick actions: what is offered for a container, and what each one would run.

The browser names an action and never sends a command, so these check both halves of that
contract — the catalogue only offers what fits the container in front of it, and an id from
outside the catalogue buys nothing.
"""

from __future__ import annotations

import pytest
from helpers import make_config

from sshfleet.containers.quick import BY_ID, UnknownAction, command_for, offers
from sshfleet.containers.runtime import ALLOWED_VERBS, FORBIDDEN_VERBS
from sshfleet.inventory import build_inventory


@pytest.fixture
def inventory():
    return build_inventory(make_config())


@pytest.fixture
def host(inventory):
    return inventory.host("tst-order")


def ids(host, name="order-production", **kwargs) -> list[str]:
    return [action["id"] for action in offers(host, name, **kwargs)]


# ------------------------------------------------------------------ what is offered


def test_a_running_container_can_be_entered(host):
    assert "shell" in ids(host)
    assert "shell-root" in ids(host)


def test_a_stopped_container_offers_only_what_works_on_one(host):
    """No shell into something that is not running — the runtime would refuse anyway.

    Its log, what the runtime knows about it and the host's health endpoint all still answer,
    and all three are what you want when a container has stopped.
    """
    offered = ids(host, state="exited")
    assert offered == ["follow-logs", "inspect", "health"]
    assert not any(offer["command"].split()[2] == "exec" for offer in offers(host, "order-production", state="exited"))


def test_the_node_toolkit_follows_the_host_role(host):
    assert "node-toolkit" in ids(host)  # tst-order is a service
    host.role = "data"
    assert "node-toolkit" not in ids(host, image="postgres:16")
    # …unless the image says Node regardless of what the host is for.
    assert "node-toolkit" in ids(host, image="registry/api-node:20")


def test_the_image_decides_which_client_is_offered(host):
    assert "psql" in ids(host, image="postgres:16")
    assert "mongosh" in ids(host, image="bitnami/mongodb:7")
    assert "redis-cli" in ids(host, image="redis:7-alpine")
    assert "psql" not in ids(host, image="redis:7-alpine")


def test_the_health_endpoint_is_only_offered_when_one_is_configured(host):
    host.health_port = None
    assert "health" not in ids(host)
    host.health_port, host.health_path = 3000, "/health"
    assert "health" in ids(host)


def test_a_name_the_runtime_would_refuse_is_offered_nothing(host):
    assert offers(host, "order; rm -rf /") == []


# ----------------------------------------------------------------- what they run


def test_the_shell_action_execs_into_the_container(host):
    _, command = command_for(host, "shell", "order-production")
    assert command.startswith("sudo -n docker exec -it order-production sh -c ")
    assert "exec bash" in command


def test_the_root_shell_asks_for_uid_zero(host):
    _, command = command_for(host, "shell-root", "order-production")
    assert "-u 0:0" in command


def test_the_node_toolkit_installs_without_touching_package_json(host):
    _, command = command_for(host, "node-toolkit", "order-production")
    assert "npm install --no-save ts-node typescript @types/node" in command
    assert "cd /usr/src/app" in command


def test_the_runtime_of_the_host_is_the_one_used(inventory):
    host = inventory.host("tst-order")
    host.runtime = "podman"
    host.sudo = False
    _, command = command_for(host, "shell", "order-production")
    assert command.startswith("podman exec -it ")


def test_an_action_outside_the_catalogue_is_refused(host):
    with pytest.raises(UnknownAction):
        command_for(host, "docker-rm", "order-production")


def test_a_container_name_is_checked_before_it_reaches_a_shell(host):
    for name in ("order; reboot", "-it", "", "a" * 200):
        with pytest.raises(UnknownAction):
            command_for(host, "shell", name)


def test_no_action_can_take_a_service_down(host):
    """The catalogue is small enough to check by hand, and this is what it is for."""
    for action_id in BY_ID:
        _, command = command_for(host, action_id, "order-production")
        words = command.replace("sudo -n ", "").split()
        assert not any(word in FORBIDDEN_VERBS for word in words[:2]), command
        if words[0] in {"docker", "podman"}:
            assert words[1] in ALLOWED_VERBS, command
