"""Classifying what OpenSSH printed into something the operator can act on."""

from __future__ import annotations

import pytest

from sshfleet.ssh import ErrorKind, classify, first_line
from sshfleet.ssh.errors import RETRYABLE, SshError

CASES = [
    ("ssh: connect to host 10.0.0.1 port 22: Connection timed out", ErrorKind.TIMEOUT_CONNECT),
    ("Connection timed out during banner exchange", ErrorKind.TIMEOUT_CONNECT),
    ("ssh: connect to host 10.0.0.1 port 22: Connection refused", ErrorKind.REFUSED),
    ("ssh: Could not resolve hostname nope: Name or service not known", ErrorKind.UNREACHABLE),
    ("ssh: connect to host 10.0.0.1 port 22: No route to host", ErrorKind.UNREACHABLE),
    ("channel 0: open failed: connect failed: Connection refused", ErrorKind.JUMP_TARGET_UNREACHABLE),
    ("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", ErrorKind.HOSTKEY_MISMATCH),
    ("Host key verification failed.", ErrorKind.HOSTKEY_MISMATCH),
    ("ubuntu@10.0.0.1: Permission denied (publickey).", ErrorKind.AUTH_FAILED),
    ("Received disconnect: Too many authentication failures", ErrorKind.AUTH_FAILED),
    ("WARNING: UNPROTECTED PRIVATE KEY FILE! Permissions 0644", ErrorKind.KEY_FILE),
    ("kex_exchange_identification: Connection closed by remote host", ErrorKind.SSHD_BUSY),
    ("ControlSocket /run/user/1000/x already exists, disabling multiplexing", ErrorKind.MUX_STALE),
    ("Control socket connect(/run/user/1000/x): Connection refused", ErrorKind.MUX_BROKEN),
    ("client_loop: send disconnect: Broken pipe", ErrorKind.DISCONNECTED),
    ("sudo: a password is required", ErrorKind.SUDO_REQUIRED),
    ("sudo: no tty present and no askpass program specified", ErrorKind.SUDO_REQUIRED),
    ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.", ErrorKind.DAEMON_DOWN),
    ("bash: line 1: docker: command not found", ErrorKind.RUNTIME_MISSING),
    ("Error response from daemon: No such container: order-production", ErrorKind.CONTAINER_NOT_FOUND),
]


@pytest.mark.parametrize(("stderr", "expected"), CASES)
def test_classify_known_failures(stderr: str, expected: ErrorKind):
    assert classify(255, stderr) is expected


def test_success_is_not_an_error():
    assert classify(0, "Warning: Permanently added '10.0.0.1' to the list of known hosts.") is None


def test_unrecognised_remote_failure_is_distinct_from_ssh_failure():
    assert classify(1, "grep: no match") is ErrorKind.REMOTE_FAILED
    assert classify(255, "something nobody predicted") is ErrorKind.UNKNOWN


def test_timeout_distinguishes_slow_command_from_slow_connection():
    assert classify(255, "", timed_out=True, mux_alive=True) is ErrorKind.COMMAND_TIMEOUT
    assert classify(255, "", timed_out=True, mux_alive=False) is ErrorKind.TIMEOUT_CONNECT


def test_only_transient_kinds_are_retried():
    assert ErrorKind.SSHD_BUSY in RETRYABLE
    assert ErrorKind.MUX_STALE in RETRYABLE
    # Retrying these would just repeat the same failure.
    assert ErrorKind.AUTH_FAILED not in RETRYABLE
    assert ErrorKind.HOSTKEY_MISMATCH not in RETRYABLE
    assert ErrorKind.SUDO_REQUIRED not in RETRYABLE


def test_first_line_skips_openssh_noise():
    stderr = (
        "Warning: Permanently added '10.0.0.1' (ED25519) to the list of known hosts.\n"
        "ubuntu@10.0.0.1: Permission denied (publickey).\n"
    )
    assert first_line(stderr) == "ubuntu@10.0.0.1: Permission denied (publickey)."


def test_suggestions_are_concrete_and_never_auto_run():
    error = SshError(ErrorKind.HOSTKEY_MISMATCH, "acme-prod-auth", "changed")
    suggestion = error.suggestion(hostname="10.0.5.7")
    assert suggestion.startswith("ssh-keygen -R 10.0.5.7")
    assert error.cell_code == "KEY!"

    sudo = SshError(ErrorKind.SUDO_REQUIRED, "acme-prod-order", "needs sudo")
    assert "sudo -n true" in sudo.suggestion()
