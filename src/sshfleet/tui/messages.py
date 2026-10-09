"""Messages workers post back to the UI.

Workers never touch widgets. They post one of these; the app routes it to the active screen,
which re-reads the fleet state. That is what keeps the interface responsive while 48 hosts
refresh in the background.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.message import Message

from ..collector import HostSnapshot
from ..fleet import RefreshProgress


class HostUpdated(Message):
    def __init__(self, snapshot: HostSnapshot) -> None:
        super().__init__()
        self.snapshot = snapshot


class RefreshProgressed(Message):
    def __init__(self, progress: RefreshProgress) -> None:
        super().__init__()
        self.progress = progress


class RefreshFinished(Message):
    def __init__(self, env: str | None) -> None:
        super().__init__()
        self.env = env


class MuxStateChanged(Message):
    def __init__(self, alive: int, total: int, bastions: dict[str, bool]) -> None:
        super().__init__()
        self.alive = alive
        self.total = total
        self.bastions = bastions


@dataclass
class AlertTransition:
    appeared: list
    cleared: list


class AlertsChanged(Message):
    def __init__(self, transition: AlertTransition) -> None:
        super().__init__()
        self.transition = transition
