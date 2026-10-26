"""Load and save ``config.yaml``.

Writes are atomic (tmp + ``os.replace``) with a ``.bak`` copy, and the file is always 0600 —
it names key paths and tunnel endpoints even though it never holds secrets.
"""

from __future__ import annotations

import types
import typing
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from ..fsutil import atomic_write
from . import defaults as D
from .schema import (
    ConcurrencyCfg,
    Config,
    ConfigError,
    EnvironmentCfg,
    FilesCfg,
    FrontendsCfg,
    HostOverride,
    LogsCfg,
    ProbesCfg,
    ProjectCfg,
    RefreshCfg,
    SshCfg,
    Thresholds,
    TunnelCfg,
)

#: What OpenSSH takes for StrictHostKeyChecking.
HOST_KEY_POLICIES = ("yes", "no", "ask", "accept-new", "off")


def _fill(cls: type, raw: Any, section: str, rename: dict[str, str] | None = None, *, minimum: int = 0) -> Any:
    """Build a simple dataclass from a dict, each value checked against its field's type.

    Unknown keys are ignored and a key left empty keeps its default. A value of the wrong kind is a
    ConfigError naming the key: unchecked, ``refresh.fast_s: "abc"`` loaded as a string and failed
    later, inside the refresh loop, as an endless "refresh failed". Whole numbers are at least
    ``minimum``.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"{section} must be a mapping of settings, not {type(raw).__name__}")
    rename = rename or {}
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for key, value in raw.items():
        target = rename.get(key, key)
        if target in hints and value is not None:
            kwargs[target] = _coerce(value, hints[target], f"{section}.{key}", minimum)
    return cls(**kwargs)


def _coerce(value: Any, hint: Any, where: str, minimum: int) -> Any:
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        hint = next(arg for arg in typing.get_args(hint) if arg is not type(None))
        origin = typing.get_origin(hint)
    if hint is bool:
        if isinstance(value, bool):
            return value
        word = str(value).strip().lower()
        if word in ("true", "yes", "on"):
            return True
        if word in ("false", "no", "off"):
            return False
        raise ConfigError(f"{where} must be true or false, not {value!r}")
    if hint is int:
        number = value if isinstance(value, int) and not isinstance(value, bool) else None
        if number is None and isinstance(value, str) and value.strip().isdigit():
            number = int(value)
        if number is None:
            raise ConfigError(f"{where} must be a whole number, not {value!r}")
        if number < minimum:
            raise ConfigError(f"{where} must be at least {minimum}, not {number}")
        return number
    if hint is float:
        if isinstance(value, bool):
            raise ConfigError(f"{where} must be a number, not {value!r}")
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where} must be a number, not {value!r}") from None
    if hint is str:
        if isinstance(value, bool):
            # YAML reads a bare yes/no/on/off as true or false.
            return "yes" if value else "no"
        if isinstance(value, (str, int, float)):
            return str(value)
        raise ConfigError(f"{where} must be text, not {type(value).__name__}")
    if origin is list or hint is list:
        if isinstance(value, list):
            return value
        raise ConfigError(f"{where} must be a list")
    if origin is dict or hint is dict:
        if isinstance(value, dict):
            return value
        raise ConfigError(f"{where} must be a mapping")
    return value


def _ssh(raw: Any) -> SshCfg:
    ssh: SshCfg = _fill(SshCfg, raw, "ssh")
    policy = ssh.strict_host_key_checking.strip().lower()
    if policy not in HOST_KEY_POLICIES:
        raise ConfigError(f"ssh.strict_host_key_checking must be one of {', '.join(HOST_KEY_POLICIES)}, not {ssh.strict_host_key_checking!r}")
    ssh.strict_host_key_checking = policy
    return ssh


def load(path: Path) -> Config:
    """Read the config file; a missing file yields an empty (but valid) config."""
    if not path.exists():
        return Config()

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML — {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level, got {type(raw).__name__}")
    return parse_mapping(raw, source=str(path))


def parse_text(text: str, *, source: str = "config") -> Config:
    """Parse config text without touching disk. Every way it can be wrong is a ConfigError, so a
    caller that checks before writing (restore, import) has one thing to catch."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML — {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level, got {type(raw).__name__}")
    return parse_mapping(raw, source=source)


def parse_mapping(raw: dict[str, Any], *, source: str = "config") -> Config:
    """``load_mapping``, with the errors a malformed mapping raises on its way through (an
    environment with no name, a list where a mapping belongs, a port that is not a number)
    turned into ConfigError."""
    try:
        return load_mapping(raw)
    except ConfigError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ConfigError(f"{source}: {type(exc).__name__}: {exc}") from exc


def _host_overrides(raw: dict[str, Any]) -> dict[str, HostOverride]:
    """Every host record, with the host named in the error when one of them is wrong."""
    out: dict[str, HostOverride] = {}
    for alias, entry in raw.items():
        try:
            out[alias] = HostOverride.from_dict(entry or {})
        except ConfigError as exc:
            raise ConfigError(f"hosts.{alias}: {exc}") from exc
    return out


#: What an environment is filed under when a file predates projects and names none.
DEFAULT_PROJECT = "default"


