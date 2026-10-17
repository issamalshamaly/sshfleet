"""Local self-checks: everything that can go wrong before a single packet is sent."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

from . import service as service_module
from .context import Context
from .envsetup import check_key

Level = Literal["ok", "warn", "fail"]


@dataclass(slots=True)
class Check:
    name: str
    level: Level
    detail: str
    fix: str = ""


def _ssh_version() -> str:
    binary = os.environ.get("SSHFLEET_SSH_BIN", "ssh")
    try:
        proc = subprocess.run([binary, "-V"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable ({exc})"
    return (proc.stderr or proc.stdout).strip().splitlines()[0] if (proc.stderr or proc.stdout) else "unknown"


def run_checks(context: Context) -> list[Check]:
    checks: list[Check] = []

    for binary, required in (("ssh", True), ("scp", False), ("rsync", False)):
        path = shutil.which(os.environ.get(f"SSHFLEET_{binary.upper()}_BIN", binary))
        if path:
            checks.append(Check(f"{binary} binary", "ok", path))
        else:
            checks.append(
                Check(
                    f"{binary} binary",
                    "fail" if required else "warn",
                    "not found on PATH",
                    f"install {binary}",
                )
            )

    checks.append(Check("ssh version", "ok", _ssh_version()))

    control = context.mux.control_dir()
    if control.is_dir():
        mode = control.stat().st_mode & 0o777
        path_len = len(str(control)) + 1 + 64  # %C expands to a 64-char hash
        too_long = path_len >= 100  # a Unix socket path is capped at 108 bytes (104 on macOS)
        level: Level = "ok" if mode == 0o700 and not too_long else "warn"
        detail = f"{control} (mode {mode:04o}, socket path ~{path_len} bytes)"
        # The two problems have different cures: a long path makes every connection fail with
        # "ControlPath too long", and no chmod fixes that.
        fixes = []
        if too_long:
            fixes.append("set ssh.control_dir to a short directory, e.g. /tmp/sshfleet-$USER")
        if mode != 0o700:
            fixes.append(f"chmod 700 {control}")
        checks.append(Check("control dir", level, detail, "; ".join(fixes)))
    else:
        checks.append(Check("control dir", "warn", f"{control} missing", "run any sshfleet command to create it"))

    config_file = context.paths.config_file
    if config_file.exists():
        mode = config_file.stat().st_mode & 0o777
        level = "ok" if not mode & 0o077 else "warn"
        checks.append(
            Check("config file", level, f"{config_file} (mode {mode:04o})", "" if level == "ok" else f"chmod 600 {config_file}")
        )
    else:
        checks.append(Check("config file", "warn", f"{config_file} does not exist yet", "sshfleet env add …"))

    secrets = context.paths.secrets_file
    if secrets.exists():
        mode = secrets.stat().st_mode & 0o777
        level = "ok" if not mode & 0o077 else "fail"
        checks.append(
            Check("secrets file", level, f"{secrets} (mode {mode:04o})", "" if level == "ok" else f"chmod 600 {secrets}")
        )

    if not context.config.environments:
        checks.append(Check("environments", "warn", "none registered", "sshfleet env add --name … --ssh-config …"))
    for env_cfg in context.config.environments:
        if not env_cfg.ssh_config_path.exists():
            checks.append(
                Check(
                    f"env {env_cfg.name}",
                    "warn",
                    f"no hosts yet: {env_cfg.ssh_config_path} does not exist",
                    "add a host on the Settings page, or fix the path in config.yaml",
                )
            )
            continue
        try:
            hosts = context.inventory.hosts(env_cfg.name)
        except KeyError:
            hosts = []
        checks.append(Check(f"env {env_cfg.name}", "ok" if hosts else "warn", f"{len(hosts)} hosts from {env_cfg.ssh_config_path}"))

        if env_cfg.key_path:
            result = check_key(env_cfg.key_path)
            checks.append(
                Check(
                    f"key {env_cfg.name}",
                    "ok" if result.ok else "fail",
                    f"{result.path} (mode {result.mode:04o})" if result.exists else f"{result.path}: missing",
                    "" if result.ok else result.fix,
                )
            )

    ssh_config = context.mux.ssh_config
    if ssh_config.exists():
        mode = ssh_config.stat().st_mode & 0o777
        checks.append(Check("generated ssh config", "ok" if not mode & 0o077 else "warn", f"{ssh_config} (mode {mode:04o})"))
    elif context.config.environments:
        checks.append(Check("generated ssh config", "warn", "not written yet", "run any sshfleet command"))

    checks.append(host_key_check(context.config.ssh.strict_host_key_checking))

    checks.extend(service_checks())

    for warning in context.warnings:
        checks.append(Check("startup", "warn", warning))

    return checks


def host_key_check(policy: str) -> Check:
    """How a host's key is believed. sshfleet's line comes first in the generated config, so it
    is the one in effect whatever the SSH files say (SECURITY.md, "How it connects")."""
    value = str(policy).strip().lower()
    if value == "yes":
        return Check("host keys", "ok", "only keys already in known_hosts are accepted (StrictHostKeyChecking yes)")
    if value in ("no", "off"):
        return Check(
            "host keys",
            "warn",
            "any key is accepted, a changed one included (StrictHostKeyChecking no)",
            "set ssh.strict_host_key_checking: accept-new, or yes with the keys in ~/.ssh/known_hosts",
        )
    if value == "ask":
        return Check(
            "host keys",
            "warn",
            "a host not in known_hosts cannot be reached: ssh would ask, and sshfleet never lets it (BatchMode)",
            "add the keys to ~/.ssh/known_hosts and set ssh.strict_host_key_checking: yes",
        )
    return Check(
        "host keys",
        "ok",
        "a host's key is trusted the first time it is seen, then checked (StrictHostKeyChecking accept-new). "
        "To trust none unseen, add the keys to ~/.ssh/known_hosts and set ssh.strict_host_key_checking: yes",
    )


def service_checks(manager: service_module.Supervisor | None = None) -> list[Check]:
    """The background service, when there is one. One nobody installed is nothing to nag about."""
    if manager is None:
        try:
            manager = service_module.supervisor()
        except service_module.ServiceError:
            return []
    if not manager.installed:
        return []
    try:
        status = manager.status()
    except service_module.ServiceError as exc:
        return [Check("background service", "warn", str(exc))]
    detail = status.state + (f" · {status.url}" if status.url else "") + (f" · {status.detail}" if status.detail else "")
    if status.active:
        return [Check("background service", "ok", detail)]
    fix = "sshfleet service logs" if status.state == "failed" else "sshfleet service start"
    return [Check("background service", "warn", detail, fix)]


def worst(checks: list[Check]) -> Level:
    if any(c.level == "fail" for c in checks):
        return "fail"
    if any(c.level == "warn" for c in checks):
        return "warn"
    return "ok"
