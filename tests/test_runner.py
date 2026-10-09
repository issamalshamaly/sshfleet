"""The runner: argv shape, concurrency limits, retries, timeouts, cancellation."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

import pytest

from sshfleet.ssh import ErrorKind
from sshfleet.ssh import argv as A
from sshfleet.ssh import proc as proc_module
from sshfleet.ssh.runner import SshRunner


def test_ssh_argv_separates_options_from_the_alias(tmp_path: Path):
    argv = A.ssh_argv(tmp_path / "cfg", "acme-prod-order", "uptime")
    assert argv[1:3] == ["-F", str(tmp_path / "cfg")]
    assert "--" in argv  # an alias starting with "-" can never be read as an option
    assert argv[argv.index("--") + 1] == "acme-prod-order"
    assert argv[-1] == "uptime"


def test_interactive_argv_re_enables_prompts(tmp_path: Path):
    argv = A.ssh_argv(tmp_path / "cfg", "host", interactive=True)
    assert "BatchMode=no" in argv


def test_script_argv_passes_arguments_quoted_but_keeps_the_script_on_stdin(tmp_path: Path):
    argv = A.bash_script_argv(tmp_path / "cfg", "host", ["a b", "it's"])
    remote = argv[-1]
    assert remote.startswith("LC_ALL=C bash -s -- ")
    assert shlex.split(remote)[-2:] == ["a b", "it's"]


def test_rsync_uses_our_ssh_config_and_secluded_args(tmp_path: Path):
    argv = A.rsync_argv(tmp_path / "cfg", "host:/var/log/syslog", str(tmp_path))
    assert "-s" in argv  # keeps the remote shell out of path parsing
    shell = argv[argv.index("-e") + 1]
    assert "-F" in shell and "cfg" in shell


async def test_run_succeeds_and_records_the_invocation(context, fake_ssh):
    result = await context.runner.run("tst-order", "uptime")
    assert result.ok, result.err
    assert "uptime" in fake_ssh.remote_commands("tst-order")


async def test_run_script_feeds_stdin_and_returns_the_fixture(context, fake_ssh):
    path = fake_ssh.write_fixture("tst-order", "probe.json", json.dumps({"hello": "world"}))
    try:
        result = await context.runner.run_script("tst-order", "# sshfleet:probe\necho hi\n")
        assert result.ok
        assert json.loads(result.out) == {"hello": "world"}
        assert "bash -s" in fake_ssh.remote_commands("tst-order")[-1]
    finally:
        path.unlink()


async def test_failure_is_classified_and_carries_the_command(context, scenario):
    scenario("bastion_down")
    result = await context.runner.run("tst-bastion", "uptime", retries=0)
    assert result.ok is False
    assert result.kind is ErrorKind.TIMEOUT_CONNECT
    error = result.error()
    assert error is not None
    assert "tst-bastion" in error.command


async def test_transient_failures_are_retried_once(context, monkeypatch):
    attempts = {"n": 0}
    original = SshRunner._once

    async def flaky(self, alias, args, timeout, stdin_data):
        attempts["n"] += 1
        if attempts["n"] == 1:
            from sshfleet.ssh.runner import RunResult

            return RunResult(alias, args, 255, b"", b"kex_exchange_identification: Connection closed by remote host",
                             0.0, ErrorKind.SSHD_BUSY)
        return await original(self, alias, args, timeout, stdin_data)

    monkeypatch.setattr(SshRunner, "_once", flaky)
    monkeypatch.setattr("sshfleet.ssh.runner.RETRY_DELAY", 0.0)
    result = await context.runner.run("tst-order", "uptime", retries=1)
    assert result.ok
    assert attempts["n"] == 2
    assert result.attempts == 2


async def test_auth_failure_is_not_retried(context, monkeypatch):
    attempts = {"n": 0}

    async def always_denied(self, alias, args, timeout, stdin_data):
        from sshfleet.ssh.runner import RunResult

        attempts["n"] += 1
        return RunResult(alias, args, 255, b"", b"Permission denied (publickey).", 0.0, ErrorKind.AUTH_FAILED)

    monkeypatch.setattr(SshRunner, "_once", always_denied)
    result = await context.runner.run("tst-order", "uptime", retries=1)
    assert result.kind is ErrorKind.AUTH_FAILED
    assert attempts["n"] == 1  # retrying would just repeat the same rejection


SLOW_SSH = str(Path(__file__).parent / "fakes" / "bin" / "slow-ssh")


async def test_timeout_kills_the_process_and_reports_it(context, monkeypatch):
    monkeypatch.setenv("SSHFLEET_SSH_BIN", SLOW_SSH)
    result = await context.runner.run("tst-order", "30", timeout=0.3, retries=0)
    assert result.ok is False
    assert result.kind in {ErrorKind.COMMAND_TIMEOUT, ErrorKind.TIMEOUT_CONNECT}
    assert result.elapsed < 3  # the process was killed, not waited out


async def test_cancellation_kills_the_process_and_propagates(context, monkeypatch):
    monkeypatch.setenv("SSHFLEET_SSH_BIN", SLOW_SSH)
    task = asyncio.create_task(context.runner.run("tst-order", "30", timeout=30))
    await asyncio.sleep(0.2)
    before = _child_count()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert _child_count() <= before - 1 or _child_count() == 0


@pytest.mark.parametrize("loop", ["asyncio", "uvloop"])
def test_a_child_that_exits_before_reading_its_script_still_says_why(loop):
    """ssh that loses its connection mid-script closes stdin under the writer. uvloop reports that
    as a RuntimeError, which ended the whole fleet refresh; the exit status and stderr that
    explain the failure have to come back instead, under either loop."""
    factory = pytest.importorskip("uvloop").new_event_loop if loop == "uvloop" else None
    script = ["sh", "-c", "echo gone >&2; exit 3"]

    async def exited_first() -> tuple[int | None, bytes]:
        child = await proc_module.spawn(script, stdin_pipe=True)
        await child.wait()
        await asyncio.sleep(0.05)  # let the loop close the pipe, as it does under load
        await proc_module.feed_stdin(child, b"x" * 200_000)
        assert child.stderr is not None
        return child.returncode, await child.stderr.read()

    async def through_run() -> proc_module.ProcResult:
        return await proc_module.run(script, stdin_data=b"x" * 2_000_000, timeout=5)

    with asyncio.Runner(loop_factory=factory) as runner:
        assert runner.run(exited_first()) == (3, b"gone\n")
        result = runner.run(through_run())
    assert (result.returncode, result.stderr, result.timed_out) == (3, b"gone\n", False)


def _child_count() -> int:
    """How many slow-ssh processes this test session still has running."""
    import subprocess

    proc = subprocess.run(["pgrep", "-f", "fakes/bin/slow-ssh"], capture_output=True, text=True)
    return len([line for line in proc.stdout.splitlines() if line.strip()])


async def test_per_bastion_concurrency_is_bounded(context, monkeypatch):
    live = {"now": 0, "peak": 0}
    original = SshRunner._once

    async def counting(self, alias, args, timeout, stdin_data):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.05)
        live["now"] -= 1
        return await original(self, alias, args, timeout, stdin_data)

    monkeypatch.setattr(SshRunner, "_once", counting)
    context.config.concurrency.per_bastion = 2
    runner = SshRunner(context.mux, context.inventory, context.config.concurrency)
    hosts = [h for h in context.inventory.hosts() if h.bastion == "tst-bastion"]
    await asyncio.gather(*(runner.run(h, "uptime") for h in hosts))
    assert live["peak"] <= 2


async def test_fan_out_yields_results_as_they_complete(context):
    hosts = context.inventory.hosts("testing")[:4]
    seen = [result.alias async for result in context.runner.fan_out(hosts, "uptime")]
    assert sorted(seen) == sorted(h.alias for h in hosts)
