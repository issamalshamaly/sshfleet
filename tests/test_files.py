"""File browsing, the deny list, and transfer fallbacks."""

from __future__ import annotations

from pathlib import Path

import pytest

from sshfleet.files import DeniedPath, assert_allowed, is_denied, parse_listing, reason_denied

# Paths shaped like real deploy secrets, which must never be downloadable.
SECRETS = [
    "/home/ubuntu/order.env.production",
    "/home/ubuntu/auth.env.staging",
    "/home/ubuntu/jwt_private_key.pem",
    "/home/ubuntu/firebase_private_key.pem",
    "/home/ubuntu/nbi-client-key.pem",
    "/home/ec2-user/ewallet.env.production",
    "/home/ubuntu/.ssh/id_rsa",
    "/home/ubuntu/.ssh/authorized_keys",
    "/root/.ssh/id_ed25519",
    "/etc/ssl/private/server.key",
    "/var/lib/jenkins/secrets/initialAdminPassword",
    "/home/ubuntu/.aws/credentials",
    "/srv/app/.env",
    "/srv/app/.env.local",
]

ALLOWED = [
    "/var/log/syslog",
    "/var/log/nginx/access.log",
    "/home/ubuntu/order-log.txt",
    "/home/ec2-user/logs/ewallet/production/app.log",
    "/usr/src/app/logs/access.log",
    "/tmp/report.json",
    "/home/ubuntu/jenkins-deploy.sh",
]


@pytest.mark.parametrize("path", SECRETS)
def test_secret_paths_are_denied(path):
    assert is_denied(path), f"{path} must be refused"
    with pytest.raises(DeniedPath):
        assert_allowed(path)


@pytest.mark.parametrize("path", ALLOWED)
def test_ordinary_paths_are_allowed(path):
    assert reason_denied(path) is None, f"{path} should be downloadable"
    assert_allowed(path)


def test_deny_reason_is_explained():
    reason = reason_denied("/home/ubuntu/order.env.production")
    assert reason and "secret" in reason.lower()


def test_extra_patterns_from_config_are_applied():
    assert reason_denied("/data/customers.csv") is None
    assert is_denied("/data/customers.csv", ["*.csv"])


def test_deny_list_is_case_insensitive():
    assert is_denied("/home/ubuntu/ORDER.ENV.PRODUCTION")
    assert is_denied("/home/ubuntu/Key.PEM")


def test_path_traversal_cannot_smuggle_a_secret():
    assert is_denied("/var/log/../home/ubuntu/order.env.production")


# ------------------------------------------------------------------- listing


def _record(kind, size, mtime, mode, owner, group, name, link=""):
    return f"{kind}\t{size}\t{mtime}\t{mode}\t{owner}\t{group}\t{name}\t{link}\0"


def test_listing_parses_nul_separated_records():
    raw = (
        _record("d", "4096", "1757760000", "755", "ubuntu", "ubuntu", "logs")
        + _record("f", "1234", "1757760100", "644", "ubuntu", "ubuntu", "order-log.txt")
        + _record("f", "600", "1757760200", "600", "ubuntu", "ubuntu", "order.env.production")
    )
    listing = parse_listing(raw, "/home/ubuntu")
    names = [e.name for e in listing.entries]
    assert names == ["logs", "order-log.txt", "order.env.production"]  # dirs first, then alphabetical
    assert listing.entries[0].is_dir
    assert listing.entries[1].size == 1234
    assert listing.entries[2].denied  # the env file is flagged in the listing itself


def test_listing_handles_names_with_spaces_and_quotes():
    raw = _record("f", "10", "1757760000", "644", "ubuntu", "ubuntu", "a file 'with' quotes.txt")
    listing = parse_listing(raw, "/tmp")
    assert listing.entries[0].name == "a file 'with' quotes.txt"


def test_listing_ignores_malformed_records():
    raw = "garbage\0" + _record("f", "1", "1757760000", "644", "u", "u", "ok.txt")
    listing = parse_listing(raw, "/tmp")
    assert [e.name for e in listing.entries] == ["ok.txt"]


def test_listing_is_truncated_at_the_limit():
    raw = "".join(_record("f", "1", "1757760000", "644", "u", "u", f"file{i}") for i in range(50))
    listing = parse_listing(raw, "/tmp", limit=10)
    assert len(listing.entries) == 10
    assert listing.truncated


# ------------------------------------------------------------------ transfer


async def test_download_refuses_a_secret_before_connecting(context, fake_ssh, tmp_path: Path):
    from sshfleet.files import download

    host = context.inventory.host("tst-order")
    with pytest.raises(DeniedPath):
        await download(context.runner, host, "/home/ubuntu/order.env.production", tmp_path / "x")
    # Nothing was attempted: no ssh, scp or rsync invocation happened.
    assert fake_ssh.calls() == []


async def test_upload_refuses_a_secret_destination(context, tmp_path: Path):
    from sshfleet.files import upload

    local = tmp_path / "payload.txt"
    local.write_text("hello")
    host = context.inventory.host("tst-order")
    with pytest.raises(DeniedPath):
        await upload(context.runner, host, local, "/home/ubuntu/auth.env.production")


async def test_upload_requires_the_local_file_to_exist(context, tmp_path: Path):
    from sshfleet.files import upload

    host = context.inventory.host("tst-order")
    with pytest.raises(FileNotFoundError):
        await upload(context.runner, host, tmp_path / "missing.txt", "/tmp/x")


def test_download_directory_is_namespaced_by_env_and_host(context, tmp_path: Path):
    from sshfleet.files import download_dir_for

    host = context.inventory.host("tst-order")
    target = download_dir_for(tmp_path, host)
    assert target.parts[-3:-1] == ("testing", "tst-order")


def test_rsync_progress_parsing():
    from sshfleet.files.transfer import PROGRESS_RE

    line = b"      1,234,567  42%    1.23MB/s    0:00:03"
    match = PROGRESS_RE.search(line)
    assert match
    assert int(match.group(1).replace(b",", b"")) == 1234567
    assert int(match.group(2)) == 42
