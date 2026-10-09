"""Merging several sources, streaming results, and writing a bundle."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import make_config

from sshfleet.logs import SearchCoordinator
from sshfleet.logs import sources as sources_module
from sshfleet.logs.bundle import Bundle, bundle_dir, slug
from sshfleet.logs.merge import TimelineMerger
from sshfleet.logs.model import (
    LogFormat,
    LogLine,
    LogSource,
    SearchDone,
    SearchRequest,
    SourceDone,
    SourceError,
    SourceKind,
    TimeWindow,
)

FIXTURES = Path(__file__).parent / "fixtures" / "logs"
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def line(source: str, ts_ns: int, text: str = "x") -> LogLine:
    return LogLine(source_id=source, label=source, seq=0, text=text, ts_ns=ts_ns)


# ------------------------------------------------------------------- merge


def test_lines_are_released_in_timestamp_order():
    merger = TimelineMerger()
    merger.register("a")
    merger.register("b")

    merger.push(line("a", 300))
    merger.push(line("b", 100))
    merger.push(line("b", 200))
    released = merger.drain()
    # "a" has only reached 300 and "b" only 200, so everything up to 200 is safe.
    assert [item.ts_ns for item in released] == [100, 200]

    merger.finish("b")
    merger.finish("a")
    assert [item.ts_ns for item in merger.flush()] == [300]


def test_nothing_is_released_while_a_source_is_silent():
    """A source that has not spoken yet may still produce an older line."""
    merger = TimelineMerger()
    merger.register("a")
    merger.register("b")
    merger.push(line("a", 100))
    assert merger.drain() == []
    assert "b" in merger.waiting_on

    merger.finish("b")
    assert [item.ts_ns for item in merger.drain()] == [100]


def test_a_failed_source_does_not_block_the_merge():
    merger = TimelineMerger()
    merger.register("a")
    merger.register("b")
    merger.push(line("a", 100))
    merger.finish("b")  # errored out
    assert [item.ts_ns for item in merger.drain()] == [100]


def test_ties_are_stable_by_source_order():
    merger = TimelineMerger()
    merger.register("first")
    merger.register("second")
    merger.push(line("second", 100, "b"))
    merger.push(line("first", 100, "a"))
    merger.finish("first")
    merger.finish("second")
    assert [item.text for item in merger.flush()] == ["a", "b"]


def test_untimed_lines_are_kept_separate_not_interleaved():
    merger = TimelineMerger()
    merger.register("a")
    merger.push(line("a", 100, "timed"))
    merger.push(LogLine("a", "a", 0, "no timestamp", None))
    merger.finish("a")
    flushed = merger.flush()
    assert [item.text for item in flushed] == ["timed", "no timestamp"]


def test_out_of_order_lines_within_a_source_are_counted():
    merger = TimelineMerger()
    merger.register("a")
    merger.push(line("a", 200))
    merger.push(line("a", 100))
    assert merger.out_of_order == 1


# ---------------------------------------------------------------- sources


@pytest.fixture
def context_config():
    return make_config()


class _Snapshot:
    """Just enough of a snapshot for the source builders: what ``ps -a`` reported."""

    def __init__(self, names, running=None, nginx=""):
        from sshfleet.collector.model import ContainerInfo

        alive = set(names if running is None else running)
        self.containers = [ContainerInfo(name=n, state="running" if n in alive else "exited") for n in names]
        self.nginx = nginx


def test_the_star_puts_running_workload_stdout_into_the_default_search(context_config):
    from sshfleet.inventory import build_inventory

    inventory = build_inventory(context_config)
    snapshots = {
        "tst-order": _Snapshot(["order-production", "order-rc"], running=["order-production"]),
        "tst-auth": _Snapshot(["auth-production"]),
        "tst-wallet": _Snapshot(["ewallet-production"]),
        "tst-kong-mobile": _Snapshot(["kong-gateway"]),
    }
    found = sources_module.main_sources(inventory, "testing", snapshots.get)

    assert [(s.host_alias, s.container) for s in found] == [
        ("tst-order", "order-production"),
        ("tst-auth", "auth-production"),
        ("tst-wallet", "ewallet-production"),
        ("tst-kong-mobile", "kong-gateway"),
    ]
    assert all(s.kind is SourceKind.CONTAINER_STDOUT for s in found)
    assert [s.label for s in found] == ["order", "auth", "wallet", "kong-m"]
    # The stopped one is not in the default search...
    assert "order-rc" not in [s.container for s in found]
    # ...but it is still readable, because its last lines are why it stopped.
    picker = sources_module.host_sources(inventory.host("tst-order"), snapshots["tst-order"])
    assert "order-rc" in [s.container for s in picker]


def test_a_host_offers_its_files_its_nginx_and_its_journal_too(context_config):
    from sshfleet.inventory import build_inventory

    inventory = build_inventory(context_config)
    wallet = sources_module.host_sources(inventory.host("tst-wallet"), _Snapshot(["ewallet-production"]))
    host_file = next(s for s in wallet if s.kind is SourceKind.HOST_FILE)
    assert host_file.path == "/home/ec2-user/logs/ewallet/production/app.log"
    assert host_file.log_format is LogFormat.ZAP_JSON
    assert any(s.kind is SourceKind.JOURNAL for s in wallet)

    # A file the application keeps inside its own image is read in the watched container.
    order = sources_module.host_sources(inventory.host("tst-order"), _Snapshot(["order-production"]))
    inside = next(s for s in order if s.kind is SourceKind.CONTAINER_FILE)
    assert inside.path == "/usr/src/app/logs/access.log" and inside.container == "order-production"

    # A gateway host offers nginx as well, because of its role.
    gateway = sources_module.host_sources(inventory.host("tst-kong-mobile"), _Snapshot([]))
    assert sum(1 for s in gateway if "nginx" in s.id) == 2


def test_labels_tell_a_host_apart_from_its_own_workloads(context_config):
    from sshfleet.inventory import build_inventory

    host = build_inventory(context_config).host("tst-order")
    assert sources_module.label_for(host) == "order"
    # One workload needs no suffix; several get a stable one whatever order ps returned them in.
    assert sources_module.label_for(host, "order-production", ["order-production"]) == "order"
    names = ["order-production", "order-rc", "order-development"]
    forward = [sources_module.label_for(host, n, names) for n in names]
    backward = [sources_module.label_for(host, n, list(reversed(names))) for n in names]
    assert forward == backward
    assert len(set(forward)) == 3
    assert forward[0].startswith("order:")


def test_labels_stay_unique_when_the_names_do_not_differ(context_config):
    from sshfleet.inventory import build_inventory

    host = build_inventory(context_config).host("tst-order")
    names = ["order-a-1", "order-a-2"]
    labels = [sources_module.label_for(host, n, names) for n in names]
    assert len(set(labels)) == 2





# ------------------------------------------------------------- end to end


def _fixture_source(alias: str, label: str, fixture: str) -> LogSource:
    return LogSource(
        id=f"testing/{label}", kind=SourceKind.CONTAINER_STDOUT, env="testing", label=label,
        host_alias=alias, service=label, container=f"{label}-production", log_format=LogFormat.PINO_PRETTY,
    )


async def test_search_streams_lines_and_a_summary(context, fake_ssh):
    """The fake ssh replays a recorded pipeline result for each host."""
    fake_ssh.write_fixture("tst-order", "logsearch.txt", FIXTURES.joinpath("grep_context.txt").read_text())
    source = _fixture_source("tst-order", "order", "grep_context.txt")
    request = SearchRequest(sources=(source,), pattern="WARN|ERROR", window=TimeWindow("2h"))

    coordinator = SearchCoordinator(context.runner, context.inventory, context.config)
    lines_out, summary = [], None
    async for event in coordinator.run(request, now=NOW):
        if isinstance(event, LogLine):
            lines_out.append(event)
        elif isinstance(event, SearchDone):
            summary = event

    assert summary is not None
    assert lines_out, "expected lines from the replayed fixture"
    # grep -n prefixes are stripped, and matches are distinguished from context.
    assert not any(item.text.startswith(("12-", "13:", "40:")) for item in lines_out)
    assert any(item.is_match for item in lines_out)
    assert any(not item.is_match for item in lines_out)
    assert summary.matches == 2


async def test_two_services_are_merged_into_one_timeline(context, fake_ssh):
    fake_ssh.write_fixture(
        "tst-order", "logsearch.txt",
        "2026-09-13T11:00:01.000000000Z [2026-09-13 11:00:01.000 +0000] INFO: order one\n"
        "2026-09-13T11:00:03.000000000Z [2026-09-13 11:00:03.000 +0000] INFO: order two\n",
    )
    fake_ssh.write_fixture(
        "tst-auth", "logsearch.txt",
        "2026-09-13T11:00:02.000000000Z [2026-09-13 11:00:02.000 +0000] INFO: auth one\n",
    )
    sources = (_fixture_source("tst-order", "order", ""), _fixture_source("tst-auth", "auth", ""))
    request = SearchRequest(sources=sources, pattern="", window=TimeWindow("2h"))

    coordinator = SearchCoordinator(context.runner, context.inventory, context.config)
    ordered = [e for e in [event async for event in coordinator.run(request, now=NOW)] if isinstance(e, LogLine)]
    assert [item.label for item in ordered] == ["order", "auth", "order"]
    assert [item.ts_ns for item in ordered] == sorted(item.ts_ns for item in ordered)


async def test_a_match_inside_an_entry_keeps_its_header_attached(context, fake_ssh):
    """grep marks the line the pattern was on, which in a pino entry is the indented body.

    The whole reason this tool exists is that ``-B 60`` is needed by hand. The header must stay
    reachable from the match, or a reader is handed a fragment with no context: "Matches only"
    in the browser keeps an entry when any line of it matched, and that is what it keys on.
    """
    fake_ssh.write_fixture(
        "tst-order", "logsearch.txt",
        # The shape grep -n -B 60 really emits: "N-" on context, "N:" on the match.
        "11-2026-09-13T11:00:00.000000000Z [2026-09-13 11:00:00.000 +0000] INFO: unrelated\n"
        "12-2026-09-13T11:00:01.000000000Z [2026-09-13 11:00:01.000 +0000] WARN: Grant Failed\n"
        "13-2026-09-13T11:00:01.000000000Z     error: {\n"
        "14:2026-09-13T11:00:01.000000000Z       \"type\": \"TRANSACTION_NOT_FOUND\"\n"
        "15-2026-09-13T11:00:01.000000000Z     }\n",
    )
    source = _fixture_source("tst-order", "order", "")
    request = SearchRequest(sources=(source,), pattern="TRANSACTION_NOT_FOUND", window=TimeWindow("2h"))

    coordinator = SearchCoordinator(context.runner, context.inventory, context.config)
    out = [e async for e in coordinator.run(request, now=NOW) if isinstance(e, LogLine)]

    # The line numbers are grep's, not the log's, and never reach the reader.
    assert not any(item.text.startswith(("11-", "12-", "13-", "14:", "15-")) for item in out), out

    matched = [item for item in out if item.is_match]
    assert [item.text.strip() for item in matched] == ['"type": "TRANSACTION_NOT_FOUND"']
    assert matched[0].is_continuation, "the match is the indented body, not the header"

    # ...and the header is part of the same entry, so it travels with it.
    entry = matched[0].entry_id
    together = [item.text for item in out if item.entry_id == entry]
    assert any("Grant Failed" in text for text in together), together
    assert not any("unrelated" in text for text in together), together


async def test_lines_outside_the_window_are_dropped(context, fake_ssh):
    """The producer is not trusted to have cut the window; the offset is only known here.

    Several formats have no remote time filter at all, and the ones that do compare timestamps
    as text without reading the offset they carry. So the cut is made on this side, where the
    line has been parsed.
    """
    fake_ssh.write_fixture(
        "tst-order", "logsearch.txt",
        "2026-09-13T09:30:00.000000000Z [2026-09-13 09:30:00.000 +0000] INFO: before the window\n"
        "2026-09-13T11:00:00.000000000Z [2026-09-13 11:00:00.000 +0000] INFO: inside the window\n"
        "2026-09-13T13:30:00.000000000Z [2026-09-13 13:30:00.000 +0000] INFO: after the window\n",
    )
    source = _fixture_source("tst-order", "order", "")
    request = SearchRequest(sources=(source,), pattern="", window=TimeWindow("2h"))

    coordinator = SearchCoordinator(context.runner, context.inventory, context.config)
    texts = [e.text async for e in coordinator.run(request, now=NOW) if isinstance(e, LogLine)]
    assert [t.rsplit("INFO: ", 1)[-1] for t in texts] == ["inside the window"]


async def test_an_entry_is_kept_or_dropped_whole(context, fake_ssh):
    """A continuation carries its header's timestamp, so the body never outlives its header."""
    fake_ssh.write_fixture(
        "tst-order", "logsearch.txt",
        "2026-09-13T09:30:00.000000000Z [2026-09-13 09:30:00.000 +0000] WARN: too old\n"
        "2026-09-13T09:30:00.000000000Z     orderId: \"stale\"\n"
        "2026-09-13T11:00:00.000000000Z [2026-09-13 11:00:00.000 +0000] WARN: in range\n"
        "2026-09-13T11:00:00.000000000Z     orderId: \"fresh\"\n",
    )
    source = _fixture_source("tst-order", "order", "")
    request = SearchRequest(sources=(source,), pattern="", window=TimeWindow("2h"))

    coordinator = SearchCoordinator(context.runner, context.inventory, context.config)
    texts = [e.text async for e in coordinator.run(request, now=NOW) if isinstance(e, LogLine)]
    assert not any("stale" in t or "too old" in t for t in texts), texts
    assert any("fresh" in t for t in texts) and any("in range" in t for t in texts), texts


