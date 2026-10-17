"""The remote pipelines: shape, quoting, caps, and what they refuse to build."""

from __future__ import annotations

import re
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import make_config

from sshfleet.inventory import build_inventory
from sshfleet.logs import commands
from sshfleet.logs.model import LogFormat, LogSource, SearchRequest, SourceKind, TimeWindow
from sshfleet.logs.sources import container_source_for

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def inventory():
    return build_inventory(make_config())


@pytest.fixture
def window():
    return TimeWindow("2h").resolve(NOW)


def stdout_source(alias: str = "tst-order", container: str = "order-production") -> LogSource:
    return LogSource(
        id=f"testing/{container}", kind=SourceKind.CONTAINER_STDOUT, env="testing", label="order",
        host_alias=alias, service="order", container=container, log_format=LogFormat.PINO_PRETTY,
    )


def request(**kwargs) -> SearchRequest:
    defaults = dict(sources=(stdout_source(),), pattern="error|fatal", before=60, tail_lines=50_000)
    defaults.update(kwargs)
    return SearchRequest(**defaults)


# ------------------------------------------------------------------- shape


def test_tail_always_accompanies_since(inventory, window):
    """Without --tail the json-file reader scans from the start of the file."""
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(), window).script
    assert "--tail 50000" in script
    assert "--since" in script
    assert script.index("--tail") < script.index("--since") or "--tail" in script


def test_since_is_sent_as_utc_with_z(inventory, window):
    """Docker reads an offset-less timestamp in the client's zone, not the server's."""
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(), window).script
    match = re.search(r"--since (\S+)", script)
    assert match
    assert match.group(1).endswith("Z"), match.group(1)


def test_colour_is_stripped_before_grep(inventory, window):
    """Otherwise ERROR would not match a line that is really ESC[31mERROR."""
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(), window).script
    assert script.index("sed -u -E") < script.index("grep -a")


def test_grep_marks_matches_and_context_distinctly(inventory, window):
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(), window).script
    assert "-n" in script  # "N:" for a match, "N-" for context
    assert "-B 60" in script
    assert "-a" in script  # a NUL byte must not turn the stream into "Binary file matches"


def test_fixed_mode_uses_grep_f(inventory, window):
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(mode="fixed"), window).script
    grep_stage = next(line for line in script.splitlines() if line.lstrip("| ").startswith("grep "))
    assert " -F " in grep_stage
    assert " -E " not in grep_stage


def test_caps_are_applied_remotely(inventory, window):
    script = commands.build(
        inventory.host("tst-order"), stdout_source(), request(max_bytes=1024, max_line_bytes=80), window
    ).script
    assert "cut -b -80" in script
    assert "head -c 1024" in script


def test_until_is_enforced_by_awk_not_by_docker(inventory, window):
    """podman's --until is unreliable; exiting early SIGPIPEs the producer just the same."""
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(), window).script
    assert "--until" not in script
    assert "awk -v U=" in script
    assert "exit" in script


def test_coverage_markers_are_emitted(inventory, window):
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(), window).script
    assert commands.FIRST_MARKER in script
    assert commands.COUNT_MARKER in script
    assert commands.STATUS_MARKER in script


def test_missing_container_is_detected_before_streaming(inventory, window):
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(), window).script
    assert "inspect" in script
    assert commands.ERROR_MARKER in script


def test_wallet_pipeline_uses_podman_without_sudo(inventory, window):
    source = LogSource(
        id="testing/ewallet", kind=SourceKind.CONTAINER_STDOUT, env="testing", label="wallet",
        host_alias="tst-wallet", service="wallet", container="ewallet-production",
    )
    script = commands.build(inventory.host("tst-wallet"), source, request(sources=(source,)), window).script
    assert "podman" in script
    assert "sudo" not in script


def test_glob_container_is_resolved_on_the_host(inventory, window):
    """Kong containers are not named by the deploy pipeline, so the name is matched remotely."""
    source = LogSource(
        id="testing/kong", kind=SourceKind.CONTAINER_STDOUT, env="testing", label="kong-m",
        host_alias="tst-kong-mobile", service="kong-mobile", container="*kong*",
    )
    script = commands.build(inventory.host("tst-kong-mobile"), source, request(sources=(source,)), window).script
    assert "ps --format" in script
    assert "grep -i -- kong" in script
    assert '"$C"' in script


