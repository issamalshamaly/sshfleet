"""HTTP and WebSocket routes."""

from . import containers, files, fleet, history, logs, misc, settings, share, terminal, tunnels

ROUTERS = [
    fleet.router,
    containers.router,
    files.router,
    files.downloads,
    tunnels.router,
    logs.router,
    logs.downloads,
    settings.router,
    history.router,
    share.router,
    misc.router,
    terminal.router,
]

__all__ = ["ROUTERS"]
