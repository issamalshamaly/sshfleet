"""Quoting, held for any input rather than for a list of examples.

The hostile names in test_logs_commands.py are the ones somebody thought of; the container-name
injection got past every example that existed at the time. These generate the rest: whatever a
search pattern or a container name holds, the pipeline is valid shell, the read-only check never
mistakes the data for structure, a command hidden in the name never runs, and a container
command splits back into exactly the words it was built from.
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from helpers import make_config
from hypothesis import given, settings
from hypothesis import strategies as st

from sshfleet.containers.runtime import RemoteCommand
from sshfleet.inventory import build_inventory
from sshfleet.logs import commands
from sshfleet.logs.model import LogFormat, LogSource, SearchRequest, SourceKind, TimeWindow

INVENTORY = build_inventory(make_config())
HOST = INVENTORY.host("tst-order")
WINDOW = TimeWindow("2h").resolve(datetime(2026, 9, 13, 12, 0, tzinfo=UTC))

#: Anything a shell might take for syntax. NUL is left out: no shell string can hold it, and
#: nothing that reaches a pipeline can carry one.
ANY_TEXT = st.text(st.characters(exclude_categories=("Cs",), exclude_characters="\x00"), max_size=40)
SHELL_HEAVY = st.text(st.sampled_from(list("$`'\"\\;|&<>(){}[]*?!~#\n\t =-x")), max_size=24)
VALUES = ANY_TEXT | SHELL_HEAVY


def _source(container: str) -> LogSource:
    """Made directly, past the check at the door, to hold the quoting to account on its own."""
    return LogSource(
        id="testing/x", kind=SourceKind.CONTAINER_STDOUT, env="testing", label="x",
        host_alias="tst-order", container=container, log_format=LogFormat.PINO_PRETTY,
    )


def _scripts(container: str, pattern: str) -> list[str]:
    source = _source(container)
    request = SearchRequest(sources=(source,), pattern=pattern, before=60, tail_lines=50_000)
    scripts = [commands.build(HOST, source, request, WINDOW).script]
    if not container.startswith("*"):
        scripts.append(commands.follow_pipeline(HOST, source, pattern).script)
    return scripts


@settings(max_examples=150, deadline=None)
@given(container=VALUES.filter(bool), pattern=VALUES)
def test_any_name_and_pattern_make_valid_shell_that_passes_the_read_only_check(container, pattern):
    # build() and follow_pipeline() run the read-only check themselves; it must not fire on data.
    for script in _scripts(container, pattern):
        checked = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert checked.returncode == 0, checked.stderr


@settings(max_examples=60, deadline=None)
@given(before=VALUES, after=VALUES, pattern=VALUES)
def test_a_command_hidden_anywhere_in_a_name_never_runs(before, after, pattern):
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        mark = root / "ran"
        fake = root / "bin"
        fake.mkdir()
        # The host as the missing-container path sees it: docker knows nothing, sudo just runs.
        (fake / "docker").write_text('#!/bin/sh\ncase "$1" in inspect) exit 1;; *) exit 0;; esac\n')
        (fake / "sudo").write_text('#!/bin/sh\nwhile [ "$1" = "-n" ]; do shift; done\nexec "$@"\n')
        for entry in fake.iterdir():
            entry.chmod(0o755)
        for hidden in (f"$(touch {mark})", f"`touch {mark}`"):
            for script in _scripts(before + hidden + after, pattern):
                subprocess.run(
                    ["bash", "-s"], input=script, text=True, capture_output=True,
                    env={"PATH": f"{fake}:/usr/bin:/bin", "HOME": scratch}, timeout=20,
                )
                assert not mark.exists(), "part of the container name was run as a command"


@settings(max_examples=200, deadline=None)
@given(verb=st.sampled_from(["ps", "inspect", "logs", "stats", "restart", "exec"]), args=st.lists(ANY_TEXT, max_size=5))
def test_a_container_command_splits_back_into_the_words_it_was_built_from(verb, args):
    rendered = RemoteCommand(HOST, verb, tuple(args)).render()
    prefix = ["sudo", "-n"] if HOST.sudo else []
    assert shlex.split(rendered) == [*prefix, HOST.runtime or "docker", verb, *args]
