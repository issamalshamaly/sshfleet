"""Shared CLI output helpers: a JSON envelope for scripting, rich tables for humans."""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

console = Console(stderr=False)
err_console = Console(stderr=True)


class Exit:
    """Exit codes shared by every command (documented in the README)."""

    OK = 0
    NO_RESULTS = 1
    USAGE = 2
    PARTIAL = 3
    FAILED = 4
    TRUNCATED = 6
    INTERRUPTED = 130


def emit_json(data: Any, *, ok: bool = True, errors: list[str] | None = None) -> None:
    payload = {"ok": ok, "data": data, "errors": errors or []}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def table(
    title: str,
    columns: list[str],
    rows: list[list[Any]],
    *,
    caption: str | None = None,
    wrap: set[str] | None = None,
) -> Table:
    """A compact table.

    Columns are clipped rather than wrapped by default: a fleet table that wraps every alias
    over four lines is unreadable in an 80-column terminal. Name the columns that may wrap.
    """
    rendered = Table(title=title, caption=caption, header_style="bold", title_justify="left", expand=False, pad_edge=False)
    wrap = wrap or set()
    for column in columns:
        rendered.add_column(column, overflow="ellipsis" if column not in wrap else "fold", no_wrap=column not in wrap)
    for row in rows:
        rendered.add_row(*["" if cell is None else cell if hasattr(cell, "__rich_console__") else str(cell) for cell in row])
    return rendered


def warn(message: str) -> None:
    err_console.print(f"[yellow]warning[/yellow] {message}")


def fail(message: str) -> None:
    err_console.print(f"[red]error[/red] {message}")


def info(message: str) -> None:
    err_console.print(message)


def env_style(color: str) -> str:
    return color if color in {"red", "yellow", "green", "cyan", "magenta", "blue", "white"} else "cyan"
