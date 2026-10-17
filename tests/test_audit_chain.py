"""The audit trail as evidence: every line linked to the one before, kept within a size, and the
reads that matter recorded along with the changes."""

from __future__ import annotations

import json
import os
import pwd
import threading
from pathlib import Path

from typer.testing import CliRunner

from sshfleet.audit.log import GENESIS, AuditLog, link
from sshfleet.cli.main import app


def _lines(path: Path) -> list[bytes]:
    return [line for line in path.read_bytes().split(b"\n") if line]


def test_every_line_carries_the_link_to_the_one_before(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for n in range(3):
        log.append("container.restart", host=f"h{n}", status="ok")
    lines = _lines(log.path)
    assert json.loads(lines[0])["prev"] == GENESIS
    assert json.loads(lines[1])["prev"] == link(lines[0])
    assert json.loads(lines[2])["prev"] == link(lines[1])
    result = log.verify()
    assert result.intact and result.entries == 3 and result.head == link(lines[2])


def test_a_line_changed_removed_or_slipped_in_is_found(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for n in range(4):
        log.append("file.download", host=f"h{n}", status="ok")
    original = log.path.read_bytes()

    log.path.write_bytes(original.replace(b'"h1"', b'"elsewhere"'))
    changed = log.verify()
    assert not changed.intact and "line 3 does not follow" in changed.breaks[0]

    lines = original.split(b"\n")
    log.path.write_bytes(b"\n".join(lines[:1] + lines[2:]))
    removed = log.verify()
    assert len(removed.breaks) == 1 and "line 2 does not follow" in removed.breaks[0]

    forged = json.dumps({"ts": "2026-10-17T00:00:00.000Z", "action": "nothing.happened"}).encode()
    log.path.write_bytes(b"\n".join([*lines[:2], forged, *lines[2:]]))
    slipped = log.verify()
    assert any("line 3 carries no link" in b for b in slipped.breaks)
    assert any("line 4 does not follow" in b for b in slipped.breaks)


def test_lines_from_before_the_chain_are_counted_not_failed(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"ts": "2026-01-01T00:00:00.000Z", "action": "ssh.open"}\n' * 2)
    log = AuditLog(path)
    log.append("ssh.open", status="ok")
    result = log.verify()
    assert result.intact and result.unlinked == 2 and result.entries == 3


def test_the_trail_is_kept_within_a_size_and_the_chain_runs_on(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=1000, keep=2)
    for n in range(40):
        log.append("file.list", host="h", target=f"/srv/{n}", status="ok")
    assert [p.name for p in log.files()] == ["audit.2.jsonl", "audit.1.jsonl", "audit.jsonl"]
    assert not log.rotated(3).exists(), "only `keep` files are kept besides the current one"
    assert all(p.stat().st_size <= 1000 for p in log.files())
    assert log.verify().intact, "the chain carries on from one file into the next"

    kept = [e["target"] for e in log.iter_entries()]
    assert kept == [f"/srv/{n}" for n in range(40 - len(kept), 40)], "oldest first, nothing out of order"
    newest, total = log.page(5)
    assert total == len(kept) and [e["target"] for e in newest] == kept[-5:]


def test_two_writers_at_once_keep_one_chain(tmp_path: Path):
    """The CLI and the web server write to the same file."""
    path = tmp_path / "audit.jsonl"

    def write(name: str) -> None:
        log = AuditLog(path)
        for n in range(60):
            log.append("logs.search", target=f"{name}-{n}", status="started")

    writers = [threading.Thread(target=write, args=(name,)) for name in ("cli", "web")]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    result = AuditLog(path).verify()
    assert result.intact and result.entries == 120


def test_a_write_cut_short_does_not_swallow_the_next_line(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("ssh.open", status="ok")
    with log.path.open("ab") as handle:
        handle.write(b'{"ts": "2026-10-17T')  # the process died mid-line
    log.append("ssh.open", host="after", status="ok")
    assert [e.get("host") for e in log.iter_entries()] == [None, "after"]


def test_the_actor_is_the_account_not_what_the_environment_says(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USER", "someone-else")
    entry = AuditLog(tmp_path / "audit.jsonl").append("ssh.open", status="ok")
    assert entry["actor"] == pwd.getpwuid(os.getuid()).pw_name
    assert entry["pid"] == os.getpid()


def test_audit_verify_says_intact_or_where_it_broke(context):
    runner = CliRunner()
    for n in range(3):
        context.audit.append("container.restart", host=f"h{n}", status="ok")
    intact = runner.invoke(app, ["audit", "verify"])
    assert intact.exit_code == 0, intact.output
    assert "intact" in intact.output and "last link" in intact.output

    path = context.audit.path
    path.write_bytes(path.read_bytes().replace(b'"h1"', b'"h9"'))
    broken = runner.invoke(app, ["audit", "verify", "--json"])
    assert broken.exit_code == 4
    assert json.loads(broken.output)["ok"] is False


async def test_what_was_looked_at_is_recorded_too(web, context, fake_ssh):
    client, _ = web
    await client.get("/api/files/ls", params={"alias": "tst-order", "path": "/home/ubuntu"})
    denied = await client.get("/api/files/preview", params={"alias": "tst-order", "path": "/home/ubuntu/order.env.production"})
    assert denied.status_code == 403
    body = {
        "env": "testing",
        "selection": {"containers": [{"host": "tst-order", "name": "order-production"}]},
        "pattern": "boom",
        "window": "2026-09-13T10:00:00Z..2026-09-13T12:00:00Z",
    }
    async with client.stream("POST", "/api/logs/search", json=body) as response:
        [line async for line in response.aiter_lines()]

    entries = list(context.audit.iter_entries())
    assert any(e["action"] == "file.list" and e["target"] == "/home/ubuntu" for e in entries)
    assert any(e["action"] == "file.preview" and e["status"] == "denied" for e in entries)
    search = next(e for e in entries if e["action"] == "logs.search")
    assert search["target"] == "boom" and "tst-order" in search["note"]
