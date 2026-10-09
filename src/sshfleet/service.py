"""Keep the web interface running in the background.

``sshfleet web`` is a foreground process: start it, use it, Ctrl+C. Some operators would rather
it were always there, like a dashboard, and just open the browser. This module writes the
service definition the operating system's own supervisor understands — a systemd *user* unit
on Linux, a launch agent on macOS — and drives that supervisor. Nothing runs as root, nothing
listens beyond 127.0.0.1, and the definition is a plain file the operator can read and edit.

Every subprocess goes through one injectable ``run`` so the tests see exactly what would be
executed without a service manager in the room.
"""

from __future__ import annotations

import datetime as dt
import os
import platform
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from .paths import Paths

UNIT = "sshfleet.service"
LABEL = "io.github.issamalshamaly.sshfleet"
DEFAULT_PORT = 8765
# Paths that are mounted after login rather than at boot: a service that lives on one of them
# starts before its own program exists and has to keep trying.
LATE_MOUNTS = ("/run/media/", "/media/", "/mnt/", "/Volumes/")

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class ServiceError(Exception):
    """Something the operator has to decide or fix; the message says what."""


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


@dataclass(frozen=True, slots=True)
class Plan:
    """What ``install`` writes and runs, decided in full before anything is touched."""

    path: Path
    content: str
    command: list[str]
    environment: dict[str, str]
    port: int
    warnings: list[str] = field(default_factory=list)
    #: True once the service was started as part of the install, False when it was only
    #: registered for the next login — see ``note`` for why.
    started: bool = False
    note: str = ""

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True, slots=True)
class Status:
    installed: bool
    path: Path
    #: Starts when the operator logs in.
    enabled: bool
    #: Running right now.
    active: bool
    #: One word for the operator: running, starting, stopped, failed, not installed.
    state: str
    pid: int | None = None
    since: str | None = None
    restarts: int | None = None
    port: int | None = None
    detail: str = ""

    @property
    def url(self) -> str | None:
        return f"http://127.0.0.1:{self.port}" if self.port else None


def interpreter(python: str | None = None) -> list[str]:
    """The command that is this very installation of sshfleet, PATH or no PATH.

    ``python -m sshfleet`` rather than the ``sshfleet`` script: the script's directory is not
    necessarily on the supervisor's PATH, and the interpreter names the exact install the
    operator just tested. The path is taken as given, never resolved: a virtual environment's
    python is a symlink to a bare interpreter that does not have sshfleet in it. A throwaway
    environment — ``uvx sshfleet`` unpacks into a cache that is garbage-collected — cannot be
    pointed at, so it is refused with the fix.
    """
    exe = Path(python or sys.executable).absolute()
    text = str(exe)
    temp = str(Path(tempfile.gettempdir()).resolve())
    if "/uv/archive-" in text or "/.cache/uv/" in text or text.startswith(temp + os.sep):
        raise ServiceError(
            f"sshfleet is running from a temporary environment ({exe}); a service needs a permanent one. "
            "Install it first — `uv tool install sshfleet` or `pipx install sshfleet` — then run "
            "`sshfleet service install` from that copy."
        )
    return [text, "-m", "sshfleet"]


def carried_environment(environ: Mapping[str, str], *, system: str) -> tuple[dict[str, str], list[str]]:
    """The variables the service needs from the shell that installs it, and what it will miss.

    PATH, so ``ssh`` and ``rsync`` are the ones the operator tested with; every ``SSHFLEET_*``
    override, so the service reads the same configuration. On Linux the ssh agent socket too,
    but only when it will still be there after this login ends.
    """
    env: dict[str, str] = {}
    warnings: list[str] = []
    if path := environ.get("PATH"):
        env["PATH"] = path
    for key in sorted(environ):
        if key.startswith("SSHFLEET_"):
            env[key] = environ[key]
    if system == "Linux":
        sock = environ.get("SSH_AUTH_SOCK")
        runtime = (environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}").rstrip("/")
        if not sock:
            warnings.append(
                "no ssh agent in this shell (SSH_AUTH_SOCK is unset): the service will open the key files "
                "directly, which works only for keys without a passphrase"
            )
        elif sock.startswith(runtime + "/"):
            env["SSH_AUTH_SOCK"] = sock
        else:
            warnings.append(
                f"SSH_AUTH_SOCK points at a per-login agent ({sock}) the service cannot rely on, so keys with a "
                f"passphrase will not open from it. Keep them in the agent your desktop runs under {runtime}, "
                "or run `systemctl --user import-environment SSH_AUTH_SOCK` after logging in."
            )
    return env, warnings


