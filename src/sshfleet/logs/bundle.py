"""Write a search to disk: one file per service, a merged timeline, and a manifest.

This replaces the manual ``ssh … > x.txt`` then ``scp`` loop. The manifest records the window,
the pattern and the exact commands, so a bundle attached to a ticket explains itself.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import __version__
from ..fsutil import make_private_dirs, open_private
from .model import LogLine, SearchDone, SearchRequest, SourceDone, SourceError

SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def slug(text: str, limit: int = 40) -> str:
    cleaned = SLUG_RE.sub("-", text).strip("-")[:limit]
    return cleaned or "all"


def bundle_dir(base: Path, env: str, pattern: str, when: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when or time.time()))
    return base / f"{env}-{stamp}-{slug(pattern)}"


@dataclass(slots=True)
class Bundle:
    """Collects lines as they stream and writes the files when the search ends."""

    directory: Path
    request: SearchRequest
    env: str
    files: dict[str, Path] = field(default_factory=dict)
    _handles: dict[str, object] = field(default_factory=dict)
    _merged: list[LogLine] = field(default_factory=list)
    _keep_merged: bool = True
    merged_limit: int = 200_000

    def __post_init__(self) -> None:
        # A bundle is production log output: private, like the config and the audit log.
        make_private_dirs(self.directory)

    def _handle(self, label: str, source_id: str):
        handle = self._handles.get(source_id)
        if handle is None:
            name = f"{slug(label, 30)}.log"
            path = self.directory / name
            suffix = 2
            while path.exists() and str(path) not in {str(p) for p in self.files.values()}:
                path = self.directory / f"{slug(label, 30)}-{suffix}.log"
                suffix += 1
            self.files[source_id] = path
            handle = open_private(path)
            self._handles[source_id] = handle
        return handle

    def add(self, line: LogLine) -> None:
        self._handle(line.label, line.source_id).write(line.text + "\n")  # type: ignore[union-attr]
        if self._keep_merged:
            self._merged.append(line)
            if len(self._merged) > self.merged_limit:
                # Stop buffering; merged.log is rebuilt from the per-source files instead.
                self._keep_merged = False
                self._merged.clear()

    def _write_merged(self) -> Path | None:
        path = self.directory / "merged.log"
        if self._keep_merged:
            with open_private(path) as handle:
                for line in self._merged:
                    when = line.when
                    stamp = when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if when else "-" * 23
                    handle.write(f"{stamp} {line.label:<9} {line.text}\n")
            return path
        return None

    def close(self, summary: SearchDone) -> Path:
        for handle in self._handles.values():
            handle.close()  # type: ignore[union-attr]
        merged = self._write_merged()

        sources = []
        for source in self.request.sources:
            outcome = summary.per_source.get(source.id)
            entry: dict = {
                "id": source.id,
                "label": source.label,
                "kind": source.kind.value,
                "host": source.host_alias,
                "label_of": source.service,
                "container": source.container,
                "path": source.path,
                "file": self.files[source.id].name if source.id in self.files else None,
            }
            if isinstance(outcome, SourceDone):
                entry.update(
                    lines=outcome.lines,
                    matches=outcome.matches,
                    bytes=outcome.bytes,
                    elapsed_s=round(outcome.elapsed, 2),
                    truncated=outcome.truncated,
                    truncated_reason=outcome.truncated_reason,
                    coverage_ok=outcome.coverage_ok,
                    oldest=_iso(outcome.oldest_ts_ns),
                    newest=_iso(outcome.newest_ts_ns),
                    command=outcome.command,
                    exit_codes=outcome.exit_codes,
                )
            elif isinstance(outcome, SourceError):
                entry.update(error={"kind": outcome.kind, "message": outcome.message})
            sources.append(entry)

        manifest = {
            "tool": "sshfleet",
            "version": __version__,
            "created_at_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "env": self.env,
            "pattern": self.request.pattern,
            "mode": self.request.mode,
            "ignore_case": self.request.ignore_case,
            "before": self.request.before,
            "after": self.request.after,
            "window": {
                "since_utc": summary.window.since.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "until_utc": summary.window.until.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "spec": self.request.window.spec,
                "resolved_at_utc": summary.window.resolved_at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "display_tz": self.request.display_tz,
            },
            "caps": {
                "tail_lines": self.request.tail_lines,
                "max_bytes": self.request.max_bytes,
                "max_line_bytes": self.request.max_line_bytes,
                "max_file_bytes": self.request.max_file_bytes,
            },
            "totals": {"lines": summary.lines, "matches": summary.matches, "bytes": summary.bytes},
            "merged_file": merged.name if merged else None,
            "sources": sources,
            "errors": [
                {"source": s.source_id, "kind": s.kind, "message": s.message}
                for s in summary.per_source.values()
                if isinstance(s, SourceError)
            ],
        }
        path = self.directory / "manifest.json"
        with open_private(path) as handle:
            handle.write(json.dumps(manifest, indent=1, ensure_ascii=False))
        return self.directory


def _iso(ts_ns: int | None) -> str | None:
    if ts_ns is None:
        return None
    return datetime.fromtimestamp(ts_ns / 1_000_000_000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
