"""Parsing the operator's SSH config files."""

from __future__ import annotations

from pathlib import Path

import pytest

from sshfleet.inventory import common_prefix, parse_ssh_config, summarize

SAMPLE = Path(__file__).parent / "sshconfigs" / "sample-env"


def test_parses_every_host_and_skips_wildcards():
    stanzas = parse_ssh_config(SAMPLE)
    aliases = [s.alias for s in stanzas]
    assert aliases == [
        "tst-bastion",
        "tst-jenkins-proxy",
        "tst-order",
        "tst-auth",
        "tst-wallet",
        "tst-admin-dashboard",
        "tst-kong-mobile",
        "tst-jenkins",
    ]
    assert all("*" not in a for a in aliases)


def test_keywords_are_case_insensitive_and_values_are_stripped():
    by_alias = {s.alias: s for s in parse_ssh_config(SAMPLE)}
    # lowercase "Hostname"
    assert by_alias["tst-auth"].hostname == "10.99.0.11"
    # trailing whitespace after the address
    assert by_alias["tst-wallet"].hostname == "10.99.0.12"
    assert by_alias["tst-wallet"].user == "ec2-user"


def test_identity_files_are_paths_only_and_inner_hosts_have_none():
    by_alias = {s.alias: s for s in parse_ssh_config(SAMPLE)}
    assert by_alias["tst-bastion"].identity_files == ["~/.ssh/test.pem"]
    assert by_alias["tst-order"].identity_files == []
    assert by_alias["tst-order"].proxy_jump == "tst-bastion"


def test_summary_counts_what_the_wizard_shows():
    summary = summarize(parse_ssh_config(SAMPLE))
    assert summary["hosts"] == 8
    assert summary["proxy_jumps"] == {"tst-bastion": 4, "tst-jenkins-proxy": 1}
    assert summary["users"] == {"ubuntu": 7, "ec2-user": 1}
    assert summary["without_identity_file"] == 5


def test_common_prefix_requires_a_dash_and_minimum_length():
    assert common_prefix(["acme-prod-order", "acme-prod-auth", "acme-prod-user"]) == "acme-prod-"
    assert common_prefix(["ab-one", "ab-two"]) is None  # too short to be a useful pattern
    assert common_prefix(["only-one"]) is None


def test_include_is_followed_once(tmp_path: Path):
    included = tmp_path / "inner"
    included.write_text("Host inner-host\n    HostName 10.0.0.9\n    User ubuntu\n")
    outer = tmp_path / "outer"
    outer.write_text(f"Include {included}\n\nHost outer-host\n    HostName 10.0.0.8\n")
    aliases = [s.alias for s in parse_ssh_config(outer)]
    assert aliases == ["inner-host", "outer-host"]


def test_include_cycle_does_not_recurse_forever(tmp_path: Path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.write_text(f"Include {second}\nHost a-host\n    HostName 10.0.0.1\n")
    second.write_text(f"Include {first}\nHost b-host\n    HostName 10.0.0.2\n")
    aliases = [s.alias for s in parse_ssh_config(first)]
    assert aliases == ["b-host", "a-host"]


def test_match_blocks_are_ignored(tmp_path: Path):
    path = tmp_path / "cfg"
    path.write_text(
        "Host real\n    HostName 10.0.0.1\n\nMatch host other\n    User root\n\nHost second\n    HostName 10.0.0.2\n"
    )
    stanzas = {s.alias: s for s in parse_ssh_config(path)}
    assert set(stanzas) == {"real", "second"}
    assert stanzas["real"].user is None  # the Match block's User must not leak


def test_equals_separator_is_accepted(tmp_path: Path):
    path = tmp_path / "cfg"
    path.write_text("Host eq\n    HostName=10.0.0.7\n    User=ubuntu\n    Port=2222\n")
    stanza = parse_ssh_config(path)[0]
    assert (stanza.hostname, stanza.user, stanza.port) == ("10.0.0.7", "ubuntu", 2222)


def test_missing_file_raises_a_clear_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="cannot read ssh config"):
        parse_ssh_config(tmp_path / "nope")


def test_multiple_proxyjump_hops_keep_the_first(tmp_path: Path):
    path = tmp_path / "cfg"
    path.write_text("Host multi\n    HostName 10.0.0.3\n    ProxyJump first,second\n")
    assert parse_ssh_config(path)[0].proxy_jump == "first"


def test_an_include_glob_never_reads_a_key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``Include ~/.ssh/*`` matches the private keys too. They are skipped by name before anything
    opens them — reading key material is the one thing this tool never does."""
    from sshfleet.inventory import included_files

    ssh = tmp_path / "ssh"
    ssh.mkdir()
    (ssh / "work").write_text("Host tst-work\n  HostName 10.0.0.1\n")
    for name in ("id_ed25519", "deploy.pem", "id_ed25519.pub", "server.key", "laptop.ppk"):
        (ssh / name).write_text("Host from-a-key\n")
    main = tmp_path / "main"
    main.write_text(f"Include {ssh}/*\n")

    opened: list[str] = []
    original = Path.read_text

    def spy(self: Path, *args, **kwargs):
        opened.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    assert [s.alias for s in parse_ssh_config(main)] == ["tst-work"]
    assert [p.name for p in included_files(main)] == ["main", "work"]
    assert set(opened) == {"main", "work"}
