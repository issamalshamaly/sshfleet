"""Port forwards and the open-a-shell helper."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from sshfleet import shell
from sshfleet.config import TunnelCfg
from sshfleet.tunnels import TunnelManager, free_port_near, port_in_use


@pytest.fixture
def manager(context, tmp_path: Path) -> TunnelManager:
    context.config.tunnels = {
        "kibana": TunnelCfg(
            name="kibana", env="testing", via="tst-bastion", remote="127.0.0.1:5601", local=15601,
            open_url="http://127.0.0.1:15601",
        )
    }
    return TunnelManager(context.config, context.mux, tmp_path / "tunnels.json")


def test_forward_spec_is_bound_to_localhost(manager):
    tunnel = manager.definition("kibana")
    assert manager.spec(tunnel) == "127.0.0.1:15601:127.0.0.1:5601"


def test_unknown_tunnel_lists_the_known_ones(manager):
    with pytest.raises(KeyError, match="kibana"):
        manager.definition("nope")


async def test_up_opens_a_forward_on_the_shared_connection(manager, fake_ssh):
    state = await manager.up("kibana")
    assert state.up
    assert state.endpoint == "127.0.0.1:15601"
    forwards = [c for c in fake_ssh.calls() if c.get("control") == "forward"]
    assert forwards, "expected ssh -O forward"
    assert forwards[-1]["alias"] == "tst-bastion"


async def test_state_survives_between_processes(manager, fake_ssh):
    await manager.up("kibana")
    assert manager.state_file.exists()
    reopened = TunnelManager(manager.config, manager.mux, manager.state_file)
    names = {s.name: s for s in reopened.status()}
    assert "kibana" in names


async def test_down_cancels_the_forward_but_not_the_connection(manager, fake_ssh):
    await manager.up("kibana")
    await manager.down("kibana")
    controls = [c.get("control") for c in fake_ssh.calls()]
    assert "cancel" in controls
    assert "exit" not in controls  # the master is shared; closing it would drop everything else


async def test_up_reports_a_failure_instead_of_raising(manager, scenario):
    scenario("bastion_down")
    state = await manager.up("kibana")
    assert state.up is False
    assert "tst-bastion" in state.error


async def test_ensure_raises_when_the_tunnel_cannot_open(manager, scenario):
    scenario("bastion_down")
    with pytest.raises(OSError, match="could not be opened"):
        await manager.ensure("kibana")


def test_free_port_near_skips_a_busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert port_in_use(port)
        assert free_port_near(port) != port


# --------------------------------------------------------------------- shell


def test_shell_command_reenables_prompts(tmp_path: Path):
    argv = shell.ssh_command(tmp_path / "cfg", "acme-prod-order")
    assert "BatchMode=no" in argv  # a passphrase prompt must still work
    assert argv[-1] == "acme-prod-order"


def test_konsole_plan_opens_a_tab(tmp_path: Path):
    plan = shell.plan(tmp_path / "cfg", "acme-prod-order", mode="konsole")
    assert plan.argv[:3] == ["konsole", "--new-tab", "-e"]


def test_tmux_plan_names_the_window_after_the_host(tmp_path: Path):
    plan = shell.plan(tmp_path / "cfg", "acme-prod-order", mode="tmux")
    assert plan.argv[:4] == ["tmux", "new-window", "-n", "acme-prod-order"]
    assert "ssh" in plan.argv[-1]


def test_mode_detection(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("KONSOLE_DBUS_SERVICE", raising=False)
    assert shell.detect_mode("auto") == "suspend"
    assert shell.detect_mode("auto", new_window=True) == "suspend"
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    assert shell.detect_mode("auto", new_window=True) == "tmux"
    assert shell.detect_mode("konsole") == "konsole"  # explicit preference wins


def test_remote_command_is_passed_through(tmp_path: Path):
    plan = shell.plan(tmp_path / "cfg", "host", mode="suspend", remote="uptime")
    assert plan.argv[-1] == "uptime"
