"""Container commands and the restart-then-verify flow.

The safety rule is not a UI convention: it is enforced where commands are built, and this
module is where that is proven.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from helpers import make_config

from sshfleet.containers import (
    ALLOWED_VERBS,
    FORBIDDEN_VERBS,
    ForbiddenCommand,
    Phase,
    RemoteCommand,
    RestartOutcome,
    actions,
    restart_and_verify,
    restart_command,
)
from sshfleet.containers import runtime as rt
from sshfleet.inventory import build_inventory

SRC = Path(__file__).parents[1] / "src"


@pytest.fixture
def inventory():
    return build_inventory(make_config())


# --------------------------------------------------------------------- safety


@pytest.mark.parametrize("verb", sorted(FORBIDDEN_VERBS))
def test_destructive_verbs_cannot_be_built(inventory, verb):
    host = inventory.host("tst-order")
    with pytest.raises(ForbiddenCommand):
        RemoteCommand(host, verb, ("order-production",))


def test_unknown_verbs_are_refused(inventory):
    with pytest.raises(ForbiddenCommand):
        RemoteCommand(inventory.host("tst-order"), "system", ("prune",))


def test_source_contains_no_destructive_container_command():
    """A grep the UI cannot get around: no stop/rm/kill/prune anywhere in the source.

    Lines that *define* the deny lists are allowed — that is where these strings are supposed
    to appear.
    """
    pattern = re.compile(r"\b(docker|podman)\s+(stop|rm|kill|prune|down)\b")
    allowed_context = ("FORBIDDEN", "BANNED", "deny", "refus")
    offenders = []
    for path in SRC.rglob("*"):
        if path.suffix not in {".py", ".sh"} or not path.is_file():
            continue
        lines = path.read_text().splitlines()
        in_deny_block = False
        for number, line in enumerate(lines, 1):
            if re.search(r"^(BANNED_FRAGMENTS|FORBIDDEN_VERBS|HARD_DENY)\b", line):
                in_deny_block = True
            elif in_deny_block and line.rstrip().endswith((")", "]", "}")) and not line.startswith(" "):
                in_deny_block = False
            if not pattern.search(line):
                continue
            if in_deny_block or any(token in line for token in allowed_context):
                continue
            offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    assert offenders == [], "destructive container commands found:\n" + "\n".join(offenders)


def test_allowed_and_forbidden_sets_do_not_overlap():
    assert not (ALLOWED_VERBS & FORBIDDEN_VERBS)


# -------------------------------------------------------------------- commands


def test_restart_command_is_in_place_with_sudo(inventory):
    command = restart_command(inventory.host("tst-order"), "order-production")
    assert command.render() == "sudo -n docker restart -t 10 order-production"


def test_wallet_uses_podman_without_sudo(inventory):
    command = restart_command(inventory.host("tst-wallet"), "ewallet-production")
    assert command.render() == "podman restart -t 10 ewallet-production"
    assert "sudo" not in command.render()


def test_container_names_are_quoted(inventory):
    rendered = restart_command(inventory.host("tst-order"), "order-production").render()
    assert rendered.endswith(" order-production")  # one argument; render() quotes every part


@pytest.mark.parametrize("name", ["weird name; rm -rf /", "x$(id)", "x`id`", "*kong*", "-f", "a\nb", ""])
def test_a_name_no_runtime_would_produce_never_reaches_a_command(inventory, name):
    """Quoted on the way out as well, but a name like this has no business in a command at all."""
    from sshfleet.containers import inspect_command, logs_command, ps_command, remove_command

    host = inventory.host("tst-order")
    builders = [restart_command, inspect_command, logs_command, remove_command]
    if name:
        builders.append(ps_command)  # ps without a name lists them all, which is its other use
    for build in builders:
        with pytest.raises(ForbiddenCommand, match="not a container name"):
            build(host, name)


def test_the_probe_port_is_only_ever_a_number():
    """It is read as shell by the inner `bash -c` of the no-curl branch."""
    assert "/dev/tcp/127.0.0.1/3001" in actions._probe_block(3001, "/health")
    with pytest.raises(ValueError):
        actions._probe_block("1;id", "/health")


def test_the_status_filter_is_one_quoted_argument(inventory):
    """Regression: ``--filter name=^NAME$`` went into the status script bare."""
    script = actions.status_script(inventory.host("tst-order"), "order-production", None, None)
    assert "--filter 'name=^order-production$'" in script
    with pytest.raises(ValueError, match="not a container name"):
        actions.status_script(inventory.host("tst-order"), "x$(id)", None, None)


def test_preview_shows_the_whole_ssh_command(inventory):
    command = restart_command(inventory.host("tst-order"), "order-production")
    preview = command.preview("/tmp/cfg")
    assert preview.startswith("ssh -F /tmp/cfg tst-order -- ")
    assert "docker restart" in preview


# -------------------------------------------------------------- verify parsing


def test_status_parsing():
    assert actions._parse_status("running\tUp 3 seconds\t200") == ("running", "Up 3 seconds", "200")
    assert actions._parse_status("exited\tExited (1)\t-") == ("exited", "Exited (1)", "-")
    assert actions._parse_status("")[0] == "missing"


def test_probe_codes():
    assert actions._probe_ok("200") and actions._probe_ok("302")
    assert not actions._probe_ok("000") and not actions._probe_ok("500") and not actions._probe_ok("-")


def test_status_script_skips_sudo_on_the_wallet(inventory):
    script = actions.status_script(inventory.host("tst-wallet"), "ewallet-production", 8080, "/health")
    assert "sudo" not in script
    assert "podman" in script
    script = actions.status_script(inventory.host("tst-order"), "order-production", 3001, "/")
    assert "sudo -n" in script


# --------------------------------------------------------------- restart flow


async def _collect(runner, host, name, **kwargs):
    steps, outcome = [], None
    async for event in restart_and_verify(runner, host, name, **kwargs):
        if isinstance(event, RestartOutcome):
            outcome = event
        else:
            steps.append(event)
    return steps, outcome


async def test_successful_restart_is_verified_before_reporting(context, monkeypatch):
    host = context.inventory.host("tst-order")
    calls: list[str] = []
    polls = iter(["running\tUp 1 second\t200", "running\tUp 2 seconds\t200"])

    async def fake_run(self, target, remote, **kwargs):
        calls.append(remote)
        from sshfleet.ssh.runner import RunResult

        return RunResult(host.alias, [], 0, b"", b"", 0.0, None)

    async def fake_script(self, target, script, args=None, **kwargs):
        from sshfleet.ssh.runner import RunResult

        return RunResult(host.alias, [], 0, next(polls).encode(), b"", 0.0, None)

    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run", fake_run)
    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run_script", fake_script)
    monkeypatch.setattr(actions, "POLL_INTERVAL", 0.0)

    steps, outcome = await _collect(context.runner, host, "order-production", port=3001, path="/")
    assert outcome is not None and outcome.ok
    assert outcome.phase is Phase.VERIFIED
    assert steps[0].phase is Phase.RESTARTING
    assert "restart -t 10 order-production" in calls[0]
    # Two consecutive good polls are required, not one.
    assert sum(1 for s in steps if s.phase in {Phase.POLLING, Phase.VERIFIED}) >= 2


async def test_container_that_exits_again_is_reported_as_failed(context, monkeypatch):
    host = context.inventory.host("tst-order")

    async def fake_run(self, target, remote, **kwargs):
        from sshfleet.ssh.runner import RunResult

        return RunResult(host.alias, [], 0, b"", b"", 0.0, None)

    async def fake_script(self, target, script, args=None, **kwargs):
        from sshfleet.ssh.runner import RunResult

        return RunResult(host.alias, [], 0, b"exited\tExited (1) 1 second ago\t-", b"", 0.0, None)

    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run", fake_run)
    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run_script", fake_script)
    monkeypatch.setattr(actions, "POLL_INTERVAL", 0.0)

    _, outcome = await _collect(context.runner, host, "order-production", port=3001, path="/")
    assert outcome is not None
    assert outcome.ok is False
    assert outcome.phase is Phase.FAILED
    assert "exited" in outcome.message


async def test_failed_restart_command_does_not_start_polling(context, monkeypatch):
    host = context.inventory.host("tst-order")
    polled = {"n": 0}

    async def fake_run(self, target, remote, **kwargs):
        from sshfleet.ssh.errors import ErrorKind
        from sshfleet.ssh.runner import RunResult

        return RunResult(host.alias, [], 1, b"", b"Error response from daemon: No such container: x", 0.0, ErrorKind.CONTAINER_NOT_FOUND)

    async def fake_script(self, target, script, args=None, **kwargs):
        polled["n"] += 1
        raise AssertionError("must not poll after the restart itself failed")

    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run", fake_run)
    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run_script", fake_script)

    _, outcome = await _collect(context.runner, host, "missing-container")
    assert outcome is not None and outcome.ok is False
    assert polled["n"] == 0
    assert "No such container" in outcome.message


async def test_verification_gives_up_and_says_so(context, monkeypatch):
    host = context.inventory.host("tst-order")

    async def fake_run(self, target, remote, **kwargs):
        from sshfleet.ssh.runner import RunResult

        return RunResult(host.alias, [], 0, b"", b"", 0.0, None)

    async def fake_script(self, target, script, args=None, **kwargs):
        from sshfleet.ssh.runner import RunResult

        return RunResult(host.alias, [], 0, b"restarting\tRestarting (1)\t-", b"", 0.0, None)

    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run", fake_run)
    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run_script", fake_script)
    monkeypatch.setattr(actions, "POLL_INTERVAL", 0.0)

    _, outcome = await _collect(context.runner, host, "order-production", port=3001, path="/", verify_timeout=0.2)
    assert outcome is not None and outcome.ok is False
    assert "not healthy" in outcome.message


async def test_without_a_probe_the_container_must_stay_up(context, monkeypatch):
    """No HTTP endpoint: the evidence is that it kept running, not that it started."""
    host = context.inventory.host("tst-order")

    async def fake_run(self, target, remote, **kwargs):
        from sshfleet.ssh.runner import RunResult

        return RunResult(host.alias, [], 0, b"", b"", 0.0, None)

    async def fake_script(self, target, script, args=None, **kwargs):
        from sshfleet.ssh.runner import RunResult

        return RunResult(host.alias, [], 0, b"running\tUp 1 second\t-", b"", 0.0, None)

    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run", fake_run)
    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run_script", fake_script)
    monkeypatch.setattr(actions, "POLL_INTERVAL", 0.0)
    monkeypatch.setattr(actions, "MIN_RUNNING_S", 0.0)

    _, outcome = await _collect(context.runner, host, "order-production", port=None, verify_timeout=5)
    assert outcome is not None and outcome.ok


def test_the_status_script_is_valid_shell(inventory):
    """Regression: the probe was built as ``probe=$( (curl …) )``.

    Bash reads a substitution that opens with "(" as arithmetic, so every probe died with a
    syntax error, the probe field came back empty, and a container that had restarted perfectly
    well was polled until the timeout and then reported as failed.
    """
    import shutil
    import subprocess

    bash = shutil.which("bash")
    assert bash, "these scripts are bash"
    for host, name, port, path in (
        (inventory.host("tst-order"), "order-production", 3001, "/health/live"),
        (inventory.host("tst-wallet"), "ewallet-production", 8080, "/health"),
        (inventory.host("tst-order"), "order-production", None, None),
    ):
        script = actions.status_script(host, name, port, path)
        assert "$((" not in script, "a command substitution must never open with a parenthesis"
        checked = subprocess.run([bash, "-n"], input=script, text=True, capture_output=True)
        assert checked.returncode == 0, checked.stderr


def test_the_status_script_reports_the_probe_it_gets(inventory, tmp_path, monkeypatch):
    """Run the real script against fake docker/curl and read the three fields back."""
    import subprocess

    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text('#!/bin/sh\nprintf "running\\tUp 3 seconds\\n"\n')
    (fake / "curl").write_text('#!/bin/sh\nprintf "200"\n')
    (fake / "sudo").write_text('#!/bin/sh\nwhile [ "$1" = "-n" ]; do shift; done\nexec "$@"\n')
    for entry in fake.iterdir():
        entry.chmod(0o755)

    script = actions.status_script(inventory.host("tst-order"), "order-production", 3001, "/health/live")
    env = {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp_path)}
    out = subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True, env=env)
    assert out.returncode == 0, out.stderr
    assert actions._parse_status(out.stdout) == ("running", "Up 3 seconds", "200")


def test_an_open_port_counts_as_healthy_when_there_is_no_curl():
    assert actions._probe_ok("open")
    assert not actions._probe_ok("closed")


async def test_a_probe_that_cannot_answer_falls_back_to_staying_up(context, monkeypatch):
    """A host without curl, or a port nothing listens on yet, must not fail a good restart."""
    host = context.inventory.host("tst-order")

    async def fake_run(self, target, remote, **kwargs):
        from sshfleet.ssh.runner import RunResult

        return RunResult(host.alias, [], 0, b"", b"", 0.0, None)

    async def fake_script(self, target, script, args=None, **kwargs):
        from sshfleet.ssh.runner import RunResult

        # What the broken script used to produce: state and status, and nothing for the probe.
        return RunResult(host.alias, [], 0, b"running\tUp 1 second\t", b"", 0.0, None)

    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run", fake_run)
    monkeypatch.setattr("sshfleet.ssh.runner.SshRunner.run_script", fake_script)
    monkeypatch.setattr(actions, "POLL_INTERVAL", 0.0)
    monkeypatch.setattr(actions, "MIN_RUNNING_S", 0.0)

    steps, outcome = await _collect(context.runner, host, "order-production", port=3001, path="/health/live", verify_timeout=5)
    assert outcome is not None and outcome.ok
    assert any("no answer on port 3001" in step.message for step in steps)


# ------------------------------------------------------------------- remove


def _fixture_name(command: str) -> str:
    """The fake ssh looks up plain commands by this sanitised name."""
    return "cmd_" + "".join(ch if ch.isalnum() else "_" for ch in command)[:80] + ".txt"


def test_rm_is_buildable_only_through_the_remove_action(inventory):
    from sshfleet.containers import GUARDED_VERBS, remove_command

    host = inventory.host("tst-order")
    with pytest.raises(ForbiddenCommand, match="stopped container"):
        RemoteCommand(host, "rm", ("x",))
    assert remove_command(host, "x").render() == "sudo -n docker rm x"
    assert "rm" in GUARDED_VERBS and "rm" not in ALLOWED_VERBS
    for verb in ("stop", "kill", "prune", "down"):
        with pytest.raises(ForbiddenCommand):
            RemoteCommand(host, verb, ("x",))


def _inspect_line(name: str, status: str, exit_code: int) -> str:
    return f"/{name}\texampleorg/x:1\tsha256:aaa\t2026-09-01T00:00:00Z\t2026-09-01T00:00:05Z\t{status}\t{exit_code}\t0\tbridge\t/var/lib/docker/x.log\n"


async def test_remove_refuses_a_container_that_is_running_on_the_host(context, fake_ssh, tmp_path):
    from sshfleet.containers import remove

    host = context.inventory.host("tst-order")
    inspect = rt.inspect_command(host, "order-production").render() + " 2>&1"
    fake_ssh.write_fixture("tst-order", _fixture_name(inspect), _inspect_line("order-production", "running", 0))

    outcome = await remove(context.runner, host, "order-production", save_logs_to=tmp_path / "saved.log")
    assert outcome.ok is False and outcome.state == "running"
    assert "only a stopped container" in outcome.message
    assert not (tmp_path / "saved.log").exists()
    assert not any(" rm " in entry.get("remote", "") for entry in fake_ssh.calls())


async def test_remove_takes_a_stopped_container_after_saving_its_log(context, fake_ssh, tmp_path):
    from sshfleet.containers import remove

    host = context.inventory.host("tst-order")
    name = "loyalty-production"
    fake_ssh.write_fixture("tst-order", _fixture_name(rt.inspect_command(host, name).render() + " 2>&1"), _inspect_line(name, "exited", 137))
    fake_ssh.write_fixture("tst-order", _fixture_name(rt.logs_command(host, name, 2000).render() + " 2>&1"), "last line before it died\n")
    fake_ssh.write_fixture("tst-order", _fixture_name("sudo -n docker rm loyalty-production 2>&1"), "loyalty-production\n")

    outcome = await remove(context.runner, host, name, save_logs_to=tmp_path / "logs" / "saved.log")
    assert outcome.ok is True, outcome.message
    assert outcome.state == "exited"
    assert outcome.command == "sudo -n docker rm loyalty-production"
    assert (tmp_path / "logs" / "saved.log").read_text() == "last line before it died\n"
    assert (tmp_path / "logs" / "saved.log").stat().st_mode & 0o777 == 0o600
    remotes = [entry.get("remote", "") for entry in fake_ssh.calls()]
    assert remotes.index(next(r for r in remotes if "docker logs" in r)) < remotes.index(next(r for r in remotes if " rm " in r))
