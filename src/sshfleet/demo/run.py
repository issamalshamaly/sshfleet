"""``sshfleet demo``: the real interface on the made-up fleet in :mod:`sshfleet.demo.fleet`.

Everything lives in a temporary directory that is removed on exit, so the operator's own
configuration, SSH files and audit trail are never read or written. The one thing swapped out is
``ssh`` itself: :mod:`sshfleet.demo.fake_ssh` answers in its place.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import fleet

#: Set while a demo runs; the web server reads it to say so on every page.
DEMO_ENV = "SSHFLEET_DEMO"


def prepare(root: Path, scenario: str) -> dict[str, str]:
    """Lay out a demo home under ``root``; returns the environment that points sshfleet at it."""
    from ..config import Config, EnvironmentCfg, HostOverride, ProjectCfg
    from ..config import loader as config_loader
    from ..paths import Paths

    ssh_dir = root / "ssh"
    (ssh_dir / "config.d").mkdir(parents=True, mode=0o700)
    environments = []
    for env in fleet.ENVIRONMENTS:
        path = ssh_dir / "config.d" / env["file"]
        path.write_text(fleet.ssh_config(env["name"]))
        environments.append(
            EnvironmentCfg(name=env["name"], ssh_config_path=path, short=env["short"], color=env["color"], project=fleet.PROJECT)
        )

    paths = Paths.detect(root / "home").ensure()
    config = Config(
        projects=[ProjectCfg(name=fleet.PROJECT, color="violet", description="A made-up online shop")],
        environments=environments,
    )
    config.hosts = {machine.alias: HostOverride.from_dict(machine.record) for machine in fleet.FLEET}
    config.refresh.fast_s = 20  # a demo should move while someone watches it
    config_loader.save(config, paths.config_file)

    bin_dir = root / "bin"
    bin_dir.mkdir(mode=0o700)
    ssh = bin_dir / "ssh"
    # The interpreter running this demo, so the fake finds sshfleet without anything on PATH.
    ssh.write_text(f"#!{sys.executable}\nimport sys\nfrom sshfleet.demo.fake_ssh import main\nsys.exit(main())\n")
    ssh.chmod(0o700)
    scenario_file = root / "scenario"
    scenario_file.write_text(scenario)

    return {
        "SSHFLEET_HOME": str(root / "home"),
        "SSHFLEET_SSH_DIR": str(ssh_dir),
        "SSHFLEET_SSH_BIN": str(ssh),
        # Copies are refused before either is called; these only make sure nothing real runs.
        "SSHFLEET_SCP_BIN": "/bin/false",
        "SSHFLEET_RSYNC_BIN": "/bin/false",
        "SSHFLEET_DEMO_SCENARIO": str(scenario_file),
        "SSHFLEET_DEMO_STATE": str(root / "state.json"),
        DEMO_ENV: "1",
    }


def _tell_the_story(scenario_file: Path, stop: threading.Event) -> None:
    """Walk through the scenarios on a loop, so the fleet changes while it is watched."""
    while not stop.is_set():
        for name, seconds in fleet.STORY:
            scenario_file.write_text(name)
            if stop.wait(seconds):
                return


def run_demo(*, scenario: str = "degraded", story: bool = False, port: int = 8766, open_browser: bool = True) -> None:
    from ..web import run_web

    root = Path(tempfile.mkdtemp(prefix="sshfleet-demo-"))
    stop = threading.Event()
    try:
        os.environ.update(prepare(root, scenario))
        if story:
            threading.Thread(target=_tell_the_story, args=(root / "scenario", stop), daemon=True).start()
        what = "walking through healthy, degraded and a bastion down" if story else f"scenario: {scenario}"
        print(f"sshfleet demo: a made-up fleet ({what}). Nothing here reaches a real server; Ctrl+C ends it.")
        run_web(port=port, open_browser=open_browser)
    finally:
        stop.set()
        time.sleep(0.05)
        shutil.rmtree(root, ignore_errors=True)