def test_raw_dump_reproduces_the_manual_command(inventory, window):
    """The operator's habit: tail 50000, grep with context, no time window, no caps."""
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(raw_dump=True), window).script
    assert "--tail 50000" in script
    assert "--since" not in script
    assert "head -c" not in script
    assert "grep -a" in script


def test_no_pattern_means_no_grep_stage(inventory, window):
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(pattern=""), window).script
    assert "grep -a" not in script


# ------------------------------------------------------------------ safety


@pytest.mark.parametrize(
    "pattern",
    ["'; rm -rf / #", "$(reboot)", "`shutdown now`", "-v", "a|b", "user's error", 'say "hi"'],
)
def test_hostile_patterns_are_quoted_not_executed(inventory, window, pattern):
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(pattern=pattern), window).script
    # The pattern appears as a quoted argument to grep, and nothing dangerous survives once
    # the quoted data is removed — which is exactly what the safety check does.
    assert shlex.quote(pattern) in script
    structure = commands.strip_quoted(script, [pattern])
    for dangerous in ("rm -rf", "reboot", "shutdown"):
        assert dangerous not in structure


def test_pattern_starting_with_a_dash_is_passed_after_e(inventory, window):
    """Without -e, grep would read the pattern as an option."""
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(pattern="-v"), window).script
    assert "-e -v --" in script


def test_container_name_is_quoted_everywhere(inventory, window):
    """Quoted where it is used, and nowhere bare: an earlier version of this test only looked
    for the quoted form, and passed while the same name sat unquoted in an ``echo`` below it."""
    name = "weird name; rm -rf /"
    source = stdout_source(container=name)
    script = commands.build(inventory.host("tst-order"), source, request(sources=(source,)), window).script
    assert shlex.quote(name) in script
    assert name not in script.replace(shlex.quote(name), "")


#: Names that are commands. `{mark}` is a file each one would create if it ever ran.
HOSTILE_NAMES = ["x$(touch {mark})", "x`touch {mark}`", "*x$(touch {mark})*"]