async def test_a_raw_dump_keeps_every_line_it_is_given(context, fake_ssh):
    """--raw-dump reproduces the manual tail+grep, which has no window to enforce."""
    fake_ssh.write_fixture(
        "tst-order", "logsearch.txt",
        "2026-09-13T09:30:00.000000000Z [2026-09-13 09:30:00.000 +0000] INFO: long before\n",
    )
    source = _fixture_source("tst-order", "order", "")
    request = SearchRequest(sources=(source,), pattern="", window=TimeWindow("2h"), raw_dump=True)

    coordinator = SearchCoordinator(context.runner, context.inventory, context.config)
    texts = [e.text async for e in coordinator.run(request, now=NOW) if isinstance(e, LogLine)]
    assert any("long before" in t for t in texts), texts


async def test_a_missing_container_is_reported_per_source(context, fake_ssh):
    fake_ssh.write_fixture("tst-order", "logsearch.txt", "")
    fake_ssh.write_fixture("tst-auth", "error.txt", "ssh: connect to host failed: Connection refused")
    fake_ssh.write_fixture("tst-auth", "error.code", "255")
    sources = (_fixture_source("tst-order", "order", ""), _fixture_source("tst-auth", "auth", ""))
    request = SearchRequest(sources=sources, pattern="x", window=TimeWindow("2h"))

    summary = None
    async for event in SearchCoordinator(context.runner, context.inventory, context.config).run(request, now=NOW):
        if isinstance(event, SearchDone):
            summary = event
    assert summary is not None
    assert "auth" in summary.failed_sources
    assert "order" not in summary.failed_sources
    (FIXTURES.parent / "healthy" / "tst-auth" / "error.txt").unlink(missing_ok=True)
    (FIXTURES.parent / "healthy" / "tst-auth" / "error.code").unlink(missing_ok=True)