def port_busy(port: int) -> bool:
    """Is something already listening on this port on the loopback interface?"""
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


class Supervisor:
    """What the two service managers have in common; each subclass speaks to one of them."""

    kind = ""

    def __init__(
        self,
        *,
        paths: Paths,
        home: Path,
        environ: Mapping[str, str],
        run: Runner,
        python: str | None,
        system: str,
    ) -> None:
        self.paths = paths
        self.home = home
        self.environ = environ
        self.run = run
        self.python = python
        self.system = system

    # -------------------------------------------------------------- shared
    @property
    def unit_path(self) -> Path:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def installed(self) -> bool:
        return self.unit_path.exists()

    def render(self, command: list[str], environment: dict[str, str], port: int) -> str:  # pragma: no cover
        raise NotImplementedError

    def plan(self, port: int = DEFAULT_PORT) -> Plan:
        command = [*interpreter(self.python), "web", "--no-open", "--port", str(port)]
        environment, warnings = carried_environment(self.environ, system=self.system)
        if command[0].startswith(LATE_MOUNTS):
            warnings.append(
                f"sshfleet runs from {command[0]}, which looks like a drive mounted after login: until it is "
                "mounted the service fails and tries again every few seconds, which is harmless but shows in its log"
            )
        return Plan(
            path=self.unit_path,
            content=self.render(command, environment, port),
            command=command,
            environment=environment,
            port=port,
            warnings=warnings,
        )

    def install(self, port: int = DEFAULT_PORT, *, busy: Callable[[int], bool] = port_busy) -> Plan:
        """Write the definition, register it for login, and start it unless the port is taken.

        The port being held almost always means the foreground ``sshfleet web`` the operator is
        replacing. Starting the service over it would only fail and retry, so in that case it is
        registered and left for the next login — or for ``sshfleet service start`` once the
        other one is gone — and the plan says so.
        """
        plan = self.plan(port)
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.unit_path.write_text(plan.content)
        self.unit_path.chmod(0o644)
        if busy(port):
            self.register(start=False)
            note = (
                f"port {port} is in use — probably the `sshfleet web` you are running now. The service starts at "
                "your next login; to switch to it now, stop that one and run `sshfleet service start`."
            )
            return replace(plan, started=False, note=note)
        self.register(start=True)
        return replace(plan, started=True)

    def uninstall(self) -> bool:
        """Stop it, forget it, remove the file. False when there was nothing to remove."""
        if not self.installed:
            return False
        self.unregister()
        self.unit_path.unlink()
        return True

    def port(self) -> int | None:
        """The port the installed definition names, read back from the file."""
        if not self.installed:
            return None
        match = re.search(r"--port\W+(\d+)", self.unit_path.read_text())
        return int(match.group(1)) if match else None

    # ------------------------------------------------------------ specific
    def register(self, *, start: bool) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def unregister(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def start(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def restart(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def status(self) -> Status:  # pragma: no cover - abstract
        raise NotImplementedError

    def logs_command(self, *, lines: int, follow: bool) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def logs_hint(self) -> str:
        return shlex.join(self.logs_command(lines=100, follow=True))

    def _call(self, argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            proc = self.run(argv)
        except OSError as exc:
            raise ServiceError(f"could not run {argv[0]}: {exc.strerror or exc}") from exc
        if check and proc.returncode:
            reason = (proc.stderr or proc.stdout or "").strip() or f"exit status {proc.returncode}"
            raise ServiceError(f"`{shlex.join(argv)}` failed: {reason}")
        return proc


def _header() -> str:
    today = dt.date.today().isoformat()
    return (
        f"# Written by `sshfleet service install` on {today}. Edit it as you like; `sshfleet service install`\n"
        "# writes it again and `sshfleet service uninstall` removes it.\n"
    )


def _systemd_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class Systemd(Supervisor):
    """A systemd user unit: ``~/.config/systemd/user/sshfleet.service``."""

    kind = "systemd"

    @property
    def unit_path(self) -> Path:
        config = Path(self.environ.get("XDG_CONFIG_HOME") or self.home / ".config")
        return config / "systemd" / "user" / UNIT

    def render(self, command: list[str], environment: dict[str, str], port: int) -> str:
        lines = [
            _header(),
            "[Unit]",
            "Description=sshfleet web interface",
            "Documentation=https://github.com/issamalshamaly/sshfleet",
            "After=network-online.target",
            "",
            "[Service]",
            f"ExecStart={shlex.join(command)}",
            "Restart=on-failure",
            "RestartSec=5",
            *(f'Environment="{key}={_systemd_quote(value)}"' for key, value in environment.items()),
            "",
            "[Install]",
            # default.target is the user session: starts at login, stops at logout unless the
            # operator asked for lingering.
            "WantedBy=default.target",
            "",
        ]
        return "\n".join(lines)

    def _systemctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._call(["systemctl", "--user", *args], check=check)

    def register(self, *, start: bool) -> None:
        self._systemctl("daemon-reload")
        self._systemctl("enable", *(["--now"] if start else []), UNIT)

    def unregister(self) -> None:
        # Not checked: a unit that was already stopped or never enabled is not a failure to remove.
        self._systemctl("disable", "--now", UNIT, check=False)

    def uninstall(self) -> bool:
        removed = super().uninstall()
        if removed:
            self._systemctl("daemon-reload", check=False)
        return removed

    def start(self) -> None:
        self._systemctl("start", UNIT)

    def stop(self) -> None:
        self._systemctl("stop", UNIT)

    def restart(self) -> None:
        self._systemctl("restart", UNIT)

    def linger(self) -> None:
        """Keep the user manager — and this service — alive while the operator is logged out."""
        self._call(["loginctl", "enable-linger"])

    def status(self) -> Status:
        if not self.installed:
            return Status(installed=False, path=self.unit_path, enabled=False, active=False, state="not installed")
        props = "LoadState,ActiveState,SubState,MainPID,ExecMainStartTimestamp,NRestarts,UnitFileState,Result"
        proc = self._call(["systemctl", "--user", "show", UNIT, "--property", props], check=False)
        if proc.returncode:
            reason = (proc.stderr or proc.stdout or "").strip() or f"exit status {proc.returncode}"
            raise ServiceError(
                f"cannot reach the user service manager: {reason}. Is this a login session with `systemd --user` running?"
            )
        values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        active_state = values.get("ActiveState", "")
        sub = values.get("SubState", "")
        state = {"active": "running", "activating": "starting", "reloading": "running", "failed": "failed"}.get(active_state, "stopped")
        if active_state == "activating" and sub == "auto-restart":
            state = "failed"
        pid = int(values.get("MainPID") or 0) or None
        restarts = int(values.get("NRestarts") or 0)
        detail = ""
        if state == "failed":
            detail = f"last result: {values.get('Result') or 'unknown'}" + (", retrying" if sub == "auto-restart" else "")
        return Status(
            installed=True,
            path=self.unit_path,
            enabled=values.get("UnitFileState") in {"enabled", "enabled-runtime", "static"},
            active=active_state == "active",
            state=state,
            pid=pid,
            since=values.get("ExecMainStartTimestamp") or None,
            restarts=restarts,
            port=self.port(),
            detail=detail,
        )

    def logs_command(self, *, lines: int, follow: bool) -> list[str]:
        return ["journalctl", "--user", "-u", UNIT, "-n", str(lines), "--no-pager", *(["-f"] if follow else [])]


class Launchd(Supervisor):
    """A launch agent: ``~/Library/LaunchAgents/<label>.plist`` in the GUI session."""

    kind = "launchd"

    @property
    def unit_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    @property
    def log_path(self) -> Path:
        return self.paths.state_dir / "service.log"

    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{LABEL}"

    def render(self, command: list[str], environment: dict[str, str], port: int) -> str:
        payload: dict[str, object] = {
            "Label": LABEL,
            "ProgramArguments": command,
            "RunAtLoad": True,
            # Come back after a crash, stay down after a clean stop.
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 5,
            "ProcessType": "Background",
            "StandardOutPath": str(self.log_path),
            "StandardErrorPath": str(self.log_path),
        }
        if environment:
            payload["EnvironmentVariables"] = environment
        return plistlib.dumps(payload, sort_keys=False).decode()

    def port(self) -> int | None:
        if not self.installed:
            return None
        try:
            arguments = plistlib.loads(self.unit_path.read_bytes()).get("ProgramArguments", [])
            return int(arguments[arguments.index("--port") + 1])
        except (plistlib.InvalidFileException, ValueError, IndexError, AttributeError):
            return None

    def _launchctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._call(["launchctl", *args], check=check)

    def _loaded(self) -> bool:
        return self._launchctl("print", self.target, check=False).returncode == 0

    def register(self, *, start: bool) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Anything launchd still holds from an earlier install is let go first; the file in
        # LaunchAgents is what makes it start at the next login either way.
        self._launchctl("bootout", self.target, check=False)
        if start:
            self._launchctl("bootstrap", self.domain, str(self.unit_path))

    def unregister(self) -> None:
        self._launchctl("bootout", self.target, check=False)

    def start(self) -> None:
        if self._loaded():
            self._launchctl("kickstart", self.target)
        else:
            self._launchctl("bootstrap", self.domain, str(self.unit_path))

    def stop(self) -> None:
        self._launchctl("kill", "SIGTERM", self.target)

    def restart(self) -> None:
        if self._loaded():
            self._launchctl("kickstart", "-k", self.target)
        else:
            self._launchctl("bootstrap", self.domain, str(self.unit_path))

    def status(self) -> Status:
        if not self.installed:
            return Status(installed=False, path=self.unit_path, enabled=False, active=False, state="not installed")
        proc = self._launchctl("print", self.target, check=False)
        if proc.returncode:
            return Status(
                installed=True, path=self.unit_path, enabled=True, active=False, state="stopped", port=self.port(),
                detail="not loaded in this session; it loads at the next login, or now with `sshfleet service start`",
            )
        text = proc.stdout
        running = re.search(r"^\s*state = running", text, re.M) is not None
        pid_match = re.search(r"^\s*pid = (\d+)", text, re.M)
        exit_match = re.search(r"^\s*last exit code = (-?\d+|\(never exited\))", text, re.M)
        state = "running" if running else "stopped"
        detail = ""
        if not running and exit_match and exit_match.group(1) not in {"0", "(never exited)"}:
            state = "failed"
            detail = f"last exit code {exit_match.group(1)}"
        return Status(
            installed=True,
            path=self.unit_path,
            enabled=True,
            active=running,
            state=state,
            pid=int(pid_match.group(1)) if pid_match else None,
            port=self.port(),
            detail=detail,
        )

    def logs_command(self, *, lines: int, follow: bool) -> list[str]:
        return ["tail", "-n", str(lines), *(["-f"] if follow else []), str(self.log_path)]


def supervisor(
    *,
    system: str | None = None,
    paths: Paths | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    run: Runner = _run,
    python: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Supervisor:
    """The service manager of this machine, or a ServiceError saying why there is none."""
    system = system or platform.system()
    kind: type[Supervisor]
    if system == "Linux":
        if not which("systemctl"):
            raise ServiceError(
                "no systemd on this machine, so there is no user service manager to hand sshfleet to. "
                "Start `sshfleet web --no-open` from your session's startup instead."
            )
        kind = Systemd
    elif system == "Darwin":
        kind = Launchd
    else:
        raise ServiceError(
            f"a background service is supported on Linux (systemd) and macOS (launchd), not on {system}. "
            "Start `sshfleet web --no-open` from your session's startup instead."
        )
    return kind(
        paths=paths or Paths.detect(),
        home=home or Path.home(),
        environ=os.environ if environ is None else environ,
        run=run,
        python=python,
        system=system,
    )
