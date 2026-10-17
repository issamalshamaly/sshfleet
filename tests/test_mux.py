"""The generated ssh_config and the ControlMaster lifecycle."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sshfleet.ssh.mux import MuxManager


def _config_text(context) -> str:
    return context.mux.write_ssh_config().read_text()


def test_host_star_block_comes_first(context):
    text = _config_text(context)
    lines = [line for line in text.splitlines() if line.startswith("Host ")]
    assert lines[0] == "Host *"  # ssh keeps the first value per option


def test_multiplexing_options_are_present(context):
    text = _config_text(context)
    assert "ControlMaster auto" in text
    assert re.search(r"ControlPath \S+/%C", text)
    assert "ControlPersist 10m" in text
    assert "BatchMode yes" in text
    assert "Compression yes" in text  # log text compresses ~10x
    assert "StrictHostKeyChecking accept-new" in text


def test_environment_key_is_added_for_the_alias_pattern(context, tmp_path: Path):
    text = _config_text(context)
    assert "Host tst-*" in text
    assert "IdentityFile" in text
    # The operator's own file is included rather than copied.
    assert "Include " in text
    for env in context.config.environments:
        assert str(env.ssh_config_path) in text


def test_default_identities_are_kept_so_agent_or_id_keys_still_work(context, monkeypatch, tmp_path: Path):
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    (fake_home / ".ssh" / "id_ed25519").write_text("x")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    text = context.mux.render_config()
    identities = [line.strip() for line in text.splitlines() if line.strip().startswith("IdentityFile")]
    # Naming an IdentityFile replaces OpenSSH's defaults, so they must be restored explicitly.
    assert any("test.pem" in line for line in identities)
    assert any("id_ed25519" in line for line in identities)


def test_config_is_private_and_only_rewritten_when_changed(context):
    path = context.mux.write_ssh_config()
    assert path.stat().st_mode & 0o777 == 0o600
    first = path.stat().st_mtime_ns
    context.mux.write_ssh_config()
    assert path.stat().st_mtime_ns == first


def test_explicit_alias_list_when_there_is_no_common_prefix(paths, tmp_path: Path):
    from sshfleet.config import Config, EnvironmentCfg
    from sshfleet.inventory import build_inventory

    env_file = tmp_path / "mixed"
    env_file.write_text(
        "Host alpha\n    HostName 10.0.0.1\nHost beta\n    HostName 10.0.0.2\nHost gamma\n    HostName 10.0.0.3\n"
    )
    key = tmp_path / "k.pem"
    key.write_text("x")
    config = Config(environments=[EnvironmentCfg(name="mixed", ssh_config_path=env_file, key_path=key)])
    inventory = build_inventory(config)
    text = MuxManager(config, paths, inventory).render_config()
    assert "Host alpha beta gamma" in text


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs the OpenSSH client to parse the config")
def test_every_environment_file_is_read_whatever_came_before_it(paths, tmp_path: Path):
    """An Include inside a Host block is conditional in OpenSSH: the file is read only for hosts
    matching that block. An environment without a key followed the previous one's key block,
    so its hosts were never found ("Could not resolve hostname <alias>")."""
    from sshfleet.config import Config, EnvironmentCfg
    from sshfleet.inventory import build_inventory

    keyed = tmp_path / "keyed"
    keyed.write_text("Host acme-web\n    HostName 192.0.2.1\n")
    keyless = tmp_path / "keyless"
    keyless.write_text("Host other-api\n    HostName 192.0.2.2\n")
    key = tmp_path / "k.pem"
    key.write_text("x")
    config = Config(
        environments=[
            EnvironmentCfg(name="keyed", ssh_config_path=keyed, key_path=key),
            EnvironmentCfg(name="keyless", ssh_config_path=keyless),
        ]
    )
    mux = MuxManager(config, paths, build_inventory(config))
    path = mux.write_ssh_config()

    def resolved(alias: str) -> str:
        # -G only parses and prints the configuration; nothing connects.
        out = subprocess.run(["ssh", "-G", "-F", str(path), alias], capture_output=True, text=True, check=True).stdout
        return next(line.split()[1] for line in out.splitlines() if line.startswith("hostname "))

    assert resolved("acme-web") == "192.0.2.1"
    assert resolved("other-api") == "192.0.2.2"


async def test_warm_opens_a_master_and_check_sees_it(context, fake_ssh):
    result = await context.mux.warm("tst-bastion")
    assert result.ok, result.message
    assert "tst-bastion" in context.mux.alive
    assert await context.mux.check("tst-bastion") is True


async def test_warm_bastions_covers_every_proxyjump_target(context):
    results = await context.mux.warm_bastions("testing")
    assert set(results) == {"tst-bastion", "tst-jenkins-proxy"}
    assert all(r.ok for r in results.values())


async def test_unreachable_host_is_classified_not_raised(context, scenario):
    scenario("bastion_down")
    result = await context.mux.warm("tst-bastion")
    assert result.ok is False
    assert result.kind is not None
    assert "timed out" in result.message.lower()


async def test_reset_socket_only_touches_our_own_control_dir(context, monkeypatch, tmp_path: Path):
    outsider = tmp_path / "not-ours"
    outsider.mkdir()
    victim = outsider / "socket"
    victim.write_text("x")

    async def fake_control_path(alias: str):
        return victim

    monkeypatch.setattr(context.mux, "control_path", fake_control_path)
    assert await context.mux.reset_socket("tst-order") is False
    assert victim.exists()


@pytest.mark.parametrize("field", ["key_path", "ssh_config_path", "name"])
def test_a_value_with_a_line_break_never_adds_lines_to_the_generated_config(paths, tmp_path: Path, field):
    """The file is written line by line: a key path ending in "\\nProxyCommand …" would have
    become a ProxyCommand that ssh runs on this machine."""
    from sshfleet.config import Config, ConfigError, EnvironmentCfg
    from sshfleet.inventory import build_inventory

    env_file = tmp_path / "env"
    env_file.write_text("Host alpha\n    HostName 10.0.0.1\n")
    key = tmp_path / "k.pem"
    key.write_text("x")
    config = Config(environments=[EnvironmentCfg(name="plain", ssh_config_path=env_file, key_path=key)])
    inventory = build_inventory(config)  # read while the file name is still a real one
    environment = config.environments[0]
    hostile = f"{getattr(environment, field)}\nProxyCommand touch {tmp_path / 'ran'}"
    setattr(environment, field, hostile if field == "name" else Path(hostile))
    with pytest.raises(ConfigError, match="line break"):
        MuxManager(config, paths, inventory).render_config()
