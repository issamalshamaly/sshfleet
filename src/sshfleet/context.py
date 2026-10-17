"""The object graph shared by the CLI and the web interface.

Building a Context parses the config and the SSH config files and writes the generated
``ssh_config``; it opens no connection, so it is cheap enough for ``--help``-style commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .audit import AuditLog
from .config import Config, ConfigError, migrate
from .config import loader as config_loader
from .history import History, StateRecord, TrackedFile, record_now
from .inventory import Inventory, build_inventory
from .paths import Paths
from .ssh import MuxManager, SshRunner


@dataclass(slots=True)
class Context:
    paths: Paths
    config: Config
    inventory: Inventory
    mux: MuxManager
    audit: AuditLog
    warnings: list[str] = field(default_factory=list)
    #: The values in ``secrets.env``, for whatever needs a token by name. Kept here, never in
    #: the process environment, which every ssh and every terminal session inherits.
    secrets: dict[str, str] = field(default_factory=dict)
    _runner: SshRunner | None = None

    def remember(self, reason: str, *, source: str = "cli") -> StateRecord | None:
        """Keep the configuration as it is on disk now in history (see history.py); nothing is
        written when it has not changed. A history that cannot be written is noted in the
        warnings, and the change it follows stands."""
        try:
            state, _ = record_now(self.config, self.paths, reason=reason, source=source)
            return state
        except Exception as exc:
            self.warnings.append(f"history: could not record the configuration — {exc}")
            return None

    @property
    def runner(self) -> SshRunner:
        """Created lazily: it allocates asyncio primitives, so it needs a running loop."""
        if self._runner is None:
            self._runner = SshRunner(self.mux, self.inventory, self.config.concurrency)
        return self._runner

    @property
    def download_dir(self) -> Path:
        configured = self.config.files.download_dir
        if configured and configured != "auto":
            return Path(configured).expanduser()
        return self.paths.download_dir

    def reload(self) -> Context:
        return build_context(self.paths, write_ssh_config=True)

    def load_secrets(self) -> int:
        """Read ``secrets.env`` into :attr:`secrets`. Refuses a world-readable file.

        It used to go into ``os.environ``, so a line such as ``LD_PRELOAD=…`` or
        ``SSHFLEET_SSH_BIN=…`` reached every ssh and every shell opened from the page.
        """
        path = self.paths.secrets_file
        if not path.exists():
            return 0
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            self.warnings.append(f"{path} is mode {mode:04o}; run `chmod 600 {path}` — not loaded")
            return 0
        loaded = 0
        for line in path.read_text(errors="replace").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, _, value = text.partition("=")
            self.secrets[key.strip()] = value.strip().strip("'\"")
            loaded += 1
        return loaded


def build_context(paths: Paths | None = None, *, write_ssh_config: bool = True) -> Context:
    resolved = paths or Paths.detect()
    warnings: list[str] = []
    resolved.ensure()

    # An older file is brought to the current layout once, atomically, keeping a backup — and the
    # file as it was is kept in history first, so the upgrade can be looked at and gone back from.
    if migrate.needs_upgrade(resolved.config_file):
        try:
            History(resolved.history_dir).record(
                [TrackedFile("config", resolved.config_file)], reason="before upgrading config.yaml", source="disk"
            )
        except Exception as exc:
            warnings.append(f"history: could not keep config.yaml before upgrading it — {exc}")
    note = migrate.upgrade(resolved.config_file)
    if note:
        warnings.append(note)
    config = config_loader.load(resolved.config_file)

    try:
        inventory = build_inventory(config)
        warnings.extend(inventory.warnings)
    except (FileNotFoundError, ConfigError) as exc:
        warnings.append(str(exc))
        inventory = Inventory()

    mux = MuxManager(config, resolved, inventory)
    if write_ssh_config and config.environments:
        mux.write_ssh_config()

    context = Context(
        paths=resolved,
        config=config,
        inventory=inventory,
        mux=mux,
        audit=AuditLog(resolved.audit_file),
        warnings=warnings,
    )
    context.load_secrets()
    # Hand edits are kept too, for whoever only ever uses the CLI: what is on disk now is recorded
    # unless the latest state already holds exactly that.
    first = not resolved.history_dir.exists()
    context.remember("first record" if first else "found on disk, changed outside sshfleet", source="disk")
    return context
