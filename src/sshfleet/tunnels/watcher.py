"""Notice tunnels that dropped by themselves, say so, and open them again.

A tunnel is a listener inside the master connection to its ``via`` host. When that connection
goes (the network drops, the laptop sleeps, the bastion reboots) the listener goes with it and
nothing on this machine notices. The web server runs this loop so that something does.

A tunnel the operator closed is never touched: closing removes its record, and only a tunnel
that still has one while nothing listens on its port counts as dropped.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .manager import TunnelManager, TunnelState

CHECK_INTERVAL_S = 5.0

#: Waits before each further reopen attempt. Quick at first, since a blip is the usual cause,
#: then one attempt a minute so a long outage costs little.
BACKOFF_S = (5.0, 15.0, 30.0, 60.0)


@dataclass(slots=True)
class Retry:
    attempts: int = 0
    next_at: float = 0.0
    error: str = ""


class TunnelWatcher:
    def __init__(
        self,
        manager: TunnelManager,
        publish: Callable[[dict[str, Any]], None],
        audit: Callable[..., Any],
    ) -> None:
        self.manager = manager
        self.publish = publish
        self.audit = audit
        self.retries: dict[str, Retry] = {}

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - never let the watcher die
                self.publish({"type": "error", "message": f"tunnel watch failed: {exc}"})
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def tick(self) -> None:
        states = {state.name: state for state in self.manager.status()}

        # A tunnel that is no longer dropped came back some other way or was closed by hand;
        # either way the browser can stop showing it as reconnecting.
        for name in [n for n in self.retries if not (n in states and states[n].dropped)]:
            del self.retries[name]
            state = states.get(name)
            self._announce("reopened" if state and state.up else "closed", state, name)

        now = time.time()
        due: list[str] = []
        for name, state in states.items():
            if not state.dropped:
                continue
            if name not in self.retries:
                self.retries[name] = Retry(next_at=now)
                self._announce("dropped", state, name)
                self.audit(
                    "tunnel.dropped", env=state.env, host=state.via, target=name,
                    status="failed", note=f"{state.endpoint} stopped listening",
                )
            if self.retries[name].next_at <= now:
                due.append(name)
        if due:
            await asyncio.gather(*(self._reopen(name) for name in due))

    async def _reopen(self, name: str) -> None:
        try:
            result = await self.manager.reopen(name)
        except Exception as exc:
            # No free port, or the definition went while the attempt waited: an attempt that
            # failed, which must wait its turn like any other rather than retry every tick.
            if name in self.retries:
                self._failed(name, str(exc), None)
            return
        retry = self.retries.get(name)
        if result is None or retry is None:
            return  # closed by hand while the attempt waited its turn; the next tick says so
        if result.up:
            del self.retries[name]
            self._announce("reopened", result, name)
            self.audit(
                "tunnel.reopen", env=result.env, host=result.via, target=name,
                status="ok", note=f"{result.endpoint}, after {retry.attempts + 1} attempt(s)",
            )
            return
        self._failed(name, result.error, result)

    def _failed(self, name: str, error: str, state: TunnelState | None) -> None:
        retry = self.retries[name]
        retry.attempts += 1
        retry.error = error
        retry.next_at = time.time() + BACKOFF_S[min(retry.attempts, len(BACKOFF_S)) - 1]
        self._announce("retrying", state, name)

    def describe(self, state: TunnelState) -> dict[str, Any]:
        """A tunnel as the browser sees it: its state plus where reopening it has got to."""
        payload = state.to_dict()
        retry = self.retries.get(state.name) if state.dropped else None
        payload["attempts"] = retry.attempts if retry else 0
        payload["retry_at"] = retry.next_at if retry and retry.attempts else None
        if retry and retry.error:
            payload["error"] = retry.error
        return payload

    def _announce(self, what: str, state: TunnelState | None, name: str) -> None:
        self.publish(
            {"type": "tunnel", "state": what, "name": name, "tunnel": self.describe(state) if state else None}
        )
