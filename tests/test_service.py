"""``sshfleet service``: the definition it writes and the supervisor calls it makes, with no supervisor present."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from sshfleet import service
from sshfleet.doctor import service_checks
from sshfleet.paths import Paths
from sshfleet.service import LABEL, UNIT, ServiceError, supervisor

PYTHON = "/opt/sshfleet/bin/python3"


class FakeRun:
    """Records every argv; answers from ``replies`` (substring of the joined argv → (code, stdout))."""

    def __init__(self, replies: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.replies = replies or {}

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, (code, out) in self.replies.items():
            if needle in joined:
                return subprocess.CompletedProcess(argv, code, out, "" if code == 0 else "fake failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def joined(self) -> list[str]:
        return [" ".join(call) for call in self.calls]


def make(tmp_path: Path, *, system: str = "Linux", environ: dict[str, str] | None = None, run: FakeRun | None = None, python: str = PYTHON, which=lambda name: f"/usr/bin/{name}"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return supervisor(
        system=system,
        paths=Paths.detect(home / "sshfleet-home"),
        home=home,
        environ={"PATH": "/usr/bin:/bin"} if environ is None else environ,
        run=run or FakeRun(),
        python=python,
        which=which,
    )


# ---------------------------------------------------------------------------------- systemd


def test_systemd_unit_names_this_very_install(tmp_path):
    manager = make(tmp_path)
    plan = manager.plan(9000)
    assert plan.path == tmp_path / "home" / ".config" / "systemd" / "user" / UNIT
    assert f"ExecStart={PYTHON} -m sshfleet web --no-open --port 9000" in plan.content
    assert "WantedBy=default.target" in plan.content
    assert "Restart=on-failure" in plan.content
    assert 'Environment="PATH=/usr/bin:/bin"' in plan.content
    assert plan.url == "http://127.0.0.1:9000"
    assert not plan.path.exists(), "plan touches nothing"


def test_install_writes_enables_and_starts(tmp_path):
    run = FakeRun()
    manager = make(tmp_path, run=run)
    plan = manager.install(8765, busy=lambda port: False)
    assert plan.started
    assert plan.path.read_text() == plan.content
    assert plan.path.stat().st_mode & 0o777 == 0o644
    assert run.joined() == ["systemctl --user daemon-reload", f"systemctl --user enable --now {UNIT}"]
    assert manager.installed
    assert manager.port() == 8765


def test_install_does_not_start_over_a_busy_port(tmp_path):
    run = FakeRun()
    manager = make(tmp_path, run=run)
    plan = manager.install(8765, busy=lambda port: port == 8765)
    assert not plan.started
    assert "port 8765 is in use" in plan.note
    assert "sshfleet service start" in plan.note
    assert run.joined() == ["systemctl --user daemon-reload", f"systemctl --user enable {UNIT}"]
    assert plan.path.exists(), "registered for the next login all the same"


def test_uninstall_removes_the_unit_and_tolerates_a_second_call(tmp_path):
    run = FakeRun()
    manager = make(tmp_path, run=run)
    manager.install(busy=lambda port: False)
    run.calls.clear()
    assert manager.uninstall()
    assert not manager.unit_path.exists()
    assert run.joined() == [f"systemctl --user disable --now {UNIT}", "systemctl --user daemon-reload"]
    run.calls.clear()
    assert not manager.uninstall()
    assert run.calls == [], "nothing installed, nothing asked of systemd"


def test_a_failing_systemctl_is_reported_not_swallowed(tmp_path):
    run = FakeRun({"enable": (1, "")})
    manager = make(tmp_path, run=run)
    with pytest.raises(ServiceError, match=r"enable --now sshfleet\.service` failed: fake failure"):
        manager.install(busy=lambda port: False)


def test_overrides_are_carried_and_a_per_login_agent_is_warned_about(tmp_path):
    environ = {
        "PATH": "/usr/local/bin:/usr/bin",
        "SSHFLEET_HOME": "/srv/fleet",
        "SSH_AUTH_SOCK": "/tmp/ssh-abc123/agent.4242",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "HOME": "/home/op",
    }
    plan = make(tmp_path, environ=environ).plan()
    assert plan.environment == {"PATH": "/usr/local/bin:/usr/bin", "SSHFLEET_HOME": "/srv/fleet"}
    assert 'Environment="SSHFLEET_HOME=/srv/fleet"' in plan.content
    assert "SSH_AUTH_SOCK" not in plan.content
    assert any("per-login agent" in w and "import-environment" in w for w in plan.warnings)


def test_a_keyring_agent_is_carried(tmp_path):
    environ = {"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh", "XDG_RUNTIME_DIR": "/run/user/1000"}
    plan = make(tmp_path, environ=environ).plan()
    assert plan.environment["SSH_AUTH_SOCK"] == "/run/user/1000/keyring/ssh"
    assert not any("agent" in w for w in plan.warnings)


def test_no_agent_at_all_is_said_once(tmp_path):
    plan = make(tmp_path, environ={"PATH": "/usr/bin"}).plan()
    assert [w for w in plan.warnings if "SSH_AUTH_SOCK is unset" in w]


def test_a_late_mounted_drive_is_pointed_out(tmp_path):
    plan = make(tmp_path, python="/run/media/op/disk/tool/.venv/bin/python").plan()
    assert any("mounted after login" in w for w in plan.warnings)


def test_a_temporary_interpreter_is_refused(tmp_path):
    manager = make(tmp_path, python="/home/op/.cache/uv/archive-v0/abc123/bin/python")
    with pytest.raises(ServiceError, match="uv tool install sshfleet"):
        manager.plan()


def test_a_virtualenv_python_is_named_as_is_not_resolved(tmp_path, monkeypatch):
    # A venv's python is a symlink to a bare interpreter without sshfleet in it: following it
    # would write a service that cannot start. (pytest's tmp_path lives under the temp dir,
    # which the throwaway-environment check would otherwise refuse.)
    monkeypatch.setattr(service.tempfile, "gettempdir", lambda: str(tmp_path / "not-here"))
    base = tmp_path / "base" / "bin"
    base.mkdir(parents=True)
    (base / "python3.12").write_text("")
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python3").symlink_to(base / "python3.12")
    plan = make(tmp_path, python=str(venv / "python3")).plan()
    assert plan.command[0] == str(venv / "python3")


def test_systemd_status_reads_show(tmp_path):
    show = "\n".join(
        [
            "LoadState=loaded",
            "ActiveState=active",
            "SubState=running",
            "MainPID=4321",
            "ExecMainStartTimestamp=Fri 2026-10-03 09:00:00 +04",
            "NRestarts=2",
            "UnitFileState=enabled",
            "Result=success",
        ]
    )
    run = FakeRun({"show": (0, show)})
    manager = make(tmp_path, run=run)
    manager.install(8800, busy=lambda port: False)
    status = manager.status()
    assert status.installed and status.enabled and status.active
    assert status.state == "running"
    assert status.pid == 4321
    assert status.restarts == 2
    assert status.since == "Fri 2026-10-03 09:00:00 +04"
    assert status.port == 8800
    assert status.url == "http://127.0.0.1:8800"
    assert manager.logs_command(lines=50, follow=True) == ["journalctl", "--user", "-u", UNIT, "-n", "50", "--no-pager", "-f"]


def test_systemd_status_calls_a_restart_loop_failed(tmp_path):
    show = "LoadState=loaded\nActiveState=activating\nSubState=auto-restart\nMainPID=0\nNRestarts=7\nUnitFileState=enabled\nResult=exit-code\n"
    manager = make(tmp_path, run=FakeRun({"show": (0, show)}))
    manager.install(busy=lambda port: False)
    status = manager.status()
    assert status.state == "failed"
    assert not status.active
    assert "exit-code" in status.detail and "retrying" in status.detail


def test_status_when_nothing_is_installed(tmp_path):
    run = FakeRun()
    status = make(tmp_path, run=run).status()
    assert not status.installed
    assert status.state == "not installed"
    assert status.url is None
    assert run.calls == []


def test_no_user_manager_is_a_clear_error(tmp_path):
    manager = make(tmp_path, run=FakeRun({"show": (1, "")}))
    manager.install(busy=lambda port: False)
    with pytest.raises(ServiceError, match="user service manager"):
        manager.status()


def test_linger_asks_logind(tmp_path):
    run = FakeRun()
    manager = make(tmp_path, run=run)
    assert isinstance(manager, service.Systemd)
    manager.linger()
    assert run.joined() == ["loginctl enable-linger"]


# ---------------------------------------------------------------------------------- launchd


def test_launchd_agent_is_a_valid_plist_for_this_install(tmp_path):
    manager = make(tmp_path, system="Darwin", environ={"PATH": "/opt/homebrew/bin:/usr/bin"})
    plan = manager.plan(8765)
    assert plan.path == tmp_path / "home" / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    payload = plistlib.loads(plan.content.encode())
    assert payload["Label"] == LABEL
    assert payload["ProgramArguments"] == [PYTHON, "-m", "sshfleet", "web", "--no-open", "--port", "8765"]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["EnvironmentVariables"] == {"PATH": "/opt/homebrew/bin:/usr/bin"}
    assert payload["StandardOutPath"].endswith("state/service.log")
    assert not plan.warnings, "macOS agents inherit the session's ssh agent; nothing to warn about"


def test_launchd_install_bootstraps_into_the_gui_domain(tmp_path):
    run = FakeRun()
    manager = make(tmp_path, system="Darwin", run=run)
    plan = manager.install(busy=lambda port: False)
    domain = f"gui/{os.getuid()}"
    assert run.joined() == [f"launchctl bootout {domain}/{LABEL}", f"launchctl bootstrap {domain} {plan.path}"]
    assert plan.started
    assert manager.port() == 8765
    run.calls.clear()
    assert manager.uninstall()
    assert run.joined() == [f"launchctl bootout {domain}/{LABEL}"]
    assert not plan.path.exists()


def test_launchd_status_reads_print(tmp_path):
    printed = f"{LABEL} = {{\n\tactive count = 1\n\tpath = /x\n\tstate = running\n\n\tpid = 777\n\tlast exit code = 0\n}}\n"
    run = FakeRun({"print": (0, printed)})
    manager = make(tmp_path, system="Darwin", run=run)
    manager.install(busy=lambda port: False)
    status = manager.status()
    assert status.active and status.state == "running" and status.pid == 777
    assert manager.logs_command(lines=20, follow=False) == ["tail", "-n", "20", str(manager.log_path)]


def test_launchd_status_when_not_loaded_in_this_session(tmp_path):
    manager = make(tmp_path, system="Darwin", run=FakeRun({"print": (113, "")}))
    manager.install(busy=lambda port: True)
    status = manager.status()
    assert status.installed and not status.active
    assert status.state == "stopped"
    assert "next login" in status.detail


# ------------------------------------------------------------------------------- platforms


def test_other_systems_are_told_what_to_do_instead(tmp_path):
    with pytest.raises(ServiceError, match="not on Windows"):
        make(tmp_path, system="Windows")


def test_linux_without_systemd_is_told_too(tmp_path):
    with pytest.raises(ServiceError, match="no systemd"):
        make(tmp_path, which=lambda name: None)


# --------------------------------------------------------------------------------- doctor


def test_doctor_is_silent_without_a_service_and_speaks_when_there_is_one(tmp_path):
    run = FakeRun({"show": (0, "ActiveState=inactive\nSubState=dead\nMainPID=0\nUnitFileState=enabled\nResult=success\n")})
    manager = make(tmp_path, run=run)
    assert service_checks(manager) == []
    manager.install(busy=lambda port: True)
    [check] = service_checks(manager)
    assert check.name == "background service"
    assert check.level == "warn"
    assert check.detail.startswith("stopped")
    assert check.fix == "sshfleet service start"
