"""Every state the configuration has been in: list them, compare them, put one back.

A restore is checked before anything is written, records the state it replaces first (so it can
be undone the same way), and is confirmed by typing the state's six-character code — the server
checks that too, not just the page.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...history import (
    HistoryError,
    RestoreRefused,
    StateRecord,
    compare,
    plan_restore,
    tracked_files,
    with_content,
)
from ..security import require_token
from ..state import WebState

router = APIRouter(prefix="/api/settings/history", dependencies=[Depends(require_token)])

#: A diff past this is cut, with a note: the page shows it, nobody reads more than that in a browser.
MAX_DIFF_CHARS = 200_000


def _state(request: Request) -> WebState:
    return request.app.state.web


def _payload(record: StateRecord) -> dict[str, Any]:
    data = record.to_dict()
    for entry in data["files"]:
        entry["kept"] = entry.get("sha256") is not None
        entry.pop("sha256", None)
    return data


def _diffs(diffs: list) -> list[dict[str, Any]]:
    out = []
    for diff in diffs:
        item = asdict(diff)
        if len(item["diff"]) > MAX_DIFF_CHARS:
            item["diff"] = item["diff"][:MAX_DIFF_CHARS]
            item["note"] = "cut short: the difference is too long to show here"
        out.append(item)
    return out


def _get(state: WebState, state_id: str) -> StateRecord:
    try:
        return state.history.get(state_id)
    except HistoryError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("")
async def history_list(request: Request, limit: int = 50, before: str | None = None):
    state = _state(request)
    records, total = await asyncio.to_thread(state.history.list, max(1, min(limit, 500)), before)
    return {"states": [_payload(r) for r in records], "total": total, "busy": state.config_busy}


@router.get("/{state_id}")
async def history_show(request: Request, state_id: str, against: str = "previous"):
    """``against=previous``: what this state changed. ``against=current``: what restoring it would
    write now, and what it would leave alone."""
    if against not in {"previous", "current"}:
        raise HTTPException(400, "against must be previous or current")
    state = _state(request)
    history = state.history
    record = _get(state, state_id)
    previous = history.previous(record.id)
    out: dict[str, Any] = {"state": _payload(record), "against": against, "previous": previous.id if previous else None}
    if against == "previous":
        older = with_content(history, previous) if previous else []
        out["diffs"] = _diffs(await asyncio.to_thread(compare, older, with_content(history, record)))
        return out
    files_now = tracked_files(state.context.config, state.context.paths)
    try:
        plan = await asyncio.to_thread(plan_restore, history, record.id, files_now)
    except RestoreRefused as exc:
        out.update(diffs=[], restore={"refused": str(exc)})
        return out
    summary = plan.to_dict()
    out["diffs"] = _diffs(plan.diffs)
    summary.pop("diffs")
    out["restore"] = summary
    return out


class RestoreBody(BaseModel):
    confirm: str
    files: list[str] | None = None


@router.post("/{state_id}/restore")
async def history_restore(request: Request, state_id: str, body: RestoreBody):
    state = _state(request)
    record = _get(state, state_id)
    if body.confirm.strip() != record.code:
        raise HTTPException(400, f"type {record.code} to confirm putting this state back")
    if state.config_busy:
        raise HTTPException(409, "another restore or import is writing the configuration; try again in a moment")
    try:
        return await state.restore(record.id, only=set(body.files) if body.files else None)
    except RestoreRefused as exc:
        raise HTTPException(400, str(exc)) from exc
    except HistoryError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OSError as exc:
        # Some files may be written. The state from before the restore was recorded first, so
        # putting that one back undoes whatever part of this got through.
        raise HTTPException(500, f"restore stopped while writing: {exc}. Restore the state recorded just before it to undo.") from exc
