"""What sshfleet leaves on this machine, and what it accepts from configuration, after the launch
review: private downloads and bundles, safe folder names, web-only tunnel links, no key read as
SSH configuration, and secrets.env kept out of every child process."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from sshfleet.config import ConfigError
from sshfleet.config.schema import TunnelCfg


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def test_a_download_and_the_folders_made_for_it_are_private(context, tmp_path: Path, monkeypatch):
    from sshfleet.files import download
    from sshfleet.ssh import argv as A

    destination = tmp_path / "downloads" / "testing" / "tst-order" / "now" / "app.log"
    monkeypatch.setattr(A, "rsync_argv", lambda _c, _s, dest, **_: ["sh", "-c", f"echo line > '{dest}'"])
    result = await download(context.runner, context.inventory.host("tst-order"), "/var/log/app.log", destination)
    assert result.ok, result.message
    assert _mode(destination) == 0o600
    for folder in (tmp_path / "downloads", destination.parent.parent.parent, destination.parent):
        assert _mode(folder) == 0o700, folder


def test_a_log_bundle_is_private(tmp_path: Path):
    from sshfleet.logs.bundle import Bundle
    from sshfleet.logs.model import SearchRequest

    bundle = Bundle(directory=tmp_path / "bundles" / "one", request=SearchRequest(sources=()), env="testing")
    handle = bundle._handle("order", "order-source")
    handle.write("a line\n")
    handle.close()
    assert _mode(tmp_path / "bundles" / "one") == 0o700
    assert all(_mode(path) == 0o600 for path in bundle.files.values())


def test_a_download_folder_never_leaves_the_downloads_directory(context, tmp_path: Path):
    from sshfleet.files import download_dir_for

    host = context.inventory.host("tst-order")
    host.env = "../../outside"
    target = download_dir_for(tmp_path, host)
    assert target.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("address", ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,x"])
def test_a_tunnel_link_must_be_a_web_address(address):
    with pytest.raises(ConfigError, match="http"):
        TunnelCfg.from_dict("kibana", {"env": "staging", "via": "bastion", "remote": "10.0.0.1:5601", "local": 15601, "open": address})
    assert TunnelCfg.from_dict(
        "kibana", {"env": "staging", "via": "bastion", "remote": "10.0.0.1:5601", "local": 15601, "open": "https://k.example/app"}
    ).open_url == "https://k.example/app"


async def test_the_settings_page_refuses_a_tunnel_link_that_is_not_a_web_address(web):
    client, _ = web
    body = {"name": "kibana", "env": "testing", "via": "tst-bastion", "remote_host": "10.0.0.1",
            "remote_port": 5601, "local": 15601, "open_url": "javascript:alert(1)"}
    response = await client.post("/api/settings/tunnels", json=body)
    assert response.status_code == 422


def test_a_file_holding_a_private_key_is_not_read_as_ssh_configuration(tmp_path: Path):
    from sshfleet.inventory import parse_ssh_config

    named_like_config = tmp_path / "production"
    named_like_config.write_text("-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaA==\n")  # assembled: check-secrets
    with pytest.raises(OSError, match="holds a private key"):
        parse_ssh_config(named_like_config)


def test_secrets_env_never_reaches_the_environment_children_inherit(paths, configured):
    """It went into os.environ, so LD_PRELOAD=… there reached every ssh and every shell."""
    from sshfleet.context import build_context

    paths.secrets_file.write_text("LD_PRELOAD=/tmp/evil.so\nTELEGRAM_TOKEN=abc123\n")
    os.chmod(paths.secrets_file, 0o600)
    before = dict(os.environ)
    context = build_context(paths)
    assert "LD_PRELOAD" not in os.environ and os.environ.get("TELEGRAM_TOKEN") is None
    assert dict(os.environ) == before
    assert context.secrets == {"LD_PRELOAD": "/tmp/evil.so", "TELEGRAM_TOKEN": "abc123"}
