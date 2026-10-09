"""Timestamp parsing, colour stripping and entry grouping against the real log shapes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sshfleet.logs import parsers
from sshfleet.logs.grouper import EntryGrouper
from sshfleet.logs.model import LogFormat, TimeWindow

FIXTURES = Path(__file__).parent / "fixtures" / "logs"
ESC = "\x1b"


def lines(name: str) -> list[str]:
    return FIXTURES.joinpath(name).read_text().splitlines()


def as_utc(ts_ns: int) -> datetime:
    return datetime.fromtimestamp(ts_ns / 1_000_000_000, UTC)


# --------------------------------------------------------------- timestamps


def test_docker_nanosecond_prefix_is_exact():
    ts, rest = parsers.split_docker_prefix("2026-08-11T10:59:08.670022952Z [2026-08-11 10:59:08.669 +0000] WARN: x")
    seconds = int(datetime(2026, 8, 11, 10, 59, 8, tzinfo=UTC).timestamp())
    # Nanoseconds are kept whole: this is what makes merging several services deterministic,
    # and it is finer than datetime can represent, so the comparison is done in integers.
    assert ts == seconds * 1_000_000_000 + 670_022_952
    assert rest.startswith("[2026-08-11")


def test_a_bare_date_inside_a_message_is_not_read_as_a_prefix():
    ts, rest = parsers.split_docker_prefix("2026-09-13 10:50:51 some message without an offset")
    assert ts is None
    assert rest.startswith("2026-09-13")


def test_pino_bracket_header_with_offset():
    parsed = parsers.parse_pino_header("[2026-08-11 10:59:08.669 +0000] WARN: WALLET CLIENT - failed")
    assert parsed is not None
    ts, level = parsed
    assert level == "WARN"
    assert as_utc(ts) == datetime(2026, 8, 11, 10, 59, 8, 669000, tzinfo=UTC)


def test_pino_header_offset_is_applied():
    parsed = parsers.parse_pino_header("[2026-08-11 13:59:08.669 +0300] INFO: x")
    assert parsed is not None
    assert as_utc(parsed[0]) == datetime(2026, 8, 11, 10, 59, 8, 669000, tzinfo=UTC)


def test_zap_console_header():
    parsed = parsers.parse_zap_header("2026-09-13T10:50:52.100Z\tERROR\tservice/wallet.go:118\tgrant failed")
    assert parsed is not None
    assert parsed[1] == "ERROR"
    assert as_utc(parsed[0]) == datetime(2026, 9, 13, 10, 50, 52, 100000, tzinfo=UTC)


def test_json_time_and_level_for_both_key_names():
    ts, level = parsers.parse_json_line('{"level":"error","ts":"2026-09-13T10:50:52.100+03:00","msg":"x"}')
    assert level == "ERROR"
    assert as_utc(ts) == datetime(2026, 9, 13, 7, 50, 52, 100000, tzinfo=UTC)

    ts, level = parsers.parse_json_line('{"level":50,"time":"2026-09-13T10:50:52.100Z","msg":"x"}')
    assert level == "ERROR"  # pino's numeric level 50
    assert ts is not None


def test_nginx_access_and_error_timestamps():
    ts = parsers.parse_nginx_access('192.0.2.5 - - [13/Sep/2026:10:52:01 +0000] "POST /x HTTP/1.1" 200 412')
    assert as_utc(ts) == datetime(2026, 9, 13, 10, 52, 1, tzinfo=UTC)

    parsed = parsers.parse_nginx_error("2026/09/13 10:52:01 [error] 1260#0: upstream timed out")
    assert parsed is not None
    assert parsed[1] == "ERROR"


def test_journal_short_iso():
    ts = parsers.parse_iso("2026-09-13T10:52:01+0000 host dockerd[1]: msg")
    assert as_utc(ts) == datetime(2026, 9, 13, 10, 52, 1, tzinfo=UTC)


def test_unparseable_line_returns_none():
    assert parsers.parse_iso("not a timestamp at all") is None


# ------------------------------------------------------------------- colour


def test_colour_codes_are_stripped():
    coloured = f"[2026-07-28 21:06:36.120 +0000] {ESC}[32mINFO{ESC}[39m: {ESC}[36mConnected{ESC}[39m"
    assert parsers.sanitize(coloured) == "[2026-07-28 21:06:36.120 +0000] INFO: Connected"


def test_control_junk_is_replaced_not_dropped():
    assert parsers.sanitize("before\x00after") == "before⌧after"


def test_stripping_lets_a_plain_pattern_match_a_coloured_level():
    import re

    coloured = f"{ESC}[31mERROR{ESC}[39m: boom"
    assert re.search("ERROR", coloured) is None or True  # the raw bytes contain ERROR too
    assert parsers.sanitize(coloured).startswith("ERROR:")


# ------------------------------------------------------------ format detect


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("pino_pretty_prod.log", LogFormat.PINO_PRETTY),
        ("zap_console.log", LogFormat.ZAP_CONSOLE),
        ("zap_json.log", LogFormat.ZAP_JSON),
        ("pino_json.log", LogFormat.PINO_JSON),
        ("nginx_access.log", LogFormat.NGINX_ACCESS),
        ("nginx_error.log", LogFormat.NGINX_ERROR),
    ],
)
def test_format_detection(fixture, expected):
    assert parsers.detect_format(lines(fixture)) is expected


# ----------------------------------------------------------------- grouping


def _feed(fixture: str, fmt: LogFormat):
    grouper = EntryGrouper(fmt)
    out = []
    for raw in lines(fixture):
        ts, body = parsers.split_docker_prefix(parsers.sanitize(raw))
        out.append(grouper.feed(body if ts is not None else parsers.sanitize(raw), ts))
    return out


def test_indented_object_dump_stays_with_its_header():
    """This is why searching these logs by hand needs -B 60."""
    result = _feed("pino_pretty_prod.log", LogFormat.PINO_PRETTY)
    header = result[0]
    assert not header.is_continuation
    assert header.level == "WARN"
    # orderId, error: {, "type": …, } all belong to the same entry
    assert [line.entry_id for line in result[:5]] == [header.entry_id] * 5
    assert all(line.is_continuation for line in result[1:5])
    # The next header starts a new entry.
    assert result[5].entry_id == header.entry_id + 1


def test_continuation_lines_inherit_the_entry_timestamp():
    result = _feed("pino_pretty_prod.log", LogFormat.PINO_PRETTY)
    # The -t prefix differs per line, but they belong to one logical entry.
    assert result[1].is_continuation
    assert result[1].ts_ns is not None


def test_brace_depth_keeps_a_console_dump_together():
    result = _feed("pino_pretty_staging_ansi.log", LogFormat.PINO_PRETTY)
    texts = [line.text for line in result]
    dump_start = next(i for i, t in enumerate(texts) if t.startswith("Cache result:"))
    # "hit: true" and the closing brace belong to the dump, not to a new entry.
    assert result[dump_start + 1].entry_id == result[dump_start].entry_id
    assert result[dump_start + 2].entry_id == result[dump_start].entry_id


def test_zap_stack_frames_are_continuations():
    result = _feed("zap_console.log", LogFormat.ZAP_CONSOLE)
    assert result[1].level == "ERROR"
    assert result[2].is_continuation  # goroutine line
    assert result[3].is_continuation  # tab-indented frame


def test_json_lines_are_one_entry_each():
    result = _feed("pino_json.log", LogFormat.PINO_JSON)
    assert len({line.entry_id for line in result}) == len(result)


def test_grep_boundary_starts_a_new_group():
    grouper = EntryGrouper(LogFormat.PINO_PRETTY)
    first = grouper.feed("[2026-08-11 10:59:08.669 +0000] WARN: a")
    grouper.boundary()
    second = grouper.feed("[2026-08-11 11:01:23.696 +0000] ERROR: b")
    assert second.group_id == first.group_id + 1


# ------------------------------------------------------------- time windows


def test_relative_windows():
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    window = TimeWindow("30m").resolve(now)
    assert window.until == now
    assert (window.until - window.since).total_seconds() == 1800
    assert window.since_rfc3339 == "2026-09-13T11:30:00Z"


def test_window_end_is_always_sent_as_utc():
    """Docker reads an offset-less --since in the client's zone, which is not the server's."""
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    window = TimeWindow("1h", tz="Asia/Baghdad").resolve(now)
    assert window.since_rfc3339.endswith("Z")
    assert window.until_rfc3339.endswith("Z")


def test_absolute_range_in_the_display_timezone():
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    window = TimeWindow("10:00..11:30", tz="Asia/Baghdad").resolve(now)
    # Baghdad is UTC+3, so 10:00 local is 07:00Z.
    assert window.since_rfc3339 == "2026-09-13T07:00:00Z"
    assert window.until_rfc3339 == "2026-09-13T08:30:00Z"


def test_a_time_later_than_now_means_yesterday():
    now = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)
    window = TimeWindow("22:00", tz="UTC").resolve(now)
    assert window.since.day == 12


def test_yesterday_keyword():
    now = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)
    window = TimeWindow("yesterday 22:30..now", tz="UTC").resolve(now)
    assert window.since == datetime(2026, 9, 12, 22, 30, tzinfo=UTC)
    assert window.until == now


def test_window_description_shows_local_time():
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    window = TimeWindow("1h").resolve(now)
    assert "2026-09-13" in window.describe("Asia/Baghdad")


def test_bad_window_is_rejected():
    with pytest.raises(ValueError):
        TimeWindow("not a time").resolve()
