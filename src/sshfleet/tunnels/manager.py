"""Port forwards over the existing master connections.

``ssh -O forward`` adds a forward to a connection that is already open, so opening the Kibana or
Seq tunnel costs nothing extra and closing it does not disturb anything else. The state file is
shared between the CLI and the TUI so both see the same tunnels.

A record in the state file is the operator's intent: it is written when a tunnel is opened and
removed only when it is closed. So a record whose port nobody holds is a tunnel that dropped by
itself, because the connection under it died, and that is what the web server's watcher reopens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import Config, TunnelCfg
from ..ssh import MuxManager, SshError

#: How long a closed forward may take to let go of its port before the close counts as failed.
RELEASE_WAIT_S = 1.0


@dataclass(slots=True)
class TunnelState:
    name: str
    env: str
    via: str
    local: int
    remote: str
    open_url: str | None
    up: bool
    since: float | None = None
    error: str = ""
    #: Opened and not closed since. With ``up`` false, the tunnel dropped by itself.
    wanted: bool = False
    #: Something listens on the local port although this tunnel is closed.
    held: bool = False

    @property
    def endpoint(self) -> str:
        return f"127.0.0.1:{self.local}"

    @property
    def dropped(self) -> bool:
        return self.wanted and not self.up

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "env": self.env,
            "via": self.via,
            "local": self.local,
            "remote": self.remote,
            "endpoint": self.endpoint,
            "url": self.open_url,
            "up": self.up,
            "wanted": self.wanted,
            "dropped": self.dropped,
            "held": self.held,
            "since": self.since,
            "error": self.error,
        }


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Whether something holds ``host:port``.

    It binds instead of connecting. A connection to an open tunnel goes all the way through the
    bastion to the database or Kibana behind it, and this runs on every status check.
    ``SO_REUSEADDR`` keeps sockets lingering in TIME_WAIT from counting as a listener.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def free_port_near(port: int, attempts: int = 20) -> int:
    for candidate in range(port, port + attempts):
        if not port_in_use(candidate):
            return candidate
    raise OSError(f"no free local port near {port}")


def port_holder(port: int) -> str:
    """Who listens on ``port``, as ``pid 123 (ssh: … [mux])``; empty when that cannot be told.

    Linux only, and only this user's processes are visible: ``/proc/net/tcp`` names the
    listening socket, and its owner is the process holding a descriptor to it.
    """
    targets: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        with contextlib.suppress(OSError, ValueError, IndexError):
            for line in Path(table).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:  # 0A = LISTEN
                    targets.add(f"socket:[{fields[9]}]")
    if not targets:
        return ""
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            for fd in (proc_dir / "fd").iterdir():
                with contextlib.suppress(OSError):
                    if os.readlink(fd) not in targets:
                        continue
                    command = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
                    return f"pid {proc_dir.name} ({command[:80]})" if command else f"pid {proc_dir.name}"
    return ""


async def released(port: int, wait: float = RELEASE_WAIT_S) -> bool:
    """Wait briefly for a port to be let go; the master frees it as it answers the cancel."""
    deadline = time.monotonic() + wait
    while port_in_use(port):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.1)
    return True


class TunnelManager:
    """Opens, closes and remembers SSH port forwards."""

    def __init__(self, config: Config, mux: MuxManager, state_file: Path) -> None:
        self.config = config
        self.mux = mux
        self.state_file = state_file
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, name: str) -> asyncio.Lock:
        """One open or close at a time per tunnel, so a reopen cannot race the operator's close."""
        return self._locks.setdefault(name, asyncio.Lock())

    # ------------------------------------------------------------- state file

    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, state: dict[str, dict]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_file.write_text(json.dumps(state, indent=1))

    # ----------------------------------------------------------------- config

    def definition(self, name: str) -> TunnelCfg:
        try:
            return self.config.tunnels[name]
        except KeyError:
            known = ", ".join(sorted(self.config.tunnels)) or "none configured"
            raise KeyError(f"unknown tunnel {name!r} (known: {known})") from None

    def spec(self, tunnel: TunnelCfg, local: int | None = None) -> str:
        return f"127.0.0.1:{local or tunnel.local}:{tunnel.remote_host}:{tunnel.remote_port}"

    # --------------------------------------------------------------- lifecycle

    async def up(self, name: str, *, local: int | None = None) -> TunnelState:
        async with self._lock(name):
            return await self._open(name, local)

    async def reopen(self, name: str) -> TunnelState | None:
        """Bring back a tunnel that dropped by itself; ``None`` if it was closed in the meantime."""
        async with self._lock(name):
            if not self._load().get(name, {}).get("up"):
                return None
            return await self._open(name, None)

    async def _open(self, name: str, local: int | None) -> TunnelState:
        tunnel = self.definition(name)
        state = self._load()
        recorded = state.get(name, {})
        wanted = bool(recorded.get("up"))
        port = local or int(recorded.get("local") or tunnel.local)

        if port_in_use(port):
            if wanted and recorded.get("local") == port:
                return self._state(tunnel, port, up=True, since=recorded.get("since"), wanted=True)
            port = free_port_near(port)

        warmed = await self.mux.warm(tunnel.via)
        if not warmed.ok:
            return self._state(tunnel, port, up=False, wanted=wanted, error=f"{tunnel.via}: {warmed.message}")

        try:
            await self.mux.forward(tunnel.via, self.spec(tunnel, port))
        except SshError as exc:
            return self._state(tunnel, port, up=False, wanted=wanted, error=exc.message)

        state[name] = {"local": port, "via": tunnel.via, "up": True, "since": time.time()}
        self._save(state)
        return self._state(tunnel, port, up=True, since=state[name]["since"], wanted=True)

    async def down(self, name: str) -> TunnelState:
        """Close a forward, and check that its port really was let go.

        The record goes either way: closing is the operator's call, and a record left behind
        would have the watcher reopen what they just closed. When something still listens on
        the port, the result says so and names it, instead of reporting a close that did not
        happen.
        """
        async with self._lock(name):
            tunnel = self.definition(name)
            state = self._load()
            port = int(state.get(name, {}).get("local") or tunnel.local)
            refused = ""
            try:
                await self.mux.cancel(tunnel.via, self.spec(tunnel, port))
            except SshError as exc:
                refused = exc.message
            state.pop(name, None)
            self._save(state)
            if await released(port):
                return self._state(tunnel, port, up=False)
            holder = port_holder(port)
            error = f"127.0.0.1:{port} is still open" + (f", held by {holder}" if holder else "")
            if refused:
                error += f" (ssh: {refused})"
            return self._state(tunnel, port, up=False, held=True, error=error)

    async def ensure(self, name: str) -> tuple[str, int]:
        """Open if needed and return ``(host, port)`` for an HTTP client."""
        state = await self.up(name)
        if not state.up:
            raise OSError(f"tunnel {name} could not be opened: {state.error}")
        return "127.0.0.1", state.local

    def status(self, env: str | None = None) -> list[TunnelState]:
        state = self._load()
        out: list[TunnelState] = []
        for name, tunnel in sorted(self.config.tunnels.items()):
            if env and tunnel.env != env:
                continue
            recorded = state.get(name, {})
            port = int(recorded.get("local") or tunnel.local)
            wanted = bool(recorded.get("up"))
            bound = port_in_use(port)
            out.append(
                self._state(
                    tunnel, port, up=wanted and bound, since=recorded.get("since"),
                    wanted=wanted, held=bound and not wanted,
                )
            )
        return out

    def status_of(self, name: str) -> TunnelState:
        """One tunnel's current state; raises KeyError when it is not configured."""
        for state in self.status():
            if state.name == name:
                return state
        raise KeyError(f"unknown tunnel {name!r}")

    async def down_all(self) -> list[str]:
        closed = []
        for state in self.status():
            if state.wanted:
                await self.down(state.name)
                closed.append(state.name)
        return closed

    def forget_via(self, aliases: set[str]) -> list[str]:
        """Drop the records of tunnels riding on connections that are being closed on purpose.

        Left in place, the watcher would take those tunnels for dropped and open them again.
        """
        state = self._load()
        names = sorted(name for name, tunnel in self.config.tunnels.items() if tunnel.via in aliases and name in state)
        for name in names:
            state.pop(name)
        if names:
            self._save(state)
        return names

    def _state(
        self,
        tunnel: TunnelCfg,
        local: int,
        *,
        up: bool,
        since: float | None = None,
        error: str = "",
        wanted: bool = False,
        held: bool = False,
    ) -> TunnelState:
        url = tunnel.open_url
        if url and local != tunnel.local:
            url = url.replace(f":{tunnel.local}", f":{local}")
        return TunnelState(
            name=tunnel.name,
            env=tunnel.env,
            via=tunnel.via,
            local=local,
            remote=tunnel.remote,
            open_url=url,
            up=up,
            since=since,
            error=error,
            wanted=wanted,
            held=held,
        )
