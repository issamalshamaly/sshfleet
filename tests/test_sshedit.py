"""Editing the operator's SSH config files: surgical, reversible, and hard to abuse."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sshfleet.inventory import parse_ssh_config
from sshfleet.inventory.sshedit import HostEdit, SshEditError, add_host, remove_host, update_host

SAMPLE = Path(__file__).parent / "sshconfigs" / "sample-env"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """A copy of the sample file in its own directory, standing in for ~/.ssh/config.d."""
    ssh_dir = tmp_path / "ssh" / "config.d"
    ssh_dir.mkdir(parents=True)
    target = ssh_dir / "test-env"
    shutil.copy(SAMPLE, target)
    return target


@pytest.fixture
def backups(tmp_path: Path) -> Path:
    return tmp_path / "state" / "ssh-backups"


def lines_of(path: Path) -> list[str]:
    return path.read_text().splitlines()


def is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """Every original line still present, in the original order."""
    iterator = iter(haystack)
    return all(line in iterator for line in needle)


def test_adding_a_host_keeps_every_existing_line_exactly_as_it_was(config_file, backups):
    before = lines_of(config_file)
    add_host(config_file, "tst-new", HostEdit(hostname="10.99.0.99", user="ubuntu", proxy_jump="tst-bastion"), backups)
    after = lines_of(config_file)

    assert is_subsequence(before, after)  # nothing rewritten, removed or reordered
    assert "Host tst-new" in after
    stanzas = {s.alias: s for s in parse_ssh_config(config_file)}
    assert stanzas["tst-new"].hostname == "10.99.0.99"
    assert stanzas["tst-new"].proxy_jump == "tst-bastion"
    # The comment header and the quirky lowercase "Hostname" of another host are untouched.
    assert after[0].startswith("# Stand-in for a real environment")
    assert "    Hostname 10.99.0.11" in after


def test_a_new_host_goes_above_the_trailing_wildcard_block(config_file, backups):
    add_host(config_file, "tst-new", HostEdit(hostname="10.99.0.99"), backups)
    text = config_file.read_text()
    assert text.index("Host tst-new") < text.index("Host *")


def test_editing_touches_only_the_named_keywords(config_file, backups):
    add_host(config_file, "tst-new", HostEdit(hostname="10.0.0.1", user="ubuntu"), backups)
    update_host(config_file, "tst-order", HostEdit(user="ec2-user", port="2222"), backups)

    stanzas = {s.alias: s for s in parse_ssh_config(config_file)}
    assert stanzas["tst-order"].user == "ec2-user"
    assert stanzas["tst-order"].port == 2222
    assert stanzas["tst-order"].hostname == "10.99.0.10"  # untouched
    assert stanzas["tst-order"].proxy_jump == "tst-bastion"  # untouched
    assert stanzas["tst-auth"].user == "ubuntu"  # its neighbour is untouched


def test_an_empty_value_removes_the_line_so_a_host_can_become_direct(config_file, backups):
    update_host(config_file, "tst-order", HostEdit(proxy_jump=""), backups)
    stanzas = {s.alias: s for s in parse_ssh_config(config_file)}
    assert stanzas["tst-order"].proxy_jump is None
    assert stanzas["tst-order"].hostname == "10.99.0.10"


def test_renaming_rewrites_only_the_host_line(config_file, backups):
    update_host(config_file, "tst-order", HostEdit(), backups, new_alias="tst-orders")
    aliases = [s.alias for s in parse_ssh_config(config_file)]
    assert "tst-orders" in aliases and "tst-order" not in aliases


def test_removing_a_host_leaves_the_file_tidy(config_file, backups):
    remove_host(config_file, "tst-auth", backups)
    text = config_file.read_text()
    assert "tst-auth" not in text
    assert "\n\n\n" not in text
    assert len(parse_ssh_config(config_file)) == 7


def test_a_stanza_shared_by_two_aliases_keeps_the_others(config_file, backups):
    config_file.write_text(config_file.read_text().replace("Host tst-order\n", "Host tst-order tst-order-alt\n"))
    remove_host(config_file, "tst-order-alt", backups)
    text = config_file.read_text()
    assert "Host tst-order\n" in text
    assert "tst-order-alt" not in text


def test_every_write_leaves_a_backup_outside_the_ssh_directory(config_file, backups):
    original = config_file.read_text()
    add_host(config_file, "tst-new", HostEdit(hostname="10.0.0.1"), backups)
    copies = list(backups.glob("*.bak"))
    assert len(copies) == 1
    assert copies[0].read_text() == original
    assert copies[0].parent != config_file.parent
    # Nothing new beside the original: `Include ~/.ssh/config.d/*` must not find a second copy.
    assert list(config_file.parent.iterdir()) == [config_file]


def test_a_value_cannot_smuggle_a_second_keyword_onto_the_line(config_file, backups):
    with pytest.raises(SshEditError):
        add_host(config_file, "evil", HostEdit(hostname="10.0.0.1\n    ProxyCommand touch /tmp/pwned"), backups)
    with pytest.raises(SshEditError):
        add_host(config_file, "evil", HostEdit(user="root -oProxyCommand=x"), backups)
    assert "ProxyCommand" not in config_file.read_text()


@pytest.mark.parametrize("alias", ["../../etc/passwd", "tst *", "", "tst\tx", "-leading-dash"])
def test_unusable_aliases_are_refused(config_file, backups, alias):
    with pytest.raises(SshEditError):
        add_host(config_file, alias, HostEdit(hostname="10.0.0.1"), backups)


def test_a_duplicate_alias_is_refused(config_file, backups):
    with pytest.raises(SshEditError, match="already declares"):
        add_host(config_file, "tst-order", HostEdit(hostname="10.0.0.1"), backups)


def test_editing_a_host_that_is_not_there_is_an_error(config_file, backups):
    with pytest.raises(SshEditError, match="no Host stanza"):
        update_host(config_file, "tst-nothing", HostEdit(user="x"), backups)


def test_a_bad_port_is_refused(config_file, backups):
    with pytest.raises(SshEditError, match="out of range"):
        add_host(config_file, "tst-new", HostEdit(hostname="10.0.0.1", port="99999"), backups)


def test_adding_to_a_missing_file_creates_it_privately(tmp_path: Path):
    """An environment can start from nothing: the first host creates the file and its directory."""
    target = tmp_path / "config.d" / "lab"
    backup = add_host(target, "lab-web", HostEdit(hostname="192.0.2.1", user="ubuntu"), tmp_path / "backups")
    assert backup is None  # there was nothing to back up
    assert target.read_text().startswith("Host lab-web\n")
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700

    second = add_host(target, "lab-db", HostEdit(hostname="192.0.2.2"), tmp_path / "backups")
    assert second is not None and second.exists()  # from now on every write is backed up
