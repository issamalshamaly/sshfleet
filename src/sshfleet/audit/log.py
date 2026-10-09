"""Append-only local record of every mutating action.

One JSON object per line, 0600. Restart, upload, download, tunnel changes and shell opens are
recorded with the exact command, so "what did I touch on production today" is answerable.
"""

from __future__ import annotations

import getpass
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AuditLog:
    path: Path

    def append(self, action: str, **fields: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "actor": os.environ.get("USER") or _safe_user(),
            "action": action,
        }
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.chmod(self.path, 0o600)
        return entry

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.page(limit)[0]

    def page(self, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
        """The last ``limit`` entries, and how many there are in total.

        The total is what lets a reader know whether "load more" has anything left to give,
        without a second request or a guess at the file's size.
        """
        if not self.path.exists():
            return [], 0
        lines = [line for line in self.path.read_text(errors="replace").splitlines() if line.strip()]
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out, len(lines)


def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry
        return "unknown"
