"""Filesystem locations used by sshfleet.

Everything is XDG-based and overridable with ``SSHFLEET_HOME`` (tests point it at a tmp dir).
The control-socket directory prefers ``$XDG_RUNTIME_DIR`` because ControlPath has a ~104 byte
limit on Linux; ``~/.cache`` is the fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "sshfleet"


def _env_dir(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


@dataclass(frozen=True, slots=True)
class Paths:
    config_dir: Path
    cache_dir: Path
    state_dir: Path
    control_dir: Path
    download_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def secrets_file(self) -> Path:
        return self.config_dir / "secrets.env"

    @property
    def ssh_config(self) -> Path:
        return self.config_dir / "ssh_config"

    @property
    def saved_searches(self) -> Path:
        return self.config_dir / "log_searches.yaml"

    @property
    def audit_file(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def ssh_backups(self) -> Path:
        """A copy of each SSH config file from before every edit sshfleet makes to it."""
        return self.state_dir / "ssh-backups"

    @property
    def history_dir(self) -> Path:
        """Every state the configuration has been in: config.yaml and the SSH files, together."""
        return self.state_dir / "history"

    @property
    def web_session_file(self) -> Path:
        """The web interface's session token and cookie value, see web/session.py."""
        return self.state_dir / "web-session.json"

    @property
    def tunnels_file(self) -> Path:
        return self.cache_dir / "tunnels.json"

    @property
    def fleet_state_dir(self) -> Path:
        return self.cache_dir / "state"

    def ensure(self) -> Paths:
        """Create every directory we own with private permissions."""
        for directory in (
            self.config_dir,
            self.cache_dir,
            self.state_dir,
            self.control_dir,
            self.fleet_state_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self

    @staticmethod
    def detect(home: Path | None = None) -> Paths:
        """Resolve paths from the environment (or from an explicit ``home`` for tests)."""
        override = home or (Path(os.environ["SSHFLEET_HOME"]).expanduser() if os.environ.get("SSHFLEET_HOME") else None)
        if override is not None:
            return Paths(
                config_dir=override / "config",
                cache_dir=override / "cache",
                state_dir=override / "state",
                control_dir=override / "mux",
                download_dir=override / "downloads",
            )

        xdg_config = _env_dir("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME
        xdg_cache = _env_dir("XDG_CACHE_HOME", Path.home() / ".cache") / APP_NAME
        xdg_state = _env_dir("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP_NAME

        runtime = os.environ.get("XDG_RUNTIME_DIR")
        control = Path(runtime) / APP_NAME if runtime and Path(runtime).is_dir() else xdg_cache / "mux"

        return Paths(
            config_dir=xdg_config,
            cache_dir=xdg_cache,
            state_dir=xdg_state,
            control_dir=control,
            download_dir=Path.home() / APP_NAME / "downloads",
        )
