"""HTTP and WebSocket routes."""

from . import containers, files, fleet, logs, misc, settings, terminal, tunnels

ROUTERS = [
    fleet.router,
    containers.router,
    files.router,
    tunnels.router,
    logs.router,
    settings.router,
    misc.router,
    terminal.router,
]

__all__ = ["ROUTERS"]