def _file_every_environment_under_a_project(
    projects: list[ProjectCfg], environments: list[EnvironmentCfg]
) -> None:
    """Repair, rather than reject, a file where an environment belongs to no project.

    Every environment belongs to exactly one project, so the rest of the tool never has to ask
    whether one is filed. A file written before projects existed, or edited by hand, is brought up
    to that shape here: the environments join the first project there is, or a new one called
    ``default`` that the operator can rename afterwards. A project an environment names but that
    has no record of its own is declared, so ``config.projects`` is always the whole list.
    """
    for env in environments:
        if env.project and not any(p.name == env.project for p in projects):
            projects.append(ProjectCfg(name=env.project))

    orphans = [e for e in environments if not e.project]
    if not orphans:
        return
    if not projects:
        projects.append(ProjectCfg(name=DEFAULT_PROJECT))
    for env in orphans:
        env.project = projects[0].name


def load_mapping(raw: dict[str, Any]) -> Config:
    """Parse an already-read mapping. The upgrade path validates against this before writing."""
    projects = [ProjectCfg.from_dict(p) for p in (raw.get("projects") or [])]
    seen_projects: set[str] = set()
    for project in projects:
        if project.name in seen_projects:
            raise ConfigError(f"duplicate project {project.name!r}")
        seen_projects.add(project.name)

    environments = [EnvironmentCfg.from_dict(e) for e in (raw.get("environments") or [])]
    seen: set[str] = set()
    for env in environments:
        if env.id in seen:
            raise ConfigError(f"duplicate environment {env.id!r} (two projects may share a name, but each needs its own id)")
        seen.add(env.id)

    _file_every_environment_under_a_project(projects, environments)
    # A name is unique inside its project only, which is where it is shown.
    named: set[tuple[str, str]] = set()
    for env in environments:
        if (env.project, env.name) in named:
            raise ConfigError(f"project {env.project!r} has two environments called {env.name!r}")
        named.add((env.project, env.name))

    known = set(D.DEFAULT_CONFIG) | {"extra"}
    return Config(
        version=int(raw.get("version") or 1),
        projects=projects,
        environments=environments,
        probes=_fill(ProbesCfg, raw.get("probes") or {}, "probes"),
        frontends=_fill(FrontendsCfg, raw.get("frontends") or {}, "frontends"),
        hosts=_host_overrides(raw.get("hosts") or {}),
        # fast_s: 0 is automatic refresh switched off, from the settings page.
        refresh=_fill(RefreshCfg, raw.get("refresh") or {}, "refresh"),
        # Zero connections at a time would stall every check.
        concurrency=_fill(ConcurrencyCfg, raw.get("concurrency") or {}, "concurrency", {"global": "global_"}, minimum=1),
        thresholds=_fill(Thresholds, raw.get("thresholds") or {}, "thresholds"),
        ssh=_ssh(raw.get("ssh") or {}),
        tunnels={k: TunnelCfg.from_dict(k, v or {}) for k, v in (raw.get("tunnels") or {}).items()},
        files=_fill(FilesCfg, raw.get("files") or {}, "files"),
        ui=dict(raw.get("ui") or D.DEFAULT_CONFIG["ui"]),
        logs=_fill(LogsCfg, raw.get("logs") or {}, "logs"),
        extra={k: v for k, v in raw.items() if k not in known},
    )


def to_dict(config: Config) -> dict[str, Any]:
    data: dict[str, Any] = {"version": config.version}
    if config.projects:
        data["projects"] = [p.to_dict() for p in config.projects]
    data["environments"] = [e.to_dict() for e in config.environments]
    data["probes"] = {f: getattr(config.probes, f) for f in config.probes.__dataclass_fields__}
    data["frontends"] = {f: getattr(config.frontends, f) for f in config.frontends.__dataclass_fields__}
    if config.hosts:
        # asdict, not vars: HostOverride is a slotted dataclass and has no __dict__.
        data["hosts"] = {
            alias: {f: v for f, v in asdict(override).items() if v not in (None, [], {}, "")}
            for alias, override in config.hosts.items()
        }
    for name, obj, rename in (
        ("refresh", config.refresh, {}),
        ("concurrency", config.concurrency, {"global_": "global"}),
        ("thresholds", config.thresholds, {}),
        ("ssh", config.ssh, {}),
        ("files", config.files, {}),
        ("logs", config.logs, {}),
    ):
        data[name] = {rename.get(f, f): getattr(obj, f) for f in obj.__dataclass_fields__}
    if config.tunnels:
        data["tunnels"] = {
            name: {
                "env": t.env,
                "via": t.via,
                "remote": t.remote,
                "local": t.local,
                **({"open": t.open_url} if t.open_url else {}),
            }
            for name, t in config.tunnels.items()
        }
    data["ui"] = config.ui or D.DEFAULT_CONFIG["ui"]
    data.update(config.extra)
    return data


def save(config: Config, path: Path) -> None:
    """Atomically write the config, keeping one backup of the previous version."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = yaml.safe_dump(to_dict(config), sort_keys=False, allow_unicode=True, width=100)
    if path.exists():
        atomic_write(path.with_suffix(path.suffix + ".bak"), path.read_bytes(), mode=0o600)
    atomic_write(path, text, mode=0o600)
