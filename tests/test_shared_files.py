"""Files more than one sshfleet process writes, and files a crash must not leave half-written."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest

from sshfleet.config import Config, TunnelCfg
from sshfleet.config import loader as config_loader
from sshfleet.tunnels.manager import TunnelManager


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@dataclass
class Warmed:
    ok: bool = True
    message: str = ""


class Mux:
    """Opens forwards without ssh; the one named in ``hold`` waits until it is let go."""

    def __init__(self, hold: str | None = None) -> None:
        self.hold = hold
        self.release = asyncio.Event()

    async def warm(self, alias):
        return Warmed()

    async def forward(self, alias, spec):
        if self.hold and self.hold in spec:
            await self.release.wait()

    async def cancel(self, alias, spec):
        return None


async def test_two_processes_opening_tunnels_keep_both_records(tmp_path: Path):
    """The CLI and the web server share tunnels.json. Each used to read it before opening its
    tunnel and write it back whole after, so the slower one erased the other's record — and the
    watcher then never reopened that tunnel when it dropped."""
    a, b = _free_port(), _free_port()
    config = Config()
    config.tunnels = {
        "kibana": TunnelCfg("kibana", "testing", "tst-bastion", f"slow.internal:{a}", a),
        "seq": TunnelCfg("seq", "testing", "tst-bastion", f"fast.internal:{b}", b),
    }
    state_file = tmp_path / "tunnels.json"
    slow_mux = Mux(hold="slow.internal")
    web = TunnelManager(config, slow_mux, state_file)  # type: ignore[arg-type]
    cli = TunnelManager(config, Mux(), state_file)  # type: ignore[arg-type]

    opening = asyncio.create_task(web.up("kibana"))
    await asyncio.sleep(0.05)  # the web server is waiting on ssh with the file already read
    assert (await cli.up("seq")).up
    slow_mux.release.set()
    assert (await opening).up

    records = json.loads(state_file.read_text())
    assert sorted(records) == ["kibana", "seq"]
    assert state_file.stat().st_mode & 0o777 == 0o600


def test_a_save_that_fails_halfway_leaves_the_config_as_it_was(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.yaml"
    config = config_loader.parse_text("version: 3\nrefresh:\n  fast_s: 60\n")
    config_loader.save(config, path)
    before = path.read_text()

    config.refresh.fast_s = 5
    real_replace = os.replace

    def crash(src, dst):
        if Path(dst) == path:
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="disk full"):
        config_loader.save(config, path)
    assert path.read_text() == before, "the old file, whole"
    assert not [p for p in tmp_path.iterdir() if "sshfleet-tmp" in p.name], "no temp left behind"
