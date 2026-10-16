"""Merging host stanzas from a bundle into the operator's own SSH file, a stanza at a time."""

from __future__ import annotations

from pathlib import Path

import pytest

from sshfleet.inventory import parse_ssh_config
from sshfleet.inventory.sshmerge import merge_text, split_stanzas

SAMPLE = (Path(__file__).parent / "sshconfigs" / "sample-env").read_text()

LOCAL = """\
# Team bastion — ask ops before changing
Host bastion
    HostName 1.2.3.4
    User ec2-user
    IdentityFile ~/.ssh/team.pem

# The order service
Host web-1
    HostName 10.0.0.1
    ProxyJump bastion
    IdentityFile ~/.ssh/team.pem

Host only-here
    HostName 10.0.0.9

Match host *.internal exec "true"
    User admin

Host *
    ServerAliveInterval 30
"""


def parses(text: str, tmp_path: Path) -> list[str]:
    path = tmp_path / "merged"
    path.write_text(text)
    return [s.alias for s in parse_ssh_config(path)]


def blocks(text: str) -> dict[tuple[str, ...], str]:
    lines = text.splitlines(keepends=True)
    _, stanzas = split_stanzas(lines)
    return {s.patterns: "".join(lines[s.start : s.end]) for s in stanzas}


def test_merging_a_file_into_itself_changes_nothing():
    for text in (LOCAL, SAMPLE):
        merged, changes = merge_text(text, text)
        assert merged == text
        assert {c.action for c in changes} <= {"same"}


def test_a_new_host_goes_above_the_wildcard_block_and_the_rest_stays_byte_for_byte(tmp_path: Path):
    incoming = "Host web-2\n    HostName 10.0.0.2\n    ProxyJump bastion\n"
    merged, changes = merge_text(LOCAL, incoming)
    assert [(c.label, c.action) for c in changes] == [("web-2", "add")]
    assert merged.index("Host web-2") < merged.index("Host *")
    before, after = blocks(LOCAL), blocks(merged)
    for patterns, text in before.items():
        assert after[patterns] == text, patterns
    assert parses(merged, tmp_path) == ["bastion", "web-1", "only-here", "web-2"]


def test_a_host_that_differs_keeps_the_local_version_unless_the_bundle_is_preferred(tmp_path: Path):
    incoming = "# from the bundle\nHost web-1\n    HostName 10.0.0.100\n    ProxyJump bastion\n"
    kept, changes = merge_text(LOCAL, incoming)
    assert kept == LOCAL
    assert changes[0].action == "kept-local"

    taken, changes = merge_text(LOCAL, incoming, prefer="bundle")
    assert changes[0].action == "update"
    web = blocks(taken)[("web-1",)]
    assert "HostName 10.0.0.100" in web
    assert "# The order service" in web  # the operator's own comment stays with the stanza
    assert "IdentityFile ~/.ssh/team.pem" in web  # and so does this machine's key
    assert "# from the bundle" not in taken
    assert blocks(taken)[("bastion",)] == blocks(LOCAL)[("bastion",)]
    assert parses(taken, tmp_path) == ["bastion", "web-1", "only-here"]


def test_a_differing_identity_file_alone_is_not_a_difference():
    incoming = "Host web-1\n    HostName 10.0.0.1\n    ProxyJump bastion\n    IdentityFile ~/.ssh/someone-elses.pem\n"
    merged, changes = merge_text(LOCAL, incoming, prefer="bundle")
    assert changes[0].action == "same"
    assert merged == LOCAL


def test_a_partly_overlapping_stanza_is_a_conflict_not_a_guess():
    incoming = "Host web-1 web-1-alt\n    HostName 10.0.0.1\n"
    merged, changes = merge_text(LOCAL, incoming, prefer="bundle")
    assert merged == LOCAL
    assert changes[0].action == "conflict" and "web-1" in changes[0].note


def test_wildcards_and_match_blocks_are_left_alone_in_merge_mode():
    incoming = 'Host *\n    ServerAliveInterval 5\n\nMatch host *.internal exec "rm -rf ~"\n    User root\n'
    merged, changes = merge_text(LOCAL, incoming, prefer="bundle")
    assert merged == LOCAL
    assert {c.action for c in changes} == {"skipped"}


def test_replace_mode_takes_the_bundle_but_keeps_each_hosts_identity_file(tmp_path: Path):
    incoming = "Host bastion\n    HostName 1.2.3.4\n    User ec2-user\n\nHost web-1\n    HostName 10.0.0.100\n"
    merged, changes = merge_text(LOCAL, incoming, mode="replace")
    assert "only-here" not in merged
    assert merged.count("IdentityFile ~/.ssh/team.pem") == 2
    actions = {c.label: c.action for c in changes}
    assert actions == {"bastion": "same", "web-1": "update", "only-here": "remove", 'host *.internal exec "true"': "remove", "*": "remove"}
    assert parses(merged, tmp_path) == ["bastion", "web-1"]


@pytest.mark.parametrize("prefer", ["local", "bundle"])
def test_the_result_always_parses_and_loses_no_local_host(tmp_path: Path, prefer: str):
    incoming = SAMPLE.replace("10.", "172.").replace("tst-order", "tst-order-new")
    merged, _ = merge_text(SAMPLE, incoming, prefer=prefer)
    original = tmp_path / "original"
    original.mkdir()
    aliases = parses(merged, tmp_path)
    assert set(parses(SAMPLE, original)) <= set(aliases)
    assert "tst-order-new" in aliases