def _docker_with_no_containers(tmp_path):
    """A local stand-in for the host: docker knows no containers, sudo just runs the command."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text('#!/bin/sh\ncase "$1" in inspect) exit 1;; *) exit 0;; esac\n')
    (fake / "sudo").write_text('#!/bin/sh\nwhile [ "$1" = "-n" ]; do shift; done\nexec "$@"\n')
    for entry in fake.iterdir():
        entry.chmod(0o755)
    return {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp_path)}


@pytest.mark.parametrize("follow", [False, True], ids=["search", "follow"])
@pytest.mark.parametrize("template", HOSTILE_NAMES)
def test_a_hostile_container_name_is_printed_never_run(inventory, window, tmp_path, template, follow):
    """Regression: the name went bare into ``echo "… missing_container NAME"``, so ``$(…)`` or
    backticks in it ran on the host as soon as the container turned out not to exist.

    The source is made directly, past the name check, to test the quoting on its own. The script
    is really run, against a docker that knows no containers, which is the path that echoed.
    """
    if follow and template.startswith("*"):
        pytest.skip("follow names one container; it has no wildcard form")
    mark = tmp_path / "ran"
    name = template.format(mark=mark)
    source = stdout_source(container=name)
    host = inventory.host("tst-order")
    if follow:
        script = commands.follow_pipeline(host, source, "error").script
    else:
        script = commands.build(host, source, request(sources=(source,)), window).script
    run = subprocess.run(
        ["bash", "-s"], input=script, text=True, capture_output=True, env=_docker_with_no_containers(tmp_path), timeout=20
    )
    assert not mark.exists(), "the container name was executed"
    assert run.returncode == 90, run.stderr
    assert f"missing_container {name}" in run.stderr


@pytest.mark.parametrize("template", HOSTILE_NAMES)
def test_pipelines_for_odd_names_are_valid_shell(inventory, window, tmp_path, template):
    name = template.format(mark=tmp_path / "ran")
    source = stdout_source(container=name)
    host = inventory.host("tst-order")
    scripts = [commands.build(host, source, request(sources=(source,)), window).script]
    if not name.startswith("*"):
        scripts.append(commands.follow_pipeline(host, source, "error").script)
    for script in scripts:
        checked = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize("name", ["x$(id)", "x`id`", "*x$(id)*", "a b", "a;b", "a|b", "-rf", "x\ny", ""])
def test_a_name_that_is_not_a_container_name_is_refused_at_the_door(inventory, name):
    with pytest.raises(ValueError, match="not a container name"):
        container_source_for(inventory.host("tst-order"), name)


@pytest.mark.parametrize("name", ["order-production", "order-api_1.2", "a", "*kong*"])
def test_real_names_and_wildcards_pass_the_door(inventory, name):
    assert container_source_for(inventory.host("tst-order"), name).container == name


def test_the_read_only_check_sees_a_value_left_unquoted():
    """It used to remove each value in its bare form too, which hid exactly this mistake."""
    value = "x$(rm -rf /tmp/z)"
    with pytest.raises(commands.CommandRefused):
        commands.assert_read_only(f'echo "missing {value}"', [value])
    commands.assert_read_only(f"echo missing {shlex.quote(value)}", [value])  # quoted, it is data


def test_follow_is_checked_like_a_search(inventory, monkeypatch):
    checked = []
    monkeypatch.setattr(commands, "assert_read_only", lambda script, data=(): checked.append(list(data)))
    commands.follow_pipeline(inventory.host("tst-order"), stdout_source(), "error")
    assert checked and "order-production" in checked[0] and "error" in checked[0]


def test_secret_paths_are_refused_as_log_sources(inventory, window):
    source = LogSource(
        id="x", kind=SourceKind.HOST_FILE, env="testing", label="order",
        host_alias="tst-order", path="/home/ubuntu/order.env.production",
    )
    with pytest.raises(commands.CommandRefused, match="secret"):
        commands.build(inventory.host("tst-order"), source, request(sources=(source,)), window)


def test_paths_outside_the_allowed_roots_are_refused(inventory, window):
    source = LogSource(
        id="x", kind=SourceKind.HOST_FILE, env="testing", label="order",
        host_alias="tst-order", path="/etc/passwd",
    )
    with pytest.raises(commands.CommandRefused, match="must live under"):
        commands.build(inventory.host("tst-order"), source, request(sources=(source,)), window)


def test_read_only_check_rejects_a_destructive_script():
    with pytest.raises(commands.CommandRefused):
        commands.assert_read_only("docker logs x | grep y; rm -rf /tmp/z")


# ----------------------------------------------------------- other sources


def test_host_file_pipeline_selects_rotated_files_by_mtime(inventory, window):
    source = LogSource(
        id="x", kind=SourceKind.HOST_FILE, env="testing", label="wallet", host_alias="tst-wallet",
        path="/home/ec2-user/logs/ewallet/production/app.log", rotated_glob="app-*.log",
        log_format=LogFormat.ZAP_JSON,
    )
    script = commands.build(inventory.host("tst-wallet"), source, request(sources=(source,)), window).script
    assert "-newermt" in script  # a file last written before the window cannot contain it
    assert "app-*.log" in script
    assert "zcat" in script  # rotated files may be compressed
    assert "nice -n 10" in script  # a big grep must not disturb the service


def test_container_file_pipeline_only_tails_inside_the_container(inventory, window):
    source = LogSource(
        id="x", kind=SourceKind.CONTAINER_FILE, env="testing", label="order", host_alias="tst-order",
        container="order-production", path="/usr/src/app/logs/access.log", log_format=LogFormat.PINO_JSON,
    )
    script = commands.build(inventory.host("tst-order"), source, request(sources=(source,)), window).script
    # busybox inside the container only has to run tail/wc; GNU grep runs on the host.
    assert "exec" in script
    assert "tail -c" in script
    assert "grep -a" in script
    assert script.index("exec") < script.index("grep -a")


def test_journal_pipeline_forces_utc(inventory, window):
    source = LogSource(
        id="x", kind=SourceKind.JOURNAL, env="testing", label="order/sys", host_alias="tst-order",
        unit="docker.service", log_format=LogFormat.JOURNAL,
    )
    script = commands.build(inventory.host("tst-order"), source, request(sources=(source,)), window).script
    assert "env TZ=UTC" in script  # sudo resets the environment
    assert "-o short-iso" in script  # Amazon Linux 2 ships systemd 219
    assert "-u docker.service" in script


def test_kernel_journal_uses_dash_k(inventory, window):
    source = LogSource(
        id="x", kind=SourceKind.JOURNAL, env="testing", label="order/sys", host_alias="tst-order", unit="kernel",
    )
    script = commands.build(inventory.host("tst-order"), source, request(sources=(source,)), window).script
    assert " -k " in script


def test_follow_pipeline_has_no_caps(inventory):
    source = stdout_source()
    pipeline = commands.follow_pipeline(inventory.host("tst-order"), source, "error")
    assert "-f" in pipeline.script
    assert "head -c" not in pipeline.script
    assert "timeout" not in pipeline.script  # follow runs until the reader stops it


# --------------------------------------------------- windows in another zone


@pytest.fixture
def riyadh_window():
    """``10:00..11:30`` typed by someone whose display zone is +03, the config default."""
    return TimeWindow("10:00..11:30", tz="Asia/Riyadh").resolve(NOW)


def test_journalctl_is_given_the_window_in_utc(inventory, riyadh_window):
    """The producer runs under TZ=UTC, so a wall clock from another zone reads three hours out."""
    source = LogSource(
        id="testing/journal", kind=SourceKind.JOURNAL, env="testing", label="order/sys",
        host_alias="tst-order", unit="docker.service", log_format=LogFormat.JOURNAL,
    )
    script = commands.build(inventory.host("tst-order"), source, request(), riyadh_window).script
    assert "--since '2026-09-13 07:00:00'" in script, script
    assert "--until '2026-09-13 08:30:00'" in script, script


def test_find_is_given_the_window_in_utc(inventory, riyadh_window):
    """``-newermt`` is handed the literal 'UTC', so the value beside it has to be UTC too."""
    source = LogSource(
        id="testing/app", kind=SourceKind.HOST_FILE, env="testing", label="wallet",
        host_alias="tst-wallet", path="/home/ec2-user/logs/app.log", log_format=LogFormat.ZAP_JSON,
    )
    script = commands.build(inventory.host("tst-wallet"), source, request(), riyadh_window).script
    assert "-newermt '2026-09-13 07:00:00 UTC'" in script, script


def test_the_remote_file_filters_are_deliberately_generous(inventory, window):
    """They compare text and cannot read an offset, so they pre-filter and the runner decides."""
    source = LogSource(
        id="testing/app", kind=SourceKind.HOST_FILE, env="testing", label="wallet",
        host_alias="tst-wallet", path="/home/ec2-user/logs/app.log", log_format=LogFormat.ZAP_JSON,
    )
    script = commands.build(inventory.host("tst-wallet"), source, request(), window).script
    # The window is 2026-09-13 10:00..12:00; the awk bounds reach a day either side of it.
    assert "-v S=2026-09-12T10:00:00" in script, script
    assert "-v U=2026-09-14T12:00:00" in script, script


def test_the_docker_prefix_is_still_compared_exactly(inventory, window):
    """``docker logs -t`` is always UTC with Z, so that one stage keeps its early exit."""
    script = commands.build(inventory.host("tst-order"), stdout_source(), request(), window).script
    assert "-v U=2026-09-13T12:00:00" in script, script


def test_the_cli_refuses_a_container_name_that_is_a_command(configured, fake_ssh):
    """A message and the usage exit code, not a traceback, and nothing sent anywhere."""
    from typer.testing import CliRunner

    from sshfleet.cli.main import app

    result = CliRunner().invoke(app, ["logs", "search", "-e", "testing", "-p", "x", "-c", "tst-order:x$(id)"])
    assert result.exit_code == 2
    assert "is not a container name" in result.output
    assert [c for c in fake_ssh.calls() if "x$(id)" in str(c)] == []


def test_a_rotated_glob_never_reads_a_secret_as_a_log(inventory, window, tmp_path):
    """`rotated_glob` comes from the config; "*" in a directory that also holds the app's .env
    read it as a log. Whatever the glob matches, a name on the deny list is not one of the files.
    The script's own file search is run, in bash, on a real directory."""
    for name in ("app.log", "app.log.1", ".env", "app.env", "server.key", "notes.secret"):
        (tmp_path / name).write_text("line\n")
    source = LogSource(
        id="f", kind=SourceKind.HOST_FILE, env="testing", label="wallet", host_alias="tst-wallet",
        path=str(tmp_path / "app.log"), rotated_glob="*", log_format=LogFormat.PLAIN,
    )
    script = commands.build(
        inventory.host("tst-wallet"), source, request(sources=(source,), allowed_paths=(str(tmp_path),)), window
    ).script
    search = script.split("[ ${#FILES[@]} -gt 0 ]")[0] + 'printf "%s\\n" "${FILES[@]}"\n'
    found = subprocess.run(["bash", "-s"], input=search, text=True, capture_output=True, timeout=20)
    assert found.returncode == 0, found.stderr
    assert sorted(Path(line).name for line in found.stdout.split()) == ["app.log", "app.log.1"]