# ------------------------------------------------------------------ bundle


def test_bundle_writes_one_file_per_service_plus_a_manifest(tmp_path: Path):
    sources = (_fixture_source("tst-order", "order", ""), _fixture_source("tst-auth", "auth", ""))
    request = SearchRequest(sources=sources, pattern="TRANSACTION_NOT_FOUND", window=TimeWindow("1h"))
    bundle = Bundle(directory=tmp_path / "b", request=request, env="production")

    bundle.add(LogLine("testing/order", "order", 0, "order line", 1_000_000_000))
    bundle.add(LogLine("testing/auth", "auth", 0, "auth line", 2_000_000_000))

    window = TimeWindow("1h").resolve(NOW)
    summary = SearchDone(
        window=window, lines=2, matches=2, bytes=40, elapsed=1.5,
        per_source={
            "testing/order": SourceDone("testing/order", "order", 1, 20, 1, 1.0, "order-production"),
            "testing/auth": SourceError("testing/auth", "auth", "missing_container", "container not found"),
        },
    )
    directory = bundle.close(summary)

    assert (directory / "order.log").read_text() == "order line\n"
    assert (directory / "auth.log").read_text() == "auth line\n"
    merged = (directory / "merged.log").read_text().splitlines()
    assert len(merged) == 2
    assert "order" in merged[0] and "auth" in merged[1]  # ordered by timestamp

    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["pattern"] == "TRANSACTION_NOT_FOUND"
    assert manifest["totals"] == {"lines": 2, "matches": 2, "bytes": 40}
    assert manifest["window"]["since_utc"].endswith("Z")
    assert manifest["errors"][0]["kind"] == "missing_container"
    labels = {s["label"]: s for s in manifest["sources"]}
    assert labels["order"]["file"] == "order.log"


def test_bundle_folder_name_describes_the_search(tmp_path: Path):
    directory = bundle_dir(tmp_path, "production", "TRANSACTION_NOT_FOUND")
    assert directory.name.startswith("production-")
    assert directory.name.endswith("-TRANSACTION-NOT-FOUND")


def test_slug_handles_an_empty_pattern():
    assert slug("") == "all"
    assert slug("error|fatal") == "error-fatal"
